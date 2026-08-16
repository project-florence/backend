"""yfinance FX provider — spot (last close) + historical daily candles.

Symbols ``USDTRY=X`` / ``EURTRY=X`` / ``GBPTRY=X`` ... (verified live 2026-08-16;
``XAUUSD=X`` style pairs do NOT exist on Yahoo — see yfinance_metals for
ounces via futures). yfinance is a sync library: every call runs through
``asyncio.to_thread`` and the shared 1.5 s ``yfinance_rate_limiter``.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone

import yfinance as yf

from src.finance.models import Candle, ProviderName, Quote
from src.finance.providers.base import BaseProvider, make_quote, yfinance_rate_limiter
from src.finance.symbols import SYMBOL_REGISTRY

logger = logging.getLogger(__name__)


def _clean(val):
    if val is None:
        return None
    if hasattr(val, "item"):
        val = val.item()
    if isinstance(val, float) and math.isnan(val):
        return None
    return val


def _to_utc(ts) -> datetime:
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc)
    return dt


def _fetch_last_close(raw_symbol: str) -> dict | None:
    """Sync: last 1d close snapshot for a raw Yahoo symbol."""
    yfinance_rate_limiter.wait()
    df = yf.Ticker(raw_symbol).history(period="5d", interval="1d")
    if df is None or df.empty:
        return None
    row = df.iloc[-1]
    return {
        "ts": _to_utc(row.name),
        "open": _clean(row.get("Open")),
        "high": _clean(row.get("High")),
        "low": _clean(row.get("Low")),
        "close": _clean(row.get("Close")),
    }


def _fetch_history(raw_symbol: str, interval: str, start, end):
    """Sync: OHLCV history for a raw Yahoo symbol."""
    yfinance_rate_limiter.wait()
    return yf.Ticker(raw_symbol).history(start=start, end=end, interval=interval)


class YFinanceFxProvider(BaseProvider):
    """FX spot fallback via ``{CODE}TRY=X`` last close."""

    name = ProviderName.YFINANCE_FX

    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        wanted = sorted(s for s in symbols if s in self.provides)
        if not wanted:
            return {}
        results: dict[str, Quote] = {}
        errors: list[Exception] = []
        for symbol in wanted:
            raw = SYMBOL_REGISTRY[symbol].provider_symbols[self.name]
            try:
                snap = await asyncio.to_thread(_fetch_last_close, raw)
            except Exception as exc:
                errors.append(exc)
                continue
            if snap is None or snap["close"] is None:
                continue
            results[symbol] = make_quote(
                symbol,
                buying=snap["close"],
                selling=snap["close"],
                price=snap["close"],
                ts=snap["ts"],
                source=self.name,
                extra={"raw_symbol": raw, **{k: snap[k] for k in ("open", "high", "low")}},
            )
        if results:
            self.record_success()
        elif errors:
            self.record_failure(errors[0])
        return results

    async def fetch_candles(
        self,
        symbol: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        if symbol not in self.provides:
            return []
        raw = SYMBOL_REGISTRY[symbol].provider_symbols[self.name]
        try:
            df = await asyncio.to_thread(_fetch_history, raw, interval, start, end)
        except Exception as exc:
            self.record_failure(exc)
            return []
        candles: list[Candle] = []
        for ts, row in df.iterrows():
            open_ = _clean(row.get("Open"))
            close = _clean(row.get("Close"))
            if open_ is None or close is None:
                continue
            volume = _clean(row.get("Volume"))
            candles.append(
                Candle(
                    symbol=symbol,
                    interval=interval,
                    ts=_to_utc(ts),
                    open=open_,
                    high=_clean(row.get("High")) or open_,
                    low=_clean(row.get("Low")) or close,
                    close=close,
                    volume=float(volume) if volume is not None else None,
                    source=self.name,
                )
            )
        return candles