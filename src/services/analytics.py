import asyncio
import json
import logging

from src.core.database import db

logger = logging.getLogger(__name__)

# Arka plan task'larini canli tut: referanssiz task'lar GC tarafindan
# silinebilir (task kaybi). done_callback ile set'ten cikar + hata loglar.
_tasks: set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task failed: %s", exc)


async def track_event(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    """Analitik olayini kaydeder. Hata durumunda sessizce gecerr (yalnizca log)."""
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("""
                INSERT INTO analytics_events (event_type, user_id, ticker, details)
                VALUES (%s, %s, %s, %s)
            """, (event_type, user_id, ticker, json.dumps(details or {})))
            await db.commit()
    except Exception as e:
        logger.warning("Analytics track_event failed: %s", e)


# --- Tek-yazici kuyrugu -----------------------------------------------------
# Gerekce: her istek sonundaki fire_and_forget, request baglantisindan
# BAGIMSIZ ikinci bir havuz baglantisi aciyordu (asagidaki Context notuna
# bak). Dashboard acilisi ~15 paralel istek demek: ~30 eszamanli slot
# talebi, 10'luk havuzda aninda PoolTimeout -> 503 dalgasi (2026-09-15).
# Artik olaylar bellek-ici kuyruga birakilir; TEK bir yazici gorev
# toplu INSERT ile yazar (pik tuketim: 1 baglanti). Analitik kritik
# olmadigi icin tasma/kapanis durumunda olay duser (sessiz kayip) --
# daha once de havuz basinca ayni kayip oluyordu (warning ile).
_QUEUE_MAXSIZE = 1000
_BATCH_MAX = 200

_queue: asyncio.Queue | None = None
_writer_task: asyncio.Task | None = None
_writer_loop: asyncio.AbstractEventLoop | None = None
_dropped = 0


async def _write_batch(batch: list) -> None:
    """Tek baglanti ile toplu yazim (yazici gorev disinda cagrilmaz)."""
    async with db.cursor(row_factory=None) as cur:
        for ev in batch:
            await cur.execute("""
                INSERT INTO analytics_events (event_type, user_id, ticker, details)
                VALUES (%s, %s, %s, %s)
            """, (ev["event_type"], ev["user_id"], ev["ticker"], json.dumps(ev["details"] or {})))
        await db.commit()


async def _writer() -> None:
    """Kuyrugu tuketen tek yazici: toplu alir, tek baglantiyla yazar."""
    assert _queue is not None
    while True:
        try:
            first = await _queue.get()
        except asyncio.CancelledError:
            break
        batch = [first]
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        try:
            # Kapanis iptali gelse bile yarim batch yazilsin (shield), sonra cik.
            await asyncio.shield(_write_batch(batch))
        except asyncio.CancelledError:
            for _ in batch:
                _queue.task_done()
            break
        except Exception as e:
            logger.warning("Analytics batch write failed, dropping %d events: %s", len(batch), e)
        for _ in batch:
            _queue.task_done()


def _ensure_writer(loop: asyncio.AbstractEventLoop) -> None:
    """Yazici gorev bu loop'ta canli degilse baslat (tembel, idempotent)."""
    global _queue, _writer_task, _writer_loop
    if _queue is None:
        _queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    if _writer_task is None or _writer_task.done() or _writer_loop is not loop:
        _writer_loop = loop
        _writer_task = loop.create_task(_writer())
        _tasks.add(_writer_task)
        _writer_task.add_done_callback(_on_task_done)


async def aclose() -> None:
    """Kapanis: yazici gorevi durdur (kuyruktaki olaylar duser)."""
    global _writer_task, _writer_loop
    task = _writer_task
    _writer_task = None
    _writer_loop = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def fire_and_forget(event_type: str, user_id: int | None = None, ticker: str | None = None, details: dict | None = None):
    """Olayi arka plan kuyruguna birakir (senkron, bloklamaz).

    Eski davranis istek basina bir task + bir havuz baglantisi aciyordu;
    yeni davranis sadece kuyruga ekler -- havuz basinca duser, hicbir
    zaman istek yolunu yavaslatmaz veya 503 uretmez.
    """
    global _dropped
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as e:
        # Calisan event loop yok (ornegin shutdown sirasinda): sessiz yutma,
        # logla.
        logger.warning("fire_and_forget skipped (no running event loop): %s", e)
        return
    # BOS context notu (tarihsel): eski task-tabanli surum, request'in
    # ContextVar baglantisini miras almasin diye ayri Context aciyordu.
    # Kuyruk surumunde task basina baglanti yok; yazici kendi baglantisini
    # toplu yazim icin kisa sureligine alir, birakir.
    _ensure_writer(loop)
    assert _queue is not None
    try:
        _queue.put_nowait({
            "event_type": event_type,
            "user_id": user_id,
            "ticker": ticker,
            "details": details or {},
        })
    except asyncio.QueueFull:
        _dropped += 1
        # Log spam'i onle: her 100. dususte bir uyar.
        if _dropped % 100 == 1:
            logger.warning("Analytics queue full, dropping events (dropped=%d)", _dropped)
