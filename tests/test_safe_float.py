"""safe_float — tolerant numeric parse, never raises."""

import math

import pytest

from src.finance.providers.base import safe_float


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Task-mandated cases.
        ("47.8424", 47.8424),
        ("1.234,56", 1234.56),
        (None, None),
        ("abc", None),
        ("", None),
        # Decimal-separator robustness.
        ("1,234.56", 1234.56),
        ("1.234.567,89", 1234567.89),
        ("0,15", 0.15),
        ("0.00", 0.0),
        # Symbols / whitespace / signs.
        ("%+0.20", 0.2),
        (" $42.50 ", 42.5),
        ("-5.5", -5.5),
        (" 42  ", 42.0),
        # Native numerics pass through.
        (42, 42.0),
        (3.14, 3.14),
        ("42", 42.0),
        # Garbage / non-numeric → None, no exception.
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("abc123", None),
        ("--", None),
        (True, None),  # bool is not a number
        (False, None),
    ],
)
def test_safe_float(raw, expected):
    result = safe_float(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)
        assert isinstance(result, float)


def test_safe_float_never_raises():
    for weird in (object(), [], {}, {"a": 1}, b"bytes", "\udcff"):
        assert safe_float(weird) is None or isinstance(safe_float(weird), float)


def test_safe_float_finite():
    result = safe_float("47.8424")
    assert result is not None
    assert math.isfinite(result)