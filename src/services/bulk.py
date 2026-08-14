"""Toplu (bulk) yfinance cekimi + yillik veri fill orkestrasyonu.

``build_candle_rows_bulk``: ``yf.download`` ile BIR cagrida birden fazla
ticker'in 1d mum verisini ceker (eski desende her ticker icin ayri
``Ticker.history`` cagrisi yapiliyordu). ``ignore_tz=False`` +
``auto_adjust=True`` SARTTIR: aksi halde zaman damgalari UTC'ye
cevrilmediginde ``price_candles (ticker, interval, ts)`` ON CONFLICT
upsert'i mumlari dogru sekilde eslesmez.

``fill_year``: bir yilin tum BIST ticker'larini 50'lik batch'lerle ceker,
``price._write_candle_rows`` (50k satir chunk) ile yazar. Redis NX lock
(TTL 600s) farkli worker'larin ayni yili iki kez doldurmasini onler;
Redis yoksa (cache'siz mod) fill baslatilmaz — mevcut conservative
davranis korunur. Surec ici ``_active_fills`` guard'i tekrari onler.
"""

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from src.core.database import db
from src.core.redis import r
from src.services.price import _clean, _write_candle_rows

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
BATCH_DELAY = 3
FILL_LOCK_TTL = 600

# Yazma chunk boyutu: _write_candle_rows tek executemany cagrisina verilecek
# maksimum satir (50k — psycopg executemany icin guvenli sinir).
WRITE_CHUNK = 50_000

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# Ayni surec icinde eszamanli fill'leri onleyen guard.
_active_fills: set[int] = set()


def _load_tickers() -> list[str]:
    """data/bist_companies.json'daki ticker kodlari (sirali)."""
    path = DATA_DIR / "bist_companies.json"
    if not path.exists():
        logger.warning("bist_companies.json bulunamadi: %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("bist_companies.json okunamadi: %s", e)
        return []
    if isinstance(data, dict):
        return sorted(data.keys())
    return []


def _download_sync(tickers_is: list[str], start, end, interval: str):
    """yf.download sync cagrisi (to_thread icinde calisir).

    stderr /dev/null'a yonlendirilir (cron deseni): yfinance'in yararli
    olmayan ilerleme/uyari ciktisi log'lari kirletmesin. Sonuc bos olsa
    bile ticker basina satir uretilmez; cagiran boslugu atlar.
    """
    stderr_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        return yf.download(
            tickers=tickers_is,
            start=start,
            end=end,
            interval=interval,
            group_by="ticker",
            auto_adjust=True,
            ignore_tz=False,
            threads=min(12, len(tickers_is)),
            progress=False,
            repair=False,
            timeout=15,
        )
    finally:
        os.dup2(stderr_fd, 2)
        os.close(devnull)
        os.close(stderr_fd)


async def build_candle_rows_bulk(
    tickers_is: list[str], start, end, interval: str = "1d"
) -> list[tuple]:
    """ISTEK SIRASINA gore her ticker icin mum satirlari uretir. DB islemi YOK.

    ``yf.download`` ticker sirasini set ile bozabilir; bu yuzden sonuc
    ``df[ticker]`` ile ISTEK SIRASINDA iterate edilerek toplanir. Her ticker
    icin: ``dropna(how='all')`` -> Close notnull filtresi -> tuple
    ``(ticker, interval, ts, open, high, low, close, volume)``. Volume NaN
    ise 0 yazilir. Ag beklemesi to_thread icinde oldugundan event loop
    bloklanmaz; cagiran taraf cagri oncesi baglanti tutmamalidir.
    """
    if not tickers_is:
        return []
    data = await asyncio.to_thread(_download_sync, tickers_is, start, end, interval)
    if data is None or data.empty:
        return []

    # Tek ticker cagrilarinda yf.download tek-seviye kolon doner
    # (MultiIndex degil); o durumda frame dogrudan o ticker'indir.
    multi = getattr(getattr(data, "columns", None), "nlevels", 1) > 1

    values: list[tuple] = []
    for ticker in tickers_is:
        try:
            df = data[ticker] if multi else data
        except KeyError:
            # Ticker yanitta yok (delisted/bos): atla.
            continue
        df = df.dropna(how="all")
        if df.empty:
            continue
        for ts, row in df.iterrows():
            close = _clean(row.get("Close"))
            if close is None:
                continue
            volume = row.get("Volume")
            if isinstance(volume, (float, int)) and not math.isnan(volume):
                volume = int(volume)
            else:
                volume = 0
            values.append((
                ticker, interval, ts.to_pydatetime(),
                _clean(row.get("Open")), _clean(row.get("High")),
                _clean(row.get("Low")), close, volume,
            ))
    return values


async def _write_candle_rows_chunked(values: list[tuple]) -> None:
    """Satirlari 50k'lik chunk'larla ``price._write_candle_rows``'a verir."""
    for i in range(0, len(values), WRITE_CHUNK):
        await _write_candle_rows(values[i:i + WRITE_CHUNK])


async def fill_year(year: int, tickers: list[str] | None = None) -> None:
    """Yilin 1d verisini bulk yf.download ile cekip price_candles'a yazar.

    Batch 50, batch arasi 3 sn. Redis NX lock (``data-fill:{year}``, TTL
    600s) farkli worker'lari; ``_active_fills`` ayni sureci korur. Redis
    yoksa veya lock alinamazsa fill baslatilmaz (conservative — mevcut
    davranis).
    """
    if year in _active_fills:
        return
    if tickers is None:
        tickers = await asyncio.to_thread(_load_tickers)
    if not tickers:
        logger.warning("bulk-fill %s: ticker listesi bos; fill atlandi", year)
        return

    acquired = await r.set(f"data-fill:{year}", "1", ex=FILL_LOCK_TTL, nx=True)
    if acquired is None:
        # Redis yok (None) veya baska worker kilidi tutuyor (False):
        # konservatif davran — fill baslatma.
        logger.info("bulk-fill %s: Redis lock alinamadi; fill atlandi", year)
        return

    _active_fills.add(year)
    try:
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        total = len(tickers)
        logger.info("bulk-fill %s basladi (%s ticker, %s batch)", year, total, (total + BATCH_SIZE - 1) // BATCH_SIZE)

        for i in range(0, total, BATCH_SIZE):
            batch = [f"{t}.IS" for t in tickers[i:i + BATCH_SIZE]]
            values: list[tuple] = []
            try:
                values = await build_candle_rows_bulk(batch, start, end, "1d")
            except Exception as e:
                # Delisted/bos batch'ler atlanir — sorun degil.
                logger.warning("bulk-fill %s: batch %d cekilemedi: %s", year, i // BATCH_SIZE, e)
            if values:
                try:
                    await _write_candle_rows_chunked(values)
                except Exception as e:
                    # Batch yazim hatasi fill'i oldurmesin; sonraki batch'ler devam etsin.
                    logger.warning("bulk-fill %s: batch %d yazilamadi: %s", year, i // BATCH_SIZE, e)
            if i + BATCH_SIZE < total:
                await asyncio.sleep(BATCH_DELAY)

        logger.info("bulk-fill %s tamamlandi", year)
    finally:
        _active_fills.discard(year)
