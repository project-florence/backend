"""Gercek Postgres + Redis'e karsi calisan gunluk-mum/quote entegrasyon katmani.

``tests/test_llm_integration.py`` ile AYNI opt-in desen (kendi kendine yeterli
fixture'lar, prod'a karsi guvenlik agi, benzersiz sahte ticker'lar, idempotent
temizlik). Hermetik ``fake_db``/``fake_redis`` seridi bu davranislari YAPISAL
olarak goremez:

    1. ``price_candles``'in ``PRIMARY KEY (ticker, interval, ts)`` kisiti ve
       ``ON CONFLICT ... DO UPDATE`` yolu yalnizca gercek Postgres'te uygulanir
       -- ``fake_db`` kisit/conflict uygulamaz, dolayisiyla kanonik yazicinin
       gercekten upsert ettigini (mukerrer satir birakmadigini) goremez.
    2. Ayni seans icin IKI yazim konvansiyonu (kanonik ``session_ts`` = Istanbul
       gece yarisi, onceki UTC gunu 21:00Z; eski/legacy = ham UTC gece yarisi
       00:00Z) ayni tarihe dusen iki AYRI TIMESTAMPTZ satiridir. Dedup
       mantiginin (``_completed_daily``) kanonik damgayi tercih etmesi, gercek
       ``TIMESTAMPTZ`` saklama/okuma ve ``ORDER BY ts DESC`` davranisina baglidir.
    3. Halt edilmis sembolun duz/hacimsiz "placeholder" mumunun kalici
       OLMAMASI (``is_placeholder_candle``) gercek yazma yolunun
       (``ensure_recent_daily_candle`` -> ``_build_candle_rows`` ->
       ``_write_candle_rows``) filtresidir; fake_db bu yolu kisit gibi
       dogrulamaz.
    4. Gercek Redis uzerindeki ``refresh_lock:{ticker}:1d`` kilidi ve TTL'i.

Bu dosya (2)'nin ve (3)'un REGRESYON testlerini icerir: kanonik seans
satirinin legacy gece yarisi satirini yenmesi, ``ensure_recent_daily_candle``'in
kanonik damgayla TEK satir yazmasi ve placeholder mumun DB'ye hic
dusmemesi.

Calistirma
----------
Varsayilan ``python -m pytest`` bu dosyayi CALISTIRMAZ
(``pyproject.toml``: ``addopts = -m "not integration"``). Acikca::

    cd backend && source .venv/bin/activate
    docker compose up -d postgres redis
    python -m pytest -m integration -q

Container'lar kapaliysa testler HATA vermez; ``_integration_target`` kisa
timeout'lu bir prob ile baglanamayinca ``pytest.skip`` eder.

Hedef / guvenlik agi
--------------------
Hedef host/port'lar uygulamanin zaten okudugu ``POSTGRES_*``/``REDIS_*``
degiskenleridir (yerel dev: Postgres localhost:5433, Redis localhost:5434).
PROD'A ASLA BAGLANILMAMASI icin ``_guard_not_local`` host yerel degilse
testleri sessizce skip eder (bilerek uzak hedef icin
``FLORENCE_INTEGRATION_ALLOW_REMOTE=1`` gerekir).

Temizlik
--------
Her test benzersiz bir sahte ticker kullanir
(``ITEST<rastgele>.IS``) ve olusturdugu satirlari ``_cleanup`` fixture'i
uzerinden teardown'da ``DELETE FROM price_candles WHERE ticker = ANY(%s)``
ile siler -- gercek sembollere DOKUNULMAZ. Ikinci kosuda da ayni sekilde
gecer (idempotent).

Yapilmayanlar
-------------
Gercek ag cagrisi YOK: yfinance yollari (``afetch_price_history`` /
``ensure_recent_daily_candle``) monkeypatch ile kesilir. Bu katman yalnizca
gercek Postgres/Redis davranisini dogrular.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import psycopg
import pytest
import redis.asyncio as aioredis

import src.services.price as price_module
import src.services.quote as quote_module
from src.core import database as database_module
from src.core import redis as redis_module
from src.services.market import MARKET_TIMEZONE, last_trading_day, session_date, session_ts

pytestmark = pytest.mark.integration

_CONNECT_TIMEOUT = 2.0


def _guard_not_local() -> str | None:
    """Prod'a asla baglanmama guvenlik agi -- bkz. dosya docstring'i."""
    if os.getenv("FLORENCE_INTEGRATION_ALLOW_REMOTE") == "1":
        return None
    for var in ("POSTGRES_HOST", "REDIS_HOST"):
        host = os.getenv(var, "")
        if host not in ("localhost", "127.0.0.1", ""):
            return (
                f"{var}={host!r} yerel gorunmuyor; entegrasyon testleri prod'a "
                "asla baglanmamali (guvenlik agi). Kasitliyse "
                "FLORENCE_INTEGRATION_ALLOW_REMOTE=1 ile gecersiz kil."
            )
    return None


@pytest.fixture(scope="module")
def _integration_target() -> None:
    """Container'lara baglanabiliyor muyuz? Yoksa TUM modul skip (hata degil).

    Bagimsiz, kisa timeout'lu bir prob baglantisi kurar (uygulamanin gercek
    havuzunu/redis proxy'sini hic olusturmadan) -- basarisizsa ``pytest.skip``.
    """
    guard = _guard_not_local()
    if guard:
        pytest.skip(guard)

    async def _probe() -> str | None:
        try:
            conn = await psycopg.AsyncConnection.connect(
                database_module._conninfo(), connect_timeout=_CONNECT_TIMEOUT
            )
            await conn.close()
        except Exception as exc:
            return f"Postgres'e baglanilamadi (POSTGRES_PORT={os.getenv('POSTGRES_PORT')}): {exc}"
        try:
            client = aioredis.Redis(
                host=os.getenv("REDIS_HOST"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                password=os.getenv("REDIS_PASSWORD") or None,
                socket_connect_timeout=_CONNECT_TIMEOUT,
                socket_timeout=_CONNECT_TIMEOUT,
            )
            await client.ping()
            await client.aclose()
        except Exception as exc:
            return f"Redis'e baglanilamadi (REDIS_PORT={os.getenv('REDIS_PORT')}): {exc}"
        return None

    failure = asyncio.run(_probe())
    if failure:
        pytest.skip(f"entegrasyon container'lari ayakta degil, atlaniyor -- {failure}")


@pytest.fixture(autouse=True)
async def _reset_singletons(_integration_target):
    """Her testte TAZE havuz/redis baglantisi kurulmasini zorlar.

    ``db``/``r`` surec-omurlu singleton'lar; pytest-asyncio her test icin YENI
    bir event loop aciyor (``asyncio_default_fixture_loop_scope = "function"``).
    Bir onceki testte kurulan baglanti o testin loop'una bagli kalirsa bu testin
    loop'unda kullanilamaz ("attached to a different loop"). Testten SONRA
    kapatmak bir sonraki testi kendi loop'unda taze kurulum yapmaya zorlar.
    """
    yield
    await database_module.db.close()
    conn = redis_module.r._conn
    if conn is not None:
        try:
            await conn.aclose()
        except Exception:
            pass
        redis_module.r._conn = None
        redis_module.r._disabled = False


@pytest.fixture
async def _cleanup():
    """Test-basina temizlik kaydi: sahte ticker'lari teardown'da sil.

    Test'ler ``_cleanup.append(fake_ticker)`` yapar (``.IS`` son ekli tam
    kod). Teardown best-effort: silme patlarsa test sonucu bozulmaz ama
    dev DB'de kalinti kalmamasi icin ticker'lar her kosuda benzersizdir.
    """
    tickers: list[str] = []
    yield tickers
    if not tickers:
        return
    try:
        async with database_module.db.cursor(row_factory=None) as cur:
            await cur.execute(
                "DELETE FROM price_candles WHERE ticker = ANY(%s)", (tickers,)
            )
            await database_module.db.commit()
    except Exception:
        pass


def _fake_base() -> str:
    """Her testte benzersiz sahte taban kod (``.IS``'siz)."""
    return f"ITEST{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Kanonik seans damgasi vs legacy UTC gece yarisi satiri
# ---------------------------------------------------------------------------


async def test_canonical_session_preferred_over_legacy_midnight_row(monkeypatch, _cleanup):
    fake_base = _fake_base()
    fake_ticker = f"{fake_base}.IS"
    _cleanup.append(fake_ticker)

    # Sabit tarih yerine bugune gore hesapla: get_quotes gunluk sorgusu son 45
    # gunle sinirli, bu yuzden S pencerenin ICINDE ama bugunden once olmali.
    today = datetime.now(MARKET_TIMEZONE).date()
    session = last_trading_day(today - timedelta(days=10))
    prev_session = last_trading_day(session)

    # Ayni seans icin iki yazim konvansiyonu: legacy ham UTC gece yarisi
    # (00:00Z, daha YENI ts) ve kanonik Istanbul gece yarisi (onceki gun 21:00Z).
    legacy_ts = datetime(session.year, session.month, session.day, tzinfo=timezone.utc)

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.executemany(
            "INSERT INTO price_candles (ticker, interval, ts, open, high, low, close, volume) "
            "VALUES (%s, '1d', %s, %s, %s, %s, %s, %s)",
            [
                (fake_ticker, session_ts(prev_session), 90.0, 90.0, 90.0, 90.0, 1000),
                (fake_ticker, legacy_ts, 999.0, 999.0, 999.0, 999.0, 1000),
                (fake_ticker, session_ts(session), 100.0, 100.0, 100.0, 100.0, 1000),
            ],
        )
        await database_module.db.commit()

    # Ag yok: telafi cekimi no-op, piyasa kapali sabitlenir.
    async def _no_ensure(ticker: str) -> bool:
        return False

    monkeypatch.setattr(price_module, "ensure_recent_daily_candle", _no_ensure)
    monkeypatch.setattr(quote_module, "get_market_status", lambda: "closed")

    quotes = await quote_module.get_quotes([fake_base])
    quote = quotes[fake_base]

    # Kanonik damgali satir (close 100) legacy satiri (close 999) yenmeli.
    assert quote["price"] == 100.0
    assert quote["previous_close"] == 90.0


# ---------------------------------------------------------------------------
# ensure_recent_daily_candle: gercek yazma yolu kanonik damgayla TEK satir
# ---------------------------------------------------------------------------


async def test_ensure_recent_daily_candle_persists_canonical_row(monkeypatch, _cleanup):
    fake_base = _fake_base()
    fake_ticker = f"{fake_base}.IS"
    _cleanup.append(fake_ticker)

    calendar_day = datetime.now(MARKET_TIMEZONE).date()
    frame = pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.5],
            "Low": [9.5],
            "Close": [11.0],
            "Volume": [12345],
        },
        index=pd.DatetimeIndex([pd.Timestamp(calendar_day)]),
    )

    async def _fake_history(ticker, interval, start, end):
        return frame

    monkeypatch.setattr(price_module, "afetch_price_history", _fake_history)

    ok = await price_module.ensure_recent_daily_candle(fake_base)
    assert ok is True

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT ts, open, high, low, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = '1d'",
            (fake_ticker,),
        )
        rows = await cur.fetchall()

    assert len(rows) == 1
    ts, open_, high, low, close, volume = rows[0]
    # tz-naive gunluk index -> kanonik Istanbul gece yarisi damgasi.
    assert ts == session_ts(calendar_day)
    assert session_date(ts) == calendar_day
    assert (open_, high, low, close) == (10.0, 11.5, 9.5, 11.0)
    assert volume == 12345


