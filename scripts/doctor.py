#!/usr/bin/env python3
"""Florence saglik kontrolu (doctor).

Kullanim:
    python scripts/doctor.py                # insan okumali cikti
    python scripts/doctor.py --json         # makine okumali JSON
    python scripts/doctor.py --fix=1        # guvenli oto-duzeltme (Redis proxy sifirlama)
    python scripts/doctor.py --fix=2        # docker compose restart (onay sorar)

Cikis kodu: tum kontroller OK ise 0, herhangi biri FAIL ise 1.
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fastapi  # noqa: E402

from src.core.database import db  # noqa: E402
from src.core.redis import r  # noqa: E402
from src.version import VERSION  # noqa: E402

LOG_DIR = os.getenv("LOG_DIR", "/var/log/florence")

# check adi -> oneri metni (tablo ciktisinda kullanilir).
SUGGESTIONS = {
    "db": "POSTGRES_HOST/POSTGRES_PORT/POSTGRES_USER/POSTGRES_PASSWORD ve `docker compose up -d postgres` kontrol et.",
    "redis": "REDIS_HOST/REDIS_PORT/REDIS_PASSWORD ve `docker compose up -d redis` kontrol et.",
    "searxng": "`docker compose up -d searxng`; NEWS_SEARCH_URL dogru mu?",
    "llm": "CUSTOM_URL/CUSTOM_MODEL/CUSTOM_API_KEY veya OPENROUTER_URL/OPENROUTER_API_KEY kontrol et.",
    "disk": "Log dizini diskinin dolu olmamasina dikkat; eski loglari temizle.",
    "docker": "`docker compose ps` ile servislerin ayakta oldugundan emin ol.",
    "logs": "Son 24 saatteki ERROR satirlarini incele (florence.log).",
}


async def check_db() -> tuple[str, str]:
    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
        await db.release_current()
        if row and row[0] == 1:
            return "OK", "SELECT 1 ok"
        return "FAIL", "SELECT 1 beklenen sonucu donmedi"
    except Exception as e:
        return "FAIL", f"{e.__class__.__name__}: {e}"


async def check_redis() -> tuple[str, str]:
    try:
        conn = await r._get_conn()
        if conn is None:
            return "FAIL", "Baglanti kurulamadi (proxy disabled)"
        ok = await conn.ping()
        return ("OK", "ping ok") if ok else ("FAIL", "ping false dondu")
    except Exception as e:
        return "FAIL", f"{e.__class__.__name__}: {e}"


async def check_searxng() -> tuple[str, str]:
    try:
        from src.clients.search import news_search

        items = await news_search("test", limit=1)
        if items:
            return "OK", f"{len(items)} sonuc dondu"
        return "WARN", "Servis calisti ama sonuc donmedi"
    except Exception as e:
        return "FAIL", f"{e.__class__.__name__}: {e}"


async def check_llm() -> tuple[str, str]:
    try:
        from src.clients.llm import health_check

        ok = await health_check()
        return ("OK", "models.list ok") if ok else ("FAIL", "models.list basarisiz")
    except Exception as e:
        return "FAIL", f"{e.__class__.__name__}: {e}"


def check_disk() -> tuple[str, str]:
    try:
        usage = shutil.disk_usage(LOG_DIR if os.path.isdir(LOG_DIR) else "/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb > 0.5:
            return "OK", f"{free_gb:.2f} GB bos"
        return "WARN", f"sadece {free_gb:.2f} GB bos kaldi"
    except Exception as e:
        return "WARN", f"okunamadi: {e}"


def check_docker() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return "WARN", "docker kurulu degil (atlandi)"
    except subprocess.TimeoutExpired:
        return "WARN", "docker compose ps zaman asimi (atlandi)"

    if result.returncode != 0:
        return "WARN", f"docker compose ps basarisiz: {result.stderr.strip()[:120]}"

    states = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            svc = json.loads(line)
            states.append(f"{svc.get('Service', '?')}={svc.get('State', '?')}")
        except json.JSONDecodeError:
            continue
    if not states:
        return "WARN", "calisan servis yok (compose dosyasi bos olabilir)"
    down = [s for s in states if not s.endswith("=running")]
    if down:
        return "WARN", "; ".join(states) + " -> durmayan servisler var"
    return "OK", "; ".join(states)


def check_logs() -> tuple[str, str]:
    log_dir = LOG_DIR
    if not os.path.isdir(log_dir):
        return "WARN", f"{log_dir} yok (atlandi)"
    cutoff = datetime.now() - timedelta(hours=24)
    error_count = 0
    files_seen = 0
    for name in sorted(os.listdir(log_dir)):
        if not name.startswith("florence.log"):
            continue
        path = os.path.join(log_dir, name)
        try:
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                continue
            files_seen += 1
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if " ERROR " in line or " ERROR:" in line:
                        error_count += 1
        except OSError:
            continue
    if files_seen == 0:
        return "WARN", "son 24 saatte log dosyasi yok"
    if error_count == 0:
        return "OK", f"{files_seen} dosya tarandi, ERROR yok"
    return "WARN", f"son 24 saatte {error_count} ERROR satiri"


def get_versions() -> dict:
    return {
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "florence": VERSION,
    }


def apply_fix_level_1() -> str:
    """Guvenli oto-duzeltme: Redis proxy durumunu sifirla (cooldown'i atla)."""
    r._conn = None
    r._disabled = False
    r._retry_after = 0.0
    return "Redis proxy durumu sifirlandi (cooldown atlandi); bir sonraki cagri yeniden baglanmayi dener."


async def apply_fix_level_2() -> str:
    """docker compose restart (onay ister)."""
    answer = input("api+admin servisleri yeniden baslatilsin mi? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        return "Iptal edildi."
    result = subprocess.run(
        ["docker", "compose", "restart", "api", "admin"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return "docker compose restart api admin tamam."
    return f"docker compose restart basarisiz: {result.stderr.strip()[:200]}"


def _format_human(checks: list[dict], versions: dict, suggestions: list[dict], fix_result: str | None) -> str:
    lines = [f"Florence Doctor — {datetime.now().isoformat(timespec='seconds')}"]
    for c in checks:
        lines.append(f"[{c['status']:4}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("Versions: python {python} | fastapi {fastapi} | florence {florence}".format(**versions))
    if suggestions:
        lines.append("")
        lines.append("=== Oneriler ===")
        lines.append("| Kontrol | Oneri |")
        lines.append("|---|---|")
        for s in suggestions:
            lines.append(f"| {s['check']} | {s['suggestion']} |")
    if fix_result:
        lines.append("")
        lines.append(f"Fix: {fix_result}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Florence saglik kontrolu")
    parser.add_argument("--json", action="store_true", help="JSON cikti")
    parser.add_argument(
        "--fix", type=int, default=0, choices=[0, 1, 2],
        help="1=guvenli oto-duzeltme (Redis proxy sifirlama), 2=docker compose restart (onay sorar)",
    )
    args = parser.parse_args()

    db_res = await check_db()
    redis_res = await check_redis()
    searxng_res = await check_searxng()
    llm_res = await check_llm()
    disk_res = check_disk()
    docker_res = check_docker()
    logs_res = check_logs()

    checks: list[dict[str, str]] = [
        {"name": "db", "status": db_res[0], "detail": db_res[1]},
        {"name": "redis", "status": redis_res[0], "detail": redis_res[1]},
        {"name": "searxng", "status": searxng_res[0], "detail": searxng_res[1]},
        {"name": "llm", "status": llm_res[0], "detail": llm_res[1]},
        {"name": "disk", "status": disk_res[0], "detail": disk_res[1]},
        {"name": "docker", "status": docker_res[0], "detail": docker_res[1]},
        {"name": "logs", "status": logs_res[0], "detail": logs_res[1]},
    ]
    versions = get_versions()

    suggestions: list[dict[str, str]] = [
        {"check": c["name"], "suggestion": SUGGESTIONS[c["name"]]}
        for c in checks
        if c["status"] == "FAIL"
    ]

    fix_result = None
    if args.fix == 1:
        fix_result = apply_fix_level_1()
    elif args.fix == 2:
        fix_result = await apply_fix_level_2()

    failed = any(c["status"] == "FAIL" for c in checks)

    if args.json:
        print(json.dumps({
            "checks": checks,
            "versions": versions,
            "fix_level": args.fix,
            "fix_result": fix_result,
            "suggestions": suggestions,
            "healthy": not failed,
        }, ensure_ascii=False, indent=2))
    else:
        print(_format_human(checks, versions, suggestions, fix_result))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
