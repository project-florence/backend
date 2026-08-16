"""Technical analysis metrics for the FX & precious-metals pipeline.

Design spec 7.1: SMA 5/20/50/200, price-vs-SMA-20 deviation, Wilder RSI-14,
20-day annualized volatility, daily/weekly/monthly changes, 52-week window
extremes + rank, all-time extremes and cross-symbol Pearson correlations.
Pure computation over ``Candle`` series — no DB/Redis access here; callers
(cron task, API) supply candles loaded from ``rate_candles`` / ``economy_rates``.

numpy is a sync library: in async code always call the ``*_async`` wrappers
(``asyncio.to_thread``) so the event loop is never blocked (AGENTS.md rule).

Insufficient data never raises: every metric degrades to ``None`` when its
window cannot be filled (e.g. fewer than 5 candles -> sma_5 None, ...
fewer than 2 closes -> all windowed metrics None).
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Sequence

import numpy as np

from src.finance.models import AnalysisResult, Candle

# Trading-day windows (design spec 7.1).
RSI_PERIOD = 14
VOL_WINDOW = 20          # 20-day volatility window
WEEK_BACK = 5            # ~1 week of trading days
MONTH_BACK = 21          # ~1 month of trading days
YEAR_BACK = 252          # ~52 weeks of trading days
TRADING_DAYS_PER_YEAR = 252.0

# Fixed correlation pairs (design spec 7.1, 30-day window).
CORRELATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("XAU-ONS", "XAG-ONS"),
    ("XAU-GRAM", "USD"),
    ("USD", "EUR"),
)

_ANNUALIZATION = math.sqrt(TRADING_DAYS_PER_YEAR)


# ---------------------------------------------------------------------------
# Window helpers (all numpy, all None-tolerant)
# ---------------------------------------------------------------------------


def _sma(values: np.ndarray, window: int) -> float | None:
    """Simple moving average of the last ``window`` values."""
    if window <= 0 or values.size < window:
        return None
    return float(np.mean(values[-window:]))


def _sma_deviation(price: float, sma: float | None) -> float | None:
    """Percent deviation of ``price`` from its SMA-20: (close - sma) / sma * 100."""
    if sma is None or sma <= 0:
        return None
    return (price - sma) / sma * 100.0


def _rsi_wilder(closes: np.ndarray, period: int = RSI_PERIOD) -> float | None:
    """Wilder's smoothed RSI-14 over the full close series.

    Seeds with the simple average of the first ``period`` gains/losses, then
    applies Wilder smoothing (alpha = 1/period). Needs period + 1 closes.
    A zero-loss window returns 100 (or 50 when both gain and loss are zero —
    flat series are neutral, not overbought).
    """
    if closes.size < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, deltas.size):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def _volatility_20d(closes: np.ndarray) -> float | None:
    """Annualized 20-day volatility in percent (design spec 7.1).

    stdev(daily returns over the last 21 closes, sample ddof=1) * sqrt(252) * 100.
    """
    if closes.size < VOL_WINDOW + 1:
        return None
    window = closes[-(VOL_WINDOW + 1):]
    prev = window[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = np.diff(window) / prev
    returns = returns[np.isfinite(returns)]
    if returns.size < 2:
        return None
    return float(np.std(returns, ddof=1)) * _ANNUALIZATION * 100.0


def _pct_change(closes: np.ndarray, back: int) -> float | None:
    """Percent change of the last close vs the close ``back`` positions earlier."""
    if back <= 0 or closes.size <= back:
        return None
    reference = float(closes[-back - 1])
    if reference == 0 or not math.isfinite(reference):
        return None
    return (float(closes[-1]) - reference) / reference * 100.0


# ---------------------------------------------------------------------------
# Per-symbol entry point
# ---------------------------------------------------------------------------


def compute_analysis(symbol: str, candles: list[Candle]) -> AnalysisResult:
    """Compute every per-symbol metric from a 1d candle series (design 7.1).

    Candles are sorted by timestamp and rows with a missing/invalid close are
    dropped before computing. Returns an ``AnalysisResult`` whose uncomputable
    fields are ``None`` — never raises on short/empty input.
    """
    result = AnalysisResult(symbol=symbol, computed_at=datetime.now(timezone.utc))
    valid = [
        c for c in candles
        if c is not None and c.close is not None and math.isfinite(float(c.close))
    ]
    if not valid:
        return result
    valid.sort(key=lambda c: c.ts if c.ts is not None else datetime.min)
    closes = np.asarray([float(c.close) for c in valid], dtype=float)
    highs = np.asarray(
        [float(c.high) if c.high is not None else float(c.close) for c in valid],
        dtype=float,
    )
    lows = np.asarray(
        [float(c.low) if c.low is not None else float(c.close) for c in valid],
        dtype=float,
    )
    price = float(closes[-1])

    # Moving averages + deviation.
    result.sma_5 = _sma(closes, 5)
    result.sma_20 = _sma(closes, 20)
    result.sma_50 = _sma(closes, 50)
    result.sma_200 = _sma(closes, 200)
    result.price_vs_sma_20 = _sma_deviation(price, result.sma_20)

    # Momentum and risk.
    result.rsi_14 = _rsi_wilder(closes)
    result.volatility_20d = _volatility_20d(closes)

    # Period changes over the close series.
    result.change_daily_pct = _pct_change(closes, 1)
    result.change_week_pct = _pct_change(closes, WEEK_BACK)
    result.change_month_pct = _pct_change(closes, MONTH_BACK)

    # 52-week window + all-time extremes (high/low columns).
    w52_high = float(np.max(highs[-YEAR_BACK:]))
    w52_low = float(np.min(lows[-YEAR_BACK:]))
    result.high_52w = w52_high
    result.low_52w = w52_low
    if w52_high > w52_low:
        result.rank_in_52w = (price - w52_low) / (w52_high - w52_low)
    result.all_time_high = float(np.max(highs))
    result.all_time_low = float(np.min(lows))
    return result


async def compute_analysis_async(symbol: str, candles: list[Candle]) -> AnalysisResult:
    """Async wrapper — runs the numpy work in a worker thread (AGENTS.md)."""
    return await asyncio.to_thread(compute_analysis, symbol, candles)


# ---------------------------------------------------------------------------
# Cross-symbol correlations (design spec 7.1: fixed pair table, 30-day window)
# ---------------------------------------------------------------------------


def compute_correlations(
    series: dict[str, list[float]],
    pairs: Sequence[tuple[str, str]] | None = None,
    window: int = 30,
) -> dict[tuple[str, str], float | None]:
    """Pearson correlation for each pair over the last ``window`` aligned closes.

    ``series`` maps canonical symbol -> close list (chronological). Pair values
    are ``None`` when either leg is missing, too short, or has zero variance.
    """
    pairs = list(pairs) if pairs is not None else list(CORRELATION_PAIRS)
    out: dict[tuple[str, str], float | None] = {}
    for left, right in pairs:
        a = series.get(left)
        b = series.get(right)
        if not a or not b:
            out[(left, right)] = None
            continue
        n = min(window, len(a), len(b))
        x = np.asarray(a[-n:], dtype=float)
        y = np.asarray(b[-n:], dtype=float)
        if x.size < 2 or y.size < 2 or float(np.std(x)) == 0 or float(np.std(y)) == 0:
            out[(left, right)] = None
            continue
        out[(left, right)] = float(np.corrcoef(x, y)[0, 1])
    return out


async def compute_correlations_async(
    series: dict[str, list[float]],
    pairs: Sequence[tuple[str, str]] | None = None,
    window: int = 30,
) -> dict[tuple[str, str], float | None]:
    """Async wrapper — runs the numpy work in a worker thread (AGENTS.md)."""
    return await asyncio.to_thread(compute_correlations, series, pairs, window)