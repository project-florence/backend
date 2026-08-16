"""Pydantic models for the finance (FX & precious metals) data pipeline.

Mirrors the design spec (ANALYSIS/ekonomi-refactor-plani.md, section 3.3):
quotes, candles, analysis results and provider health are the wire format for
Redis cache values and DB JSONB payloads alike. All price fields are floats —
the backend never carries display formatting (the "0.00" bug class).
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AssetClass(str, Enum):
    FX = "fx"
    METAL = "metal"
    COMMODITY = "commodity"  # optional: brent/wti (macro)


class QuoteKind(str, Enum):
    BID_ASK = "bid_ask"  # bid/ask spread (currencies, jeweller quotes)
    PRICE = "price"      # single price (ounces, futures)


class ProviderName(str, Enum):
    GENELPARA = "genelpara"
    TCMB = "tcmb"
    YFINANCE_FX = "yfinance_fx"
    YFINANCE_METALS = "yfinance_metals"
    FRANKFURTER = "frankfurter"
    DB_SNAPSHOT = "db_snapshot"  # last known-good DB record (fallback)


class Quote(BaseModel):
    symbol: str                     # canonical symbol, e.g. "XAU-ONS"
    buying: float | None = None     # bid for bid_ask; also filled for PRICE
    selling: float | None = None    # ask for bid_ask
    price: float | None = None      # single price (ounce/futures)
    change_pct: float | None = None  # computed from our own close series (float!)
    change_text: str | None = None  # optional "%+1,23" display (frontend choice)
    currency: str = "TRY"           # currency of the price
    unit: str = "1"                 # "1" unit / "1 ounce" / "1 gram"
    source: ProviderName
    ts: datetime                    # source timestamp
    stale: bool = False             # True when served from a DB snapshot fallback
    extra: dict = Field(default_factory=dict)  # raw fields (yon, degisim, ...)


class QuoteBundle(BaseModel):
    ts: datetime                    # collection time (UTC)
    source: ProviderName | None = None  # winning source (first successful)
    quotes: dict[str, Quote]        # canonical symbol -> quote
    remaining: int | None = None    # GenelPara daily quota (monitoring)


class Candle(BaseModel):
    symbol: str
    interval: str                   # "1d" (1h later)
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    source: ProviderName | None = None


class AnalysisResult(BaseModel):
    symbol: str
    computed_at: datetime
    sma_5: float | None = None
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    price_vs_sma_20: float | None = None      # percent deviation
    rsi_14: float | None = None
    volatility_20d: float | None = None       # annualized daily-return std (%)
    change_daily_pct: float | None = None
    change_week_pct: float | None = None
    change_month_pct: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    all_time_high: float | None = None
    all_time_low: float | None = None
    rank_in_52w: float | None = None          # 0..1 (1 = 52w high)
    correlations: dict[str, float] = Field(default_factory=dict)  # {"XAG-ONS": 0.87}


class ProviderStatus(BaseModel):
    provider: ProviderName
    last_success: datetime | None = None
    last_error: datetime | None = None
    consecutive_failures: int = 0
    circuit_open: bool = False
    last_error_msg: str | None = None