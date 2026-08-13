"""Yillik gunluk (1d) veri rotasi: CSV/JSON indirme + on-demand fill.

``GET /api/v1/data/daily/{year}`` — auth zorunlu, kredi harcatmaz. Rate limit:
kullanici basina 10 istek / 60 sn (asilirsa 429).

Yil icin DB'de hic/az veri varsa arka planda on-demand fill baslatilir:
bist_companies.json'daki tum ticker'lar 50'lik batch'lerle yfinance'tan
(``src.clients.yfinance`` — services/price.py'nin kullandigi istemci)
``start=year-01-01`` .. ``end=year+1-01-01`` arasi 1d history cekilir ve
``price_candles``'a upsert edilir (price.py:107-121 deseni). Yanit mevcut
veriyle doner; fill gecikmeli tamamlanir (sonraki isteklerde dolu gorunur).

Ayni yil icin eszamanli ikinci fill baslatilmaz: Redis'te
``data-fill:{year}`` NX 60s lock + surec ici ``_active_fills`` guard'i.
"""

import asyncio
import json
import logging
import zlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.api.deps import get_current_user
from src.core.database import db
from src.core.ratelimit import rate_limiter
from src.core.redis import r
from src.services.price import _build_candle_rows, _write_candle_rows

logger = logging.getLogger(__name__)

router = APIRouter()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
BATCH_SIZE = 50
BATCH_DELAY = 5
# Yil icin bu kadar az 1d mum varsa yil "bos" sayilir ve fill tetiklenir.
FILL_MIN_ROWS = 10
FILL_LOCK_TTL = 60

# Arka plan fill task'lari: referanssiz task'lar GC tarafindan silinebilir;
# set'te tutulur, done_callback ile cikarilir + hata loglanir (analytics.py
# deseni).
_fill_tasks: set[asyncio.Task] = set()
_active_fills: set[int] = set()

CSV_HEADER = "ticker,date,open,high,low,close,volume"


def _on_fill_done(task: asyncio.Task) -> None:
    _fill_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Yillik veri fill gorevi hata verdi: %s", exc)


def _csv_val(v) -> str:
    return "" if v is None else str(v)


