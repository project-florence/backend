"""Olu ticker bastirma (dead-ticker suppression).

BIST evrenindeki ticker'lar (`tickers` tablosu) hicbir zaman silinmez (bkz.
`src/services/bist.py::_fetch_and_persist_all` — sadece INSERT/UPDATE yapar).
Upstream'de (yfinance) artik bulunmayan semboller icin toplu tazeleme
dongusu (`src/cron/tasks.py`) her turda ayni basarisiz istegi tekrarlar; bu
da hem upstream rate limit'ini bosa harcar hem de gercek hatalari log
gurultusune gomer.

Bu modul `rate_provider_status` (bkz. src/finance/providers/base.py,
migrations/009) ile ayni devre kesici (circuit breaker) desenini ticker
bazinda uygular: ardisik `failure_threshold` basarisizliktan sonra ticker
`suppressed_until` suresi boyunca bastirilir. Sure dolunca ozel bir "prob"
mekanizmasi gerekmez — bir sonraki cron turu ticker'i normal akisinda
tekrar dener (basarili olursa sayac sifirlanir, basarisiz olursa yeni bir
bastirma penceresi acilir).

Iki basarisizlik turu ayirt edilir:
- "not_found": upstream'in sembolu tanimadigina dair guclu kanit (bos
  quote/profile, "possibly delisted", 404 vb.) -> uzun bekleme.
- "transient": ag/istek hatasi gibi zayif kanit -> kisa bekleme, ayni gun
  icinde tekrar denenir.

Fail-safe: DB erisilemezse `get_suppressed_tickers()` bos kume doner (hicbir
sey bastirilmaz, mevcut davranis = hepsini dene). `record_success` /
`record_failure` DB hatasinda sessizce loglar, cron turunu patlatmaz.

NOT: Bu bastirma sadece toplu/arka plan tazeleme donguleri icindir (bkz.
cron/tasks.py cagri noktalari). Kullanicinin acikca istedigi tekil
fiyat/quote/history istekleri (src/services/price.py, src/api/bist.py,
src/services/portfolio.py, src/services/simulations.py) bu modulu hic
gormez ve her zaman upstream'i dener — bastirilmis olsa bile.
"""

import logging
from datetime import datetime, timedelta, timezone

from src.core.config import get_config
from src.core.database import db

logger = logging.getLogger(__name__)

NOT_FOUND = "not_found"
TRANSIENT = "transient"

# Upstream'in "sembol yok" dedigini gosteren ifadeler (mesaj tabanli, ucuz
# siniflandirma — yfinance batch/`raise_errors=False` yolunda tipik hata
# turu yakalanamadigi icin string eslesmesi kullanilir).
_NOT_FOUND_HINTS = (
    "possibly delisted",
    "no price data found",
    "not found",
    "404",
    "no data found",
)


def _cfg() -> dict:
    return get_config()["ticker_health"]


def classify_error(error: BaseException | str | None) -> str:
    """Bir hatayi 'not_found' / 'transient' olarak siniflandirir (ucuz, mesaj tabanli)."""
    if error is None:
        return TRANSIENT
    text = str(error).lower()
    if any(hint in text for hint in _NOT_FOUND_HINTS):
        return NOT_FOUND
    return TRANSIENT


def _cooldown_for(kind: str) -> timedelta:
    cfg = _cfg()
    seconds = cfg["not_found_cooldown_s"] if kind == NOT_FOUND else cfg["transient_cooldown_s"]
    return timedelta(seconds=seconds)


def _compute_suppression(consecutive_failures: int, kind: str, now: datetime) -> datetime | None:
    """Ardisik basarisizlik sayisina gore yeni `suppressed_until` degerini hesaplar.

    Esik altindaysa None (bastirma yok) doner — saf fonksiyon, DB gerektirmez.
    """
    if consecutive_failures < _cfg()["failure_threshold"]:
        return None
    return now + _cooldown_for(kind)


async def record_success(ticker: str) -> None:
    """Basarili fetch: sayaci sifirla, bastirmayi kaldir."""
    ticker = ticker.upper()
    now = datetime.now(timezone.utc)
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "INSERT INTO ticker_health "
                "(ticker, consecutive_failures, last_attempt_at, last_success_at, "
                " last_failure_kind, last_error, suppressed_until, updated_at) "
                "VALUES (%s, 0, %s, %s, NULL, NULL, NULL, %s) "
                "ON CONFLICT (ticker) DO UPDATE SET "
                "consecutive_failures = 0, last_attempt_at = EXCLUDED.last_attempt_at, "
                "last_success_at = EXCLUDED.last_success_at, last_failure_kind = NULL, "
                "last_error = NULL, suppressed_until = NULL, updated_at = EXCLUDED.updated_at",
                (ticker, now, now, now),
            )
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("ticker_health: basari kaydi yazilamadi (%s)", ticker, exc_info=True)


async def record_failure(ticker: str, kind: str, error: str | None = None) -> None:
    """Basarisiz fetch: sayaci artir, esigi asarsa bastir."""
    ticker = ticker.upper()
    now = datetime.now(timezone.utc)
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT consecutive_failures FROM ticker_health WHERE ticker = %s", (ticker,)
            )
            row = await cur.fetchone()
            current = (row[0] if row else 0) or 0
            new_count = current + 1
            suppressed_until = _compute_suppression(new_count, kind, now)

            await cur.execute(
                "INSERT INTO ticker_health "
                "(ticker, consecutive_failures, last_attempt_at, last_failure_kind, "
                " last_error, suppressed_until, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (ticker) DO UPDATE SET "
                "consecutive_failures = EXCLUDED.consecutive_failures, "
                "last_attempt_at = EXCLUDED.last_attempt_at, "
                "last_failure_kind = EXCLUDED.last_failure_kind, "
                "last_error = EXCLUDED.last_error, "
                "suppressed_until = EXCLUDED.suppressed_until, "
                "updated_at = EXCLUDED.updated_at",
                (ticker, new_count, now, kind, (error or "")[:500], suppressed_until, now),
            )
            await db.commit()
    except Exception:
        await db.rollback()
        logger.warning("ticker_health: basarisizlik kaydi yazilamadi (%s)", ticker, exc_info=True)


async def get_suppressed_tickers() -> set[str]:
    """Su an bastirma penceresi aktif olan ticker'larin kumesini dondurur.

    DB erisilemezse (fail-safe) bos kume doner — hicbir ticker bastirilmaz,
    yani cagiran taraf mevcut (bastirmasiz) davranisa duser.
    """
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT ticker, suppressed_until FROM ticker_health "
                "WHERE suppressed_until IS NOT NULL"
            )
            rows = await cur.fetchall()
    except Exception:
        logger.warning("ticker_health: bastirilan ticker listesi okunamadi, hicbiri bastirilmiyor", exc_info=True)
        return set()

    now = datetime.now(timezone.utc)
    suppressed = set()
    for ticker, suppressed_until in rows:
        if suppressed_until is not None and suppressed_until > now:
            suppressed.add(ticker)
    return suppressed


async def filter_suppressed(tickers: list[str]) -> list[str]:
    """Verilen ticker listesinden su an bastirilmis olanlari cikarir."""
    if not tickers:
        return tickers
    suppressed = await get_suppressed_tickers()
    if not suppressed:
        return tickers
    return [t for t in tickers if t.upper() not in suppressed]
