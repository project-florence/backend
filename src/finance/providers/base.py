"""BaseProvider: circuit breaker + shared quote/parse helpers.

Circuit semantics (design spec 3.4 / 6.3): ``circuit_threshold`` consecutive
failures open the circuit for ``circuit_open_s`` seconds (both from the
``finance`` config section). After the cooldown the provider is half-open —
one more failure re-opens it; a success resets everything.

Every ``fetch_quotes`` implementation follows the same contract: never raise;
record the outcome into the circuit and return a partial dict.
"""

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.core.config import get_config
from src.finance.models import ProviderName, ProviderStatus, Quote, QuoteKind
from src.finance.symbols import SYMBOL_REGISTRY


@dataclass
class CircuitState:
    """In-memory provider health (persisted to rate_provider_status after each refresh)."""

    consecutive_failures: int = 0
    open_until: float | None = None       # time.monotonic() deadline while open
    last_success: datetime | None = None
    last_error: datetime | None = None
    last_error_msg: str | None = None


def safe_float(value) -> float | None:
    """Parse a numeric string with either '.' or ',' as decimal separator.

    Handles both "47.8424" and "1.234,56" (thousands separators stripped).
    Returns None for anything unparseable — never raises.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return None if math.isnan(result) or math.isinf(result) else result
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("%", "").replace("$", "").replace("€", "").replace("£", "")
    last_comma = text.rfind(",")
    last_dot = text.rfind(".")
    if last_comma > last_dot:
        # Comma is the decimal separator ("1.234,56") -> strip dots, swap comma.
        text = text.replace(".", "").replace(",", ".")
    elif last_dot > last_comma:
        # Dot is the decimal separator ("1,234.56") -> strip commas.
        text = text.replace(",", "")
    else:
        text = text.replace(",", "")
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


def make_quote(
    symbol: str,
    *,
    buying: float | None = None,
    selling: float | None = None,
    price: float | None = None,
    ts: datetime | None = None,
    source: ProviderName,
    extra: dict | None = None,
) -> Quote:
    """Build a Quote for a canonical symbol, filling currency/unit/kind from the registry."""
    d = SYMBOL_REGISTRY.get(symbol)
    currency = d.currency if d is not None else "TRY"
    unit = d.unit if d is not None else "1"
    if d is not None and d.quote_kind is QuoteKind.PRICE:
        # Single-price quotes carry the value in both ``price`` and ``buying``.
        if price is None and buying is not None:
            price = buying
        elif buying is None and price is not None:
            buying = price
    return Quote(
        symbol=symbol,
        buying=buying,
        selling=selling,
        price=price,
        currency=currency,
        unit=unit,
        source=source,
        ts=ts or datetime.now(timezone.utc),
        extra=extra or {},
    )


class RateLimiter:
    """Thread-safe minimum-delay gate shared by sync-library providers (yfinance).

    Reuses the existing ``price_history.rate_limit_delay`` knob (1.5 s).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        try:
            delay = float(get_config()["price_history"]["rate_limit_delay"])
        except Exception:
            delay = 1.5
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_request = time.monotonic()


# One global rate budget for every yfinance-backed provider.
yfinance_rate_limiter = RateLimiter()


class BaseProvider(ABC):
    """Base class implementing the circuit breaker and status reporting."""

    name: ProviderName
    provides: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self._circuit = CircuitState()

    @abstractmethod
    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        """Fetch quotes for the requested canonical symbols (contract above)."""

    async def fetch_candles(
        self,
        symbol: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list:
        raise NotImplementedError(f"{self.name} has no candle history")

    # --- circuit breaker ---------------------------------------------------
    def record_success(self) -> None:
        self._circuit.consecutive_failures = 0
        self._circuit.open_until = None
        self._circuit.last_success = datetime.now(timezone.utc)
        self._circuit.last_error_msg = None

    def record_failure(self, exc: Exception) -> None:
        cfg = get_config()["finance"]
        now = datetime.now(timezone.utc)
        self._circuit.consecutive_failures += 1
        self._circuit.last_error = now
        self._circuit.last_error_msg = f"{type(exc).__name__}: {exc}"[:500]
        if self._circuit.consecutive_failures >= int(cfg["circuit_threshold"]):
            self._circuit.open_until = time.monotonic() + float(cfg["circuit_open_s"])

    def restore_from_status(self, status) -> None:
        """Rehydrate the in-memory circuit from persisted provider health.

        Called at startup so a restarted process honors an open circuit whose
        cooldown window has not yet elapsed (no more blind full tries). If the
        cooldown already elapsed, the provider is left half-open (probes allowed).
        """
        try:
            cfg = get_config()["finance"]
            open_s = float(cfg["circuit_open_s"])
        except Exception:
            open_s = 600.0
        self._circuit.consecutive_failures = getattr(status, "consecutive_failures", 0) or 0
        self._circuit.last_success = getattr(status, "last_success", None)
        self._circuit.last_error = getattr(status, "last_error", None)
        self._circuit.last_error_msg = getattr(status, "last_error_msg", None)
        open = bool(getattr(status, "circuit_open", False)) and self._circuit.last_error is not None
        if open:
            remaining = (
                self._circuit.last_error + timedelta(seconds=open_s) - datetime.now(timezone.utc)
            ).total_seconds()
            if remaining > 0:
                self._circuit.open_until = time.monotonic() + remaining
            # else: cooldown elapsed -> leave open_until None (half-open probe).

    @property
    def is_available(self) -> bool:
        """False while the circuit is open; True otherwise (half-open probes allowed)."""
        open_until = self._circuit.open_until
        if open_until is None:
            return True
        return time.monotonic() >= open_until

    def status(self) -> ProviderStatus:
        open_until = self._circuit.open_until
        is_open = open_until is not None and time.monotonic() < open_until
        return ProviderStatus(
            provider=self.name,
            last_success=self._circuit.last_success,
            last_error=self._circuit.last_error,
            consecutive_failures=self._circuit.consecutive_failures,
            circuit_open=is_open,
            last_error_msg=self._circuit.last_error_msg,
        )