def _load_tickers() -> list[str]:
    """data/bist_companies.json'daki ticker kodlari (sirali)."""
    path = DATA_DIR / "bist_companies.json"
    if not path.exists():
        logger.warning("bist_companies.json bulunamadi: %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("bist_companies.json okunamadi: %s", e)
        return []
    if isinstance(data, dict):
        return sorted(data.keys())
    return []


async def _fill_year(year: int) -> None:
    """Yilin 1d verisini yfinance'tan cekip price_candles'a upsert eder.

    50'lik batch'lerde once tum ticker'larin verisi cekilir (ag beklemesi
    sirasinda hic baglanti tutulmaz), sonra TEK executemany + TEK commit ile
    yazilir: 613 commit yerine ~13 commit. ``price_write_lock`` korunur.
    """
    try:
        tickers = await asyncio.to_thread(_load_tickers)
        if not tickers:
            return
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        total = len(tickers)
        logger.info("data-fill %s basladi (%s ticker)", year, total)

        for i in range(0, total, BATCH_SIZE):
            batch = [f"{t}.IS" for t in tickers[i:i + BATCH_SIZE]]
            values: list[tuple] = []
            for ticker_is in batch:
                try:
                    values.extend(
                        await _build_candle_rows(ticker_is, "1d", start, end)
                    )
                except Exception as e:
                    # Delisted/bos ticker'lar atlanir — sorun degil.
                    logger.warning("data-fill %s: %s atlandi: %s", year, ticker_is, e)
            try:
                await _write_candle_rows(values)
            except Exception as e:
                # Batch yazim hatasi fill'i oldurmesin; sonraki batch'ler devam
                # etsin (eski davranista her ticker ayri ayri yakalaniyordu).
                logger.warning("data-fill %s: batch yazilamadi: %s", year, e)
            if i + BATCH_SIZE < total:
                await asyncio.sleep(BATCH_DELAY)

        logger.info("data-fill %s tamamlandi", year)
    finally:
        _active_fills.discard(year)


async def _maybe_start_fill(year: int) -> None:
    """Yil icin on-demand fill baslatir (zaten calisiyorsa yeniden baslatmaz)."""
    if year in _active_fills:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Redis NX lock: farkli surec/worker'lar da ayni yili iki kez doldurmasin.
    # Redis yoksa (cache'siz mod) None doner -> fill baslatilmaz; _active_fills
    # guard'i ayni surec icinde tekrari onler.
    acquired = await r.set(f"data-fill:{year}", "1", ex=FILL_LOCK_TTL, nx=True)
    if acquired is None:
        return
    _active_fills.add(year)
    task = asyncio.create_task(_fill_year(year))
    _fill_tasks.add(task)
    task.add_done_callback(_on_fill_done)


async def _stream_csv(start: datetime, end: datetime):
    """1d mumlarini satir satir CSV olarak uretir (fetchmany ile akar)."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute(
                "SELECT ticker, ts, open, high, low, close, volume FROM price_candles "
                "WHERE interval = '1d' AND ts >= %s AND ts < %s AND close IS NOT NULL "
                "ORDER BY ticker, ts",
                (start, end),
            )
            yield CSV_HEADER + "\n"
            while True:
                rows = await cur.fetchmany(5000)
                if not rows:
                    break
                lines = []
                for ticker, ts, o, h, l, c, v in rows:
                    lines.append(
                        f"{ticker},{ts:%Y-%m-%d},{_csv_val(o)},{_csv_val(h)},"
                        f"{_csv_val(l)},{_csv_val(c)},{_csv_val(v)}\n"
                    )
                yield "".join(lines)
    finally:
        # Streaming sirasinda tutulan baglantiyi havuza iade et.
        await db.release_current()


def _gzip_stream(async_gen):
    """Async generator'u gzip (zlib, wbits=31) ile paketleyerek akitir."""
    compressor = zlib.compressobj(level=6, wbits=16 + zlib.MAX_WBITS)

    async def _wrapped():
        async for chunk in async_gen:
            if chunk:
                compressed = compressor.compress(chunk)
                if compressed:
                    yield compressed
        tail = compressor.flush()
        if tail:
            yield tail

    return _wrapped()


@router.get("/data/daily/{year}")
async def daily_data(
    year: int,
    format: str = Query(default="csv"),
    current_user_id: int = Depends(get_current_user),
):
    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Use 'csv' or 'json'")

    now = datetime.now(timezone.utc)
    if year < 1990 or year > now.year + 1:
        raise HTTPException(status_code=400, detail="Invalid year")

    await rate_limiter.check(f"data-daily:{current_user_id}", max_requests=10, window_seconds=60)

    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM price_candles WHERE interval = '1d' AND ts >= %s AND ts < %s",
            (start, end),
        )
        row_count = (await cur.fetchone())[0]

    # create_task context mirasi: fill task'i bu istegin baglantisini miras
    # alip onunla calismasin — COUNT sonrasi baglanti havuza iade edilir.
    # (Sonraki _stream_csv / JSON SELECT kendi cursor'unu acar.)
    await db.release_current()

    # Yil icin hic/az veri varsa on-demand fill'i arka planda baslat; yanit
    # mevcut veriyle doner.
    if row_count < FILL_MIN_ROWS:
        await _maybe_start_fill(year)

    if format == "csv":
        return StreamingResponse(
            _gzip_stream(_stream_csv(start, end)),
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="florence-daily-{year}.csv.gz"'
            },
        )

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT ticker, ts, open, high, low, close, volume FROM price_candles "
            "WHERE interval = '1d' AND ts >= %s AND ts < %s AND close IS NOT NULL "
            "ORDER BY ticker, ts",
            (start, end),
        )
        rows = await cur.fetchall()

    result: dict[str, list[dict]] = {}
    for ticker, ts, o, h, l, c, v in rows:
        result.setdefault(ticker, []).append({
            "date": ts.strftime("%Y-%m-%d"),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
    return result