# ---------------------------------------------------------------------------
# Placeholder (halt edilmis sembol) mumu DB'ye yazilmamali
# ---------------------------------------------------------------------------


async def test_placeholder_candle_is_not_persisted(monkeypatch, _cleanup):
    fake_base = _fake_base()
    fake_ticker = f"{fake_base}.IS"
    _cleanup.append(fake_ticker)

    today = datetime.now(MARKET_TIMEZONE).date()
    normal_day = last_trading_day(today)
    placeholder_day = last_trading_day(last_trading_day(normal_day))

    # (a) normal mum (volume > 0), (b) FARKLI seansta placeholder: hacim 0 ve
    # O=H=L=C (yfinance'in halt edilmis sembolde urettigi duz bar).
    frame = pd.DataFrame(
        {
            "Open": [20.0, 20.0],
            "High": [21.0, 20.0],
            "Low": [19.0, 20.0],
            "Close": [20.5, 20.0],
            "Volume": [5000, 0],
        },
        index=pd.DatetimeIndex(
            [pd.Timestamp(normal_day), pd.Timestamp(placeholder_day)]
        ),
    )

    async def _fake_history(ticker, interval, start, end):
        return frame

    monkeypatch.setattr(price_module, "afetch_price_history", _fake_history)

    ok = await price_module.ensure_recent_daily_candle(fake_base)
    assert ok is True

    async with database_module.db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT ts, close, volume FROM price_candles "
            "WHERE ticker = %s AND interval = '1d' ORDER BY ts",
            (fake_ticker,),
        )
        rows = await cur.fetchall()

    # Placeholder elendi -> yalnizca normal satir kalir.
    assert len(rows) == 1
    ts, close, volume = rows[0]
    assert session_date(ts) == normal_day
    assert close == 20.5
    assert volume == 5000
