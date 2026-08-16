"""Analysis metrics — hand-computed SMA/RSI/volatility over synthetic candles.

Pure computation over Candle series (design spec 7.1): SMA 5/20/50/200,
price-vs-SMA-20 deviation, Wilder RSI-14, 20-day annualized volatility,
daily/weekly/monthly changes, 52-week + all-time extremes and rank.
Insufficient data must never raise — uncomputable fields degrade to None.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.finance.analysis import (
    compute_analysis,
    compute_analysis_async,
    compute_correlations,
)
from src.finance.models import Candle

BASE_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candles(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            symbol="XAU-ONS",
            interval="1d",
            ts=BASE_TS + timedelta(days=i),
            open=float(c),
            high=float(c),
            low=float(c),
            close=float(c),
        )
        for i, c in enumerate(closes)
    ]


def _rising_series(n: int = 30) -> list[float]:
    """Strictly rising close series with mild wiggle (returns vary)."""
    return [100.0 + i + (0.3 if i % 3 == 1 else 0.0) for i in range(n)]


# ---------------------------------------------------------------------------
# SMA / deviations / period changes (hand-computed)
# ---------------------------------------------------------------------------


def test_sma20_and_changes_match_hand_computation():
    closes = _rising_series(30)
    result = compute_analysis("XAU-ONS", _candles(closes))

    hand_sma20 = sum(closes[-20:]) / 20
    assert result.sma_20 == pytest.approx(hand_sma20)
    assert result.sma_5 == pytest.approx(sum(closes[-5:]) / 5)
    # windows too small for 50/200 on a 30-candle series
    assert result.sma_50 is None
    assert result.sma_200 is None

    price = closes[-1]
    assert result.price_vs_sma_20 == pytest.approx((price - hand_sma20) / hand_sma20 * 100)

    def pct(back: int) -> float:
        return (closes[-1] - closes[-back - 1]) / closes[-back - 1] * 100

    assert result.change_daily_pct == pytest.approx(pct(1))
    assert result.change_week_pct == pytest.approx(pct(5))
    assert result.change_month_pct == pytest.approx(pct(21))


# ---------------------------------------------------------------------------
# RSI-14
# ---------------------------------------------------------------------------


def test_rsi_is_100_for_strictly_rising_series():
    # zero-loss window -> 100 (documented behavior; no loss ever occurred)
    result = compute_analysis("XAU-ONS", _candles(_rising_series(30)))
    assert result.rsi_14 == pytest.approx(100.0)


def test_rsi_matches_hand_computation():
    # 15 closes with deltas +3/-1 repeated: the seed window (14 deltas) covers
    # the whole series, so no Wilder smoothing iterations run -> avg_gain =
    # 7*3/14 = 1.5, avg_loss = 7*1/14 = 0.5 -> rs = 3 -> RSI = 75.0 exactly.
    closes = [100.0 + 2 * ((i + 1) // 2) + (i % 2) for i in range(15)]
    result = compute_analysis("XAU-ONS", _candles(closes))
    assert result.rsi_14 == pytest.approx(75.0)
    assert result.rsi_14 is not None
    assert 0.0 < result.rsi_14 < 100.0


def test_rsi_bounded_for_longer_mixed_series():
    # Same pattern over 30 closes: smoothing blends, value stays strictly
    # inside (0, 100) since both gains and losses are non-zero.
    closes = [100.0 + 2 * ((i + 1) // 2) + (i % 2) for i in range(30)]
    result = compute_analysis("XAU-ONS", _candles(closes))
    assert result.rsi_14 is not None
    assert 0.0 < result.rsi_14 < 100.0


# ---------------------------------------------------------------------------
# Volatility (20d annualized, %)
# ---------------------------------------------------------------------------


def test_volatility_positive_for_varying_returns():
    result = compute_analysis("XAU-ONS", _candles(_rising_series(30)))
    assert result.volatility_20d is not None
    assert result.volatility_20d > 0


def test_volatility_hand_computed_for_geometric_series():
    # constant 1% daily return -> zero volatility
    closes = [100.0 * (1.01 ** i) for i in range(30)]
    result = compute_analysis("XAU-ONS", _candles(closes))
    assert result.volatility_20d == pytest.approx(0.0, abs=1e-9)


def test_volatility_matches_annualization_formula():
    closes = _rising_series(30)
    result = compute_analysis("XAU-ONS", _candles(closes))
    window = np.asarray(closes[-21:], dtype=float)
    returns = np.diff(window) / window[:-1]
    expected = float(np.std(returns, ddof=1)) * np.sqrt(252) * 100.0
    assert result.volatility_20d == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Extremes: 52-week window + all-time
# ---------------------------------------------------------------------------


def test_extremes_and_rank_hand_computed():
    closes = _rising_series(30)
    result = compute_analysis("XAU-ONS", _candles(closes))
    assert result.high_52w == pytest.approx(max(closes))
    assert result.low_52w == pytest.approx(min(closes))
    assert result.all_time_high == pytest.approx(max(closes))
    assert result.all_time_low == pytest.approx(min(closes))
    assert result.rank_in_52w == pytest.approx(
        (closes[-1] - min(closes)) / (max(closes) - min(closes))
    )
    assert result.rank_in_52w is not None
    assert 0.0 <= result.rank_in_52w <= 1.0


# ---------------------------------------------------------------------------
# Insufficient data — None fields, never raises
# ---------------------------------------------------------------------------


def test_three_candle_series_degrades_to_none():
    result = compute_analysis("XAU-ONS", _candles([100.0, 101.0, 102.5]))
    assert result.sma_5 is None
    assert result.sma_20 is None
    assert result.sma_50 is None
    assert result.sma_200 is None
    assert result.price_vs_sma_20 is None
    assert result.rsi_14 is None            # needs 15 closes
    assert result.volatility_20d is None    # needs 21 closes
    assert result.change_week_pct is None   # needs 6 closes
    assert result.change_month_pct is None  # needs 22 closes
    assert result.change_daily_pct == pytest.approx((102.5 - 101.0) / 101.0 * 100)
    assert result.high_52w == pytest.approx(102.5)
    assert result.all_time_high == pytest.approx(102.5)
    assert result.rank_in_52w == pytest.approx(1.0)


def test_empty_series_returns_empty_result():
    result = compute_analysis("XAU-ONS", [])
    assert result.symbol == "XAU-ONS"
    assert result.sma_20 is None
    assert result.rsi_14 is None
    assert result.high_52w is None
    assert result.all_time_high is None


# ---------------------------------------------------------------------------
# Async wrapper + correlations
# ---------------------------------------------------------------------------


async def test_compute_analysis_async_wrapper():
    result = await compute_analysis_async("XAU-ONS", _candles(_rising_series(30)))
    assert result.sma_20 is not None
    assert result.rsi_14 == pytest.approx(100.0)


def test_correlations_perfect_positive_and_missing():
    a = [float(i) for i in range(30)]
    out = compute_correlations({"XAU-ONS": a, "XAG-ONS": [x * 2 + 5 for x in a]})
    assert out[("XAU-ONS", "XAG-ONS")] == pytest.approx(1.0)
    # pairs with a missing leg degrade to None
    assert out[("XAU-GRAM", "USD")] is None
    assert out[("USD", "EUR")] is None


def test_correlations_zero_variance_returns_none():
    out = compute_correlations({"XAU-ONS": [1.0] * 30, "XAG-ONS": [1.0] * 30})
    assert out[("XAU-ONS", "XAG-ONS")] is None