"""Finans haber beslemeleri, global endeks cotasyonlari ve makro takvim.

Bu modul, plan dosyasindaki (20260819-rss-data-collection) uc ``client``
katmanini barindirir: tumu "fetch + normalize" isi yapar, Redis'e dokunmaz
(cache orkestrasyonu ``src/services/marketfeed.py``'de). Tum fonksiyonlar
down-tolerant'tir: hata durumunda *asla* raise etmezler — bos liste / None
donerler.

Konvansiyonlar (AGENTS.md):
- Ag: ortak async httpx client ``await get_client()``.
- Sync kutuphaneler (feedparser, yfinance) ``asyncio.to_thread`` icinde.
- FRED makro takvimi; fredapi 0.5.2'de ``get_releases``/``get_release_dates``
  YOK, bu yuzden FRED REST ``releases/dates`` ucu httpx ile cagrilir
  (FRED_API_KEY yoksa / placeholder ise -> bos liste).

GDELT deprecated oldugu icin KULLANILMAZ.
"""

import asyncio
import calendar
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import pandas as pd
from pydantic import BaseModel

from src.clients.http import get_client

logger = logging.getLogger(__name__)

FRED_BASE = "https://api.stlouisfed.org/fred"

# FRED_API_KEY orten "gercek disi" degerler — bos liste donmek istedigimiz
# placeholder'lar (bkz. .env.example). Gercek anahtar yoksa FRED'e istek atmayiz.
_FRED_PLACEHOLDERS = {"your_fred_api_key", "none", "changeme", "fred-api-key"}


# --------------------------------------------------------------------------
# Veri modelleri (Pydantic — kolay JSON serialize / Redis cache)
# --------------------------------------------------------------------------
class FeedItem(BaseModel):
    title: str
    link: str
    url: str          # kaynak feed URL'si
    source: str       # insan-okur kaynak adi (orn. "Yahoo Finance")
    published: datetime | None = None
    summary: str | None = None


class IndexQuote(BaseModel):
    key: str          # yfinance sembolu (orn. "^GSPC")
    name: str
    value: float | None
    change_pct: float | None
    as_of: datetime | None = None


class MacroEvent(BaseModel):
    date: datetime | None
    region: str
    event: str
    series: str | None = None    # FRED release_id
    impact: str | None = None    # "high" | "medium" | "low" | None


# --------------------------------------------------------------------------
# 1. RSS / finans haber besleme
# --------------------------------------------------------------------------
def _published_to_dt(parsed) -> datetime | None:
    """feedparser ``published_parsed`` (UTC time.struct_time) -> tz-aware datetime."""
    if not parsed:
        return None
    try:
        ts = calendar.timegm(parsed)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _parse_rss(content: bytes, feed_url: str, source: str, limit: int) -> list[FeedItem]:
    """feedparser ile parse + normalize. Hata tolere edilir (bos liste)."""
    try:
        parsed = feedparser.parse(content)
    except Exception as exc:  # pragma: no cover - feedparser nadiren raise eder
        logger.warning("feedparser parse error for %s: %s", source, exc)
        return []

    entries = getattr(parsed, "entries", None) or []
    items: list[FeedItem] = []
    for e in entries[:limit]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title:
            continue
        items.append(
            FeedItem(
                title=title,
                link=link,
                url=feed_url,
                source=source,
                published=_published_to_dt(e.get("published_parsed") or e.get("updated_parsed")),
                summary=(e.get("summary") or e.get("description") or "").strip() or None,
            )
        )
    return items


async def fetch_rss(url: str, limit: int = 10, source: str | None = None) -> list[FeedItem]:
    """Tek RSS feed'ini ortak httpx client ile indir, feedparser ile parse et.

    HTTP 200 degilse veya parse edilemezse bos liste doner (raise etmez).
    ``source`` verilmezse URL'den turetir.
    """
    src = source or url
    try:
        client = await get_client()
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning("RSS %s returned HTTP %s", url, resp.status_code)
            return []
        # feedparser C-uzantisi dosya/yol alabilir; bytes icin set_content kullanilir
        return await asyncio.to_thread(_parse_rss, resp.content, url, src, limit)
    except Exception as exc:
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return []


# --------------------------------------------------------------------------
# 2. Global endeks cotasyonlari (yfinance tek batch)
# --------------------------------------------------------------------------
def _download_indices(symbols: list[str]) -> dict[str, tuple[float, float | None, datetime] | None]:
    """Sync yardimci: yfinance ``download`` tek seferde tum sembolleri ceker.

    Doner: {sembol: (kapanis, change_pct, as_of)} — basarisiz sembol None.
    """
    if not symbols:
        return {}
    import yfinance as yf

    df = yf.download(symbols, period="5d", interval="1d", progress=False,
                     group_by="ticker", auto_adjust=False, threads=True)
    out: dict[str, tuple[float, float | None, datetime] | None] = {}
    if df is None or df.empty:
        return {s: None for s in symbols}

    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            if multi:
                if sym not in df.columns.get_level_values(0):
                    out[sym] = None
                    continue
                close = df[sym]["Close"].dropna()
            else:
                close = df["Close"].dropna()
            if len(close) < 2:
                out[sym] = None
                continue
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            chg = ((last / prev) - 1.0) * 100.0 if prev else None
            as_of = close.index[-1].to_pydatetime()
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
            out[sym] = (last, chg, as_of)
        except Exception as exc:
            logger.warning("yfinance index %s failed: %s", sym, exc)
            out[sym] = None
    return out


