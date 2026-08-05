import logging
import threading
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
    """Kaydedilen bir cron isi. Redis'e degil, veritabanina kalici olarak yazilir.

    `source` orijinal kaynak kodun metnidir; is basladiktan sonra (sunucu
    yeniden baslatilsa bile) yeniden derlenip calistirilabilir.
    """

    name: str
    description: str = ""
    source: str
    interval_ms: int
    last_run: datetime | None = None

    @property
    def interval(self) -> float:
        return self.interval_ms / 1000.0


class CronClient:
    """Redis ve veritabani uzerinde koordine edilen arka plan zamanlayicisi.

    Mantik: sunucuda birden fazla worker oldugunda, tek sefer yapilmasi
    gereken islemler birden fazla tekrarlanmasin diye calisma gecmisi (last_run)
    ve kilitler kullanilir. Bir worker isi claim ettiginde o isin zaman damgasini
    gunceller; boylece diger workerlar ayni isi tekrarlamaz.

    Her vadesi gelen is kendi thread'inde calistirilir; boylece uzun suren bir
    is (orn. fiyat guncelleme) diger isleri bloklamaz. Ayni isin ust uste
    binmesi `_running` kumesi ve Redis kilidi ile engellenir.
    """

    def __init__(self) -> None:
        self._code_dict: dict[str, CodeType] = {}
        self._jobs: dict[str, Job] = {}
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._check_interval = 1.0
        self._fallback_locks: dict[str, float] = {}
        self._initialized = False

    # ------------------------------------------------------------------
    # Veritabani yardimcilar
    # ------------------------------------------------------------------
    def _save_to_db(self, job: Job) -> None:
        with db.cursor() as cur:
            cur.execute(
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
        db.commit()

    def _load_from_db(self) -> list[Job]:
        with db.cursor() as cur:
            cur.execute("SELECT name, description, source, interval_ms, last_run FROM cron_jobs")
            rows = cur.fetchall()
        return [
            Job(name=row[0], description=row[1], source=row[2], interval_ms=row[3], last_run=row[4])
            for row in rows
        ]

    def _delete_from_db(self, name: str) -> None:
        with db.cursor() as cur:
            cur.execute("DELETE FROM cron_jobs WHERE name = %s", (name,))
        db.commit()

    def _mark_done(self, name: str) -> None:
        now = datetime.now(UTC)
        with db.cursor() as cur:
            cur.execute("UPDATE cron_jobs SET last_run = %s WHERE name = %s", (now, name))
        db.commit()
        job = self._jobs.get(name)
        if job:
            job.last_run = now

    # ------------------------------------------------------------------
    # Redis kilit (coklu worker koordinasyonu)
    # ------------------------------------------------------------------
    def _claim(self, name: str, interval_ms: int) -> bool:
        """Isi atomik olarak sahiplenir. Kazanan worker True alir."""
        lock_key = f"{PREFIX}lock:{name}"
        ttl_seconds = max(1, int(interval_ms / 1000))
        try:
            conn = r._get_conn()
        except Exception:
            conn = None
        if conn is not None:
            return bool(conn.set(lock_key, "1", nx=True, ex=ttl_seconds))

        now = time.monotonic()
        with self._lock:
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

    def _run_code(self, code: CodeType, name: str) -> None:
        globals_dict = {"__cron_name__": name}
        exec(code, globals_dict)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def register_job(
        self,
        name: str,
        interval_ms: int,
        code: str | Path,
        description: str = "",
        last_run: datetime | None = None,
    ) -> None:
        """Bir isi kaydeder: veritabanina yazar ve yerel sozluge derlenmis kodunu ekler.

        `last_run` verilirse ilk calismayi ertelemek icin kullanilir; mevcut
        isin DB'deki `last_run` degeri kayit guncellenirken korunur.
        """
        source = code.read_text() if isinstance(code, Path) else code

        job = Job(name=name, description=description, source=source, interval_ms=interval_ms, last_run=last_run)
        with self._lock:
            self._jobs[name] = job
            self._code_dict[name] = self._compile(source, name)
        self._save_to_db(job)
        logger.info("Cron job '%s' kaydedildi (interval=%dms)", name, interval_ms)

    def get_job(self, name: str) -> Job | None:
        return self._jobs.get(name)

    def list_jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.name)

    def remove_job(self, name: str) -> None:
        with self._lock:
            self._jobs.pop(name, None)
            self._code_dict.pop(name, None)
            self._running.discard(name)
        self._delete_from_db(name)
        logger.info("Cron job '%s' kaldirildi", name)

    def run_job(self, name: str) -> bool:
        with self._lock:
            code = self._code_dict.get(name)
        if code is None:
            logger.warning("Cron job '%s' bulunamadi", name)
            return False
        logger.info("Cron job '%s' calistiriliyor", name)
        self._run_code(code, name)
        return True

    def _run_job_worker(self, name: str) -> None:
        try:
            self.run_job(name)
        except Exception:
            logger.exception("Cron job '%s' calistirilirken hata", name)
        finally:
            self._mark_done(name)
            with self._lock:
                self._running.discard(name)

    def run_due_jobs(self) -> None:
        for job in list(self._jobs.values()):
            if not self._is_due(job):
                continue
            if job.name in self._running:
                continue
            if not self._claim(job.name, job.interval_ms):
                continue
            with self._lock:
                self._running.add(job.name)
            thread = threading.Thread(
                target=self._run_job_worker,
                args=(job.name,),
                name=f"cron-job-{job.name}",
                daemon=True,
            )
            thread.start()

    def init(self) -> None:
        """Kayitlari veritabanindan yukler ve derlenmis kodlari olusturur."""
        if self._initialized:
            return
        for job in self._load_from_db():
            self._jobs[job.name] = job
            self._code_dict[job.name] = self._compile(job.source, job.name)
        self._initialized = True
        logger.info("Cron %d is yuklendi", len(self._jobs))

    def work(self) -> None:
        """Arka plan dongusu: her saniye vadesi gelen isleri baslatir."""
        logger.info("Cron worker dongusu basladi")
        while not self._stop_event.wait(self._check_interval):
            try:
                self.run_due_jobs()
            except Exception:
                logger.exception("Cron dongusunde beklenmeyen hata")
        logger.info("Cron worker dongusu durdu")

    def start(self) -> None:
        """init() cagirir ve arka plan thread'ini baslatir."""
        self.init()
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.work, name="cron-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)


cron_client = CronClient()
