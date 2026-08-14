"""Asyncio tabanli arka plan zamanlayicisi (CronClient).

Eski thread tabanli surumun yerine gecer: her vadesi gelen is, ana event loop
uzerinde bir ``asyncio.Task`` olarak baslatilir. Gorev kaynak kodu ya
``async def __cron_main__()`` tanimlar (yeni format) ya da eski duz sync
kod olabilir (uyumluluk icin thread'de calistirilir).

Koordinasyon Redis kilitleriyle yapilir; coklu worker'da ayni is tekrarlanmaz.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from types import CodeType

from pydantic import BaseModel

from src.core.database import db
from src.core.redis import r

logger = logging.getLogger(__name__)

PREFIX = "cron-"


class Job(BaseModel):
    """Kaydedilen bir cron isi. Redis'e degil, veritabanina kalici olarak yazilir."""

    name: str
    description: str = ""
    source: str
    interval_ms: int
    last_run: datetime | None = None

    @property
    def interval(self) -> float:
        return self.interval_ms / 1000.0


class CronClient:
    def __init__(self) -> None:
        self._code_dict: dict[str, CodeType] = {}
        self._jobs: dict[str, Job] = {}
        self._running: set[str] = set()
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._check_interval = 1.0
        self._fallback_locks: dict[str, float] = {}
        self._fallback_lock = asyncio.Lock()
        self._initialized = False

    # ------------------------------------------------------------------
    # Veritabani yardimcilar
    # ------------------------------------------------------------------
    async def _save_to_db(self, job: Job) -> None:
        async with db.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO cron_jobs (name, description, source, interval_ms, last_run)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    description = EXCLUDED.description,
                    source = EXCLUDED.source,
                    interval_ms = EXCLUDED.interval_ms
                """,
                (job.name, job.description, job.source, job.interval_ms, job.last_run),
            )
            # Commit blok icinde (blok cikisinda otomatik iade rollback etmesin).
            await db.commit()

    async def _load_from_db(self) -> list[Job]:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT name, description, source, interval_ms, last_run FROM cron_jobs")
            rows = await cur.fetchall()
        return [
            Job(name=row[0], description=row[1], source=row[2], interval_ms=row[3], last_run=row[4])
            for row in rows
        ]

    async def _delete_from_db(self, name: str) -> None:
        async with db.cursor() as cur:
            await cur.execute("DELETE FROM cron_jobs WHERE name = %s", (name,))
            # Commit blok icinde (otomatik iade rollback etmesin).
            await db.commit()

    async def _mark_done(self, name: str) -> None:
        now = datetime.now(UTC)
        async with db.cursor() as cur:
            await cur.execute("UPDATE cron_jobs SET last_run = %s WHERE name = %s", (now, name))
            # Commit blok icinde (otomatik iade rollback etmesin).
            await db.commit()
        job = self._jobs.get(name)
        if job:
            job.last_run = now

    # ------------------------------------------------------------------
    # Redis kilit (coklu worker koordinasyonu)
    # ------------------------------------------------------------------
    async def _claim(self, name: str, interval_ms: int) -> bool:
        """Isi atomik olarak sahiplenir. Kazanan worker True alir."""
        lock_key = f"{PREFIX}lock:{name}"
        ttl_seconds = max(1, int(interval_ms / 1000))
        acquired = await r.set(lock_key, "1", nx=True, ex=ttl_seconds)
        if acquired is not None:
            return bool(acquired)

        now = time.monotonic()
        async with self._fallback_lock:
            expires_at = self._fallback_locks.get(lock_key, 0.0)
            if expires_at > now:
                return False
            self._fallback_locks[lock_key] = now + ttl_seconds
            if len(self._fallback_locks) > 10_000:
                for stale_key, stale_expiry in list(self._fallback_locks.items()):
                    if stale_expiry <= now:
                        self._fallback_locks.pop(stale_key, None)
        return True

    def _is_due(self, job: Job) -> bool:
        if job.last_run is None:
            return True
        elapsed = (datetime.now(UTC) - job.last_run).total_seconds() * 1000
        return elapsed >= job.interval_ms

    # ------------------------------------------------------------------
    # Kod calistirma
    # ------------------------------------------------------------------
    def _compile(self, source: str, name: str) -> CodeType:
        return compile(source, f"<cron:{name}>", "exec")

    async def _run_code(self, code: CodeType, name: str) -> None:
        ns: dict = {"__cron_name__": name}
        exec(code, ns)
        main = ns.get("__cron_main__")
        if main is not None:
            await main()
        else:
            # Eski format: duz sync kaynak. Event loop'u bloklamamak icin
            # thread'de calistir (eski davranis).
            await asyncio.to_thread(exec, code, ns)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def register_job(
        self,
        name: str,
        interval_ms: int,
        code: str | Path,
        description: str = "",
        last_run: datetime | None = None,
    ) -> None:
        """Bir isi kaydeder: veritabanina yazar ve yerel sozluge derlenmis kodunu ekler."""
        source = code.read_text() if isinstance(code, Path) else code

        job = Job(name=name, description=description, source=source, interval_ms=interval_ms, last_run=last_run)
        async with self._lock:
            self._jobs[name] = job
            self._code_dict[name] = self._compile(source, name)
        await self._save_to_db(job)
        logger.info("Cron job '%s' kaydedildi (interval=%dms)", name, interval_ms)

    def get_job(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def list_jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.name)

    async def remove_job(self, name: str) -> None:
        async with self._lock:
            self._jobs.pop(name, None)
            self._code_dict.pop(name, None)
            self._running.discard(name)
        await self._delete_from_db(name)
        logger.info("Cron job '%s' kaldirildi", name)

    async def run_job(self, name: str) -> bool:
        async with self._lock:
            code = self._code_dict.get(name)
        if code is None:
            logger.warning("Cron job '%s' bulunamadi", name)
            return False
        logger.info("Cron job '%s' calistiriliyor", name)
        await self._run_code(code, name)
        return True

    async def _run_job_worker(self, name: str) -> None:
        try:
            await self.run_job(name)
        except Exception:
            logger.exception("Cron job '%s' calistirilirken hata", name)
        finally:
            try:
                await self._mark_done(name)
            except Exception:
                logger.exception("Cron job '%s' last_run guncellenemedi", name)
            finally:
                # Kritik: is ne yaparsa yapsin, task'a ait DB baglantisi havuzdan
                # cikarilmis olabilir; commit edilmemisse burada iade edilir.
                # (Aksi halde baglantilar havuza donmez -> havuz tukenir -> 30sn
                # PoolTimeout -> tum DB islemleri 500 verir.)
                await db.release_current()
                async with self._lock:
                    self._running.discard(name)

    async def run_due_jobs(self) -> None:
        for job in list(self._jobs.values()):
            if not self._is_due(job):
                continue
            if job.name in self._running:
                continue
            if not await self._claim(job.name, job.interval_ms):
                continue
            async with self._lock:
                self._running.add(job.name)
            asyncio.create_task(self._run_job_worker(job.name))

    async def init(self) -> None:
        """Kayitlari veritabanindan yukler ve derlenmis kodlari olusturur."""
        if self._initialized:
            return
        try:
            for job in await self._load_from_db():
                self._jobs[job.name] = job
                self._code_dict[job.name] = self._compile(job.source, job.name)
        finally:
            await db.release_current()
        self._initialized = True
        logger.info("Cron %d is yuklendi", len(self._jobs))

    async def work(self) -> None:
        """Arka plan dongusu: her saniye vadesi gelen isleri baslatir."""
        logger.info("Cron worker dongusu basladi")
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_due_jobs()
                except Exception:
                    logger.exception("Cron dongusunde beklenmeyen hata")
                await asyncio.sleep(self._check_interval)
        finally:
            logger.info("Cron worker dongusu durdu")

    async def start(self) -> None:
        """init() cagirir ve arka plan task'ini baslatir."""
        await self.init()
        if self._worker_task and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self.work(), name="cron-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=3.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
            except Exception:
                pass
            self._worker_task = None


cron_client = CronClient()
