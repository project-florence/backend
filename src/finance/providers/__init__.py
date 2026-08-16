"""Provider layer for the finance pipeline.

Defines the ``Provider`` protocol (design spec 3.4). Concrete implementations
live in sibling modules; ``registry.py`` builds and prioritizes them.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from src.finance.models import ProviderName, Quote, Candle


@runtime_checkable
class Provider(Protocol):
    """Contract every data provider must satisfy."""

    name: ProviderName
    # Canonical symbols this provider can serve directly (set by registry).
    provides: frozenset[str]

    async def fetch_quotes(self, symbols: set[str]) -> dict[str, Quote]:
        """Return the requested canonical symbols this provider can serve.

        Symbols it cannot serve are silently skipped. Never raises — failures
        are recorded into the circuit state and a partial/empty dict is
        returned so the orchestrator can continue down the fallback chain.
        """
        ...

    async def fetch_candles(
        self,
        symbol: str,
        interval: str = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Historical candles (yfinance-backed providers; most raise NotImplemented)."""
        ...