"""Hermetik testler: analytics tek-yazici kuyrugu (``src/services/analytics.py``).

2026-09-15 503 dalgasinin yapisal nedenlerinden biri: middleware'deki her
``fire_and_forget`` cagrisi, istek baglantisindan BAGIMSIZ ikinci bir havuz
baglantisi aciyordu (~15 paralel istek -> ~30 slot talebi, 10'luk havuzda
aninda tukenme). Yeni tasarimda ``fire_and_forget`` sadece bellek-ici
kuyruga ekler; tek yazici gorev toplu INSERT ile yazar (pik: 1 baglanti).
"""

import asyncio

from src.services import analytics as analytics_module


async def _drain_queue_isolated():
    """Onceki testten kalmis olabilecek kuyruk/yazici durumunu sifirla."""
    await analytics_module.aclose()
    q = analytics_module._queue
    if q is not None:
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                break


async def test_fire_and_forget_batches_through_single_writer(fake_db):
    """Kuyruga birakilan olaylar tek yazici tarafindan topluca yazilir."""
    await _drain_queue_isolated()

    analytics_module.fire_and_forget("ev1", user_id=1, details={"a": 1})
    analytics_module.fire_and_forget("ev2", user_id=2, ticker="THYAO")
    analytics_module.fire_and_forget("ev3", user_id=None)

    assert analytics_module._queue is not None
    await asyncio.wait_for(analytics_module._queue.join(), timeout=5)

    inserts = [q for q in fake_db.queries if "INSERT INTO analytics_events" in q[0]]
    assert len(inserts) == 3
    assert fake_db.commit_calls >= 1

    await analytics_module.aclose()


async def test_queue_full_drops_silently(monkeypatch):
    """Kuyruk tasinca olay duser, istek yolu asla bloklanmaz/hata vermez."""
    # Yaziciyi devre disi birak ki bosaltma olmasin (deterministik tasma).
    monkeypatch.setattr(analytics_module, "_ensure_writer", lambda loop: None)
    monkeypatch.setattr(analytics_module, "_queue", asyncio.Queue(maxsize=2))
    before = analytics_module._dropped

    for i in range(5):
        analytics_module.fire_and_forget(f"ev{i}", user_id=i)

    assert analytics_module._queue.qsize() == 2
    assert analytics_module._dropped - before == 3


def test_fire_and_forget_without_loop_drops_silently():
    """Calisan loop yoksa (senkron baglam) sessizce vazgecilir, hata yok."""
    analytics_module.fire_and_forget("ev-no-loop", user_id=1)