async def fetch_indices(symbols: list[str] | None = None,
                        names: dict[str, str] | None = None) -> list[IndexQuote]:
    """Global endeksler: son kapanis + gunluk degisim. Tek sembol toleransli.

    ``symbols``/``names`` verilmezse config ``marketfeed`` varsayilanlarindan alinir.
    """
    from src.core.config import get_config

    cfg = get_config()["marketfeed"]
    symbols = symbols or cfg["index_symbols"]
    names = names or cfg["index_names"]

    try:
        data = await asyncio.to_thread(_download_indices, list(symbols))
    except Exception as exc:
        logger.warning("yfinance index batch failed entirely: %s", exc)
        return []

    quotes: list[IndexQuote] = []
    for sym in symbols:
        row = data.get(sym)
        if row is None:
            continue  # tek sembol basarisiz -> atla, digerleri kalsin
        value, chg, as_of = row
        quotes.append(
            IndexQuote(
                key=sym,
                name=names.get(sym, sym),
                value=value,
                change_pct=chg,
                as_of=as_of,
            )
        )
    return quotes


# --------------------------------------------------------------------------
# 3. Makro takvim (yaklasan yayin tarihleri)
# --------------------------------------------------------------------------
# (region, alt-kelime -> impact) eslemesi: release_name'de aranan anahtarlar.
# Duyarliligi artirmak icin onemli serilere (FOMC, CPI, GDP, tarim-disi istihdam)
# "high" verilir.
_MACRO_RULES: list[tuple[str, list[str], str]] = [
    ("US", ["federal open market committee", "fomc"], "high"),
    ("US", ["consumer price index", "cpi", "cpi"], "high"),
    ("US", ["unemployment", "employment situation", "nonfarm payroll", "payroll"], "high"),
    ("US", ["real gross domestic product", "gross domestic product", "gdp"], "high"),
    ("US", ["producer price index"], "medium"),
    ("US", ["advance monthly retail", "retail sales"], "medium"),
    ("US", ["housing starts"], "medium"),
    ("US", ["industrial production"], "medium"),
    ("US", ["job openings", "jolts"], "medium"),
    ("US", ["personal income", "personal consumption"], "medium"),
    ("US", ["consumer credit"], "low"),
    ("US", ["new residential sales"], "low"),
    ("US", ["international trade", "trade in goods and services"], "medium"),
]


def _classify_release(name: str) -> tuple[str, str] | None:
    """Release adindan (region, impact) bulur; onemsizse None."""
    lower = (name or "").lower()
    best: tuple[str, str] | None = None
    for region, keywords, impact in _MACRO_RULES:
        if any(kw in lower for kw in keywords):
            # ilk eslesen siralama kurali zaten oncelikli; en yuksek impact'i koru
            if best is None or _impact_rank(impact) > _impact_rank(best[1]):
                best = (region, impact)
    return best


def _impact_rank(i: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(i or "", 0)


async def fetch_macro_calendar(days: int = 45) -> list[MacroEvent]:
    """Yaklasan onemli makro yayin tarihleri (FRED releases/dates).

    FRED_API_KEY yoksa veya placeholder ise -> ``[]`` (down-tolerant).
    fredapi 0.5.2'de get_releases/get_release_dates bulunmadigi icin FRED REST
    ucu dogrudan cagrilir. Not: TCMB karar tarihleri scraping gerektirir —
    simdilik TBD / kapsam disi.
    """
    key = os.getenv("FRED_API_KEY")
    if not key or key.strip() in _FRED_PLACEHOLDERS:
        return []

    today = datetime.now(timezone.utc)
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    params = {
        "api_key": key,
        "file_type": "json",
        "realtime_start": start,
        "realtime_end": end,
        "include_release_dates_with_no_data": "true",
        "limit": 1000,
    }
    try:
        client = await get_client()
        resp = await client.get(f"{FRED_BASE}/releases/dates", params=params)
        if resp.status_code != 200:
            logger.warning("FRED releases/dates HTTP %s", resp.status_code)
            return []
        payload = resp.json()
    except Exception as exc:
        logger.warning("FRED macro calendar fetch failed: %s", exc)
        return []

    events: list[MacroEvent] = []
    cutoff_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_end = today + timedelta(days=days)
    for row in payload.get("releases_dates", []) or []:
        name = row.get("release_name") or ""
        date_str = row.get("date")
        region_impact = _classify_release(name)
        if region_impact is None:
            continue
        if not date_str:
            continue
        try:
            d = datetime.fromisoformat(date_str)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (cutoff_start <= d <= cutoff_end):
            continue
        region, impact = region_impact
        events.append(
            MacroEvent(
                date=d,
                region=region,
                event=name,
                series=row.get("release_id"),
                impact=impact,
            )
        )

    # tarihe gore sirala
    events.sort(key=lambda e: e.date or datetime.min.replace(tzinfo=timezone.utc))
    return events
