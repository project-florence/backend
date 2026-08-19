"""simulate_from_data — pure Monte Carlo helper, no network / no DB."""

import pytest

from src.simulation.montecarlo import simulate_from_data


def _make_history(n=50, base=100.0, step=0.5):
    return [{"close": base + i * step} for i in range(n)]


def test_simulate_from_data_returns_expected_shape():
    history = _make_history()
    result = simulate_from_data(history, days=30, bounds="0.05", target=110.0)

    assert set(result) == {"prob_above", "prob_below", "confidence"}

    prob_above = result["prob_above"]
    prob_below = result["prob_below"]
    assert isinstance(prob_above, float)
    assert 0.0 <= prob_above <= 1.0
    assert 0.0 <= prob_below <= 1.0
    assert prob_above + prob_below == pytest.approx(1.0)

    confidence = result["confidence"]
    assert set(confidence) == {"min", "max", "percent", "days", "bounds"}
    assert confidence["days"] == 30
    assert confidence["bounds"] == "0.05"
    assert 0.0 <= confidence["percent"] <= 1.0
    assert confidence["min"] <= confidence["max"]


def test_simulate_from_data_auto_target_uses_current_price():
    history = _make_history()
    result = simulate_from_data(history, days=30, current_price=100.0)
    assert result["prob_above"] + result["prob_below"] == pytest.approx(1.0)
    assert isinstance(result["prob_above"], float)


def test_simulate_from_data_requires_current_price_when_no_target():
    history = _make_history()
    with pytest.raises(ValueError, match="target requires current_price"):
        simulate_from_data(history, days=30, target=None, current_price=None)


def test_simulate_from_data_ffills_nan_closes():
    history = _make_history()
    history[10]["close"] = float("nan")
    result = simulate_from_data(history, days=30, target=110.0)
    assert result["prob_above"] + result["prob_below"] == pytest.approx(1.0)
