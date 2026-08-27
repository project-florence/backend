#!/usr/bin/env python3
"""Florence saglik kontrolu (doctor) -- REFACTOR_PLAN.md Adim 5.

Kullanim:
    python scripts/doctor.py                # insan okumali cikti
    python scripts/doctor.py --json         # makine okumali JSON
    python scripts/doctor.py --fix=1        # guvenli oto-duzeltme (Redis proxy sifirlama)
    python scripts/doctor.py --fix=2        # docker compose restart (onay sorar)

Cikis kodu: herhangi bir kontrol FAIL ise 1, aksi halde 0 (WARN kod'u dusurmez).

Kontroller (``checks[].name``):
    db, redis, searxng          -- altyapi baglanti probu (degismedi)
    llm_master_key              -- FLORENCE_MASTER_KEY env kontrolu (AYRI kontrol --
                                    bkz. "WARN/FAIL karari" asagida, cok yaygin bir
                                    ariza sinifi oldugu icin kendi satiri var)
    llm:<purpose>                -- src/llm/settings.py::PURPOSES icindeki her amac
                                    (bugun: digest, report, embedding) icin: secim var
                                    mi, sagliayici katalogda mi, anahtar cozulebiliyor
                                    mu, model saglayicinin CANLI roster'inda mi, son
                                    token_usage kaydi basarili miydi.
    digest:<slot>                 -- src/core/config.py::get_config()["digest"]["slot_times"]
                                    icindeki her slot (bugun: morning, noon, evening)
                                    icin: bugun uretildi mi / pencere henuz gelmedi mi /
                                    pencere GECTI ama uretim yok (FAIL).
    digest:errors                 -- son 48 saatte purpose='digest' status='error'
                                    token_usage satirlari (bilgi amacli, WARN).
    disk, docker, logs             -- islemsel kontroller (degismedi)

--json semasi:
    {
      "checks": [{"name": str, "status": "OK"|"WARN"|"FAIL", "detail": str}, ...],
      "versions": {"python": str, "fastapi": str, "florence": str},
      "fix_level": 0|1|2,
      "fix_result": str | null,
      "suggestions": [{"check": str, "status": "WARN"|"FAIL", "suggestion": str}, ...],
      "healthy": bool   # true <=> checks icinde hic FAIL yok (WARN healthy'yi dusurmez)
    }
    ``suggestions`` yalniz OK-disi kontroller icin doldurulur (WARN dahil, sadece
    FAIL degil -- eski surumden FARK, bkz. asagidaki not).

WARN vs FAIL karari -- ``llm:<purpose>`` icin (REFACTOR_PLAN.md 2.4'ten bilincli
sapma, gerekce):
    REFACTOR_PLAN.md 2.4 "doctor kirmizi gosterir" diyor ama Adim 7'nin kendisi de
    "kisa yapilandirilmamis pencere bilincli" diyor -- ilk deploy sonrasi
    ``admin_cli.py llm set`` calistirilana kadar TUM amaclarin yapilandirilmamis
    olmasi BEKLENEN bir durumdur, bir ariza degil. Bu ikisini ayni renkte
    gostermek "kirmizi = gercek sorun" sinyalini asindirir (bu doctor'un butun
    amaci sinyal netligi). Bu yuzden burada AYRIM YAPILDI:
      - Hic secim yok (``llm_settings``'te satir yok)              -> WARN
        "henuz kurulmadi" -- beklenen ilk-deploy durumu.
      - Secim VAR ama cozulemiyor (saglayici katalogdan dusmus,
        llm_providers satiri yok, devre disi, base_url yok,
        anahtar cozulemiyor)                                       -> FAIL
        "bir zamanlar calisiyordu, simdi bozuk" -- 2026-08-26'nin sekli.
      - Secim cozuluyor ama saglayici anahtar istiyor (KEYLESS_PROVIDERS
        disinda) ve kayitli anahtar yok                            -> FAIL
        (PROVIDERS.md/Adim 4: opencode-zen roster'i anahtarsiz cevap verir
        ama /chat/completions 401 doner -- bu tuzagi sessizce gecmek YOK)
      - Model saglayicinin canli roster'inda YOK                   -> FAIL
        (2026-08-26'nin ikinci ayagi: ox-alpha-free roster'dan kaldirilmisti)
      - Roster'a ulasilamiyor (ag/saglayici kesintisi)              -> WARN
        (dogrulanamadi, ama doctor'un/secimin sucu olmayabilir)
      - Son token_usage kaydi status='error'                       -> FAIL

Nedensellik notu (karistirmamak icin): reasoning'in yapilandirilmis-cikti
(``digest``/``report``) amaclarinda varsayilan kapali olmasi 2026-08-26
arizasindan AYRI ve ondan ONCEKI bir kural (reasoning token'lari pydantic
semasini bozuyor, bkz. src/llm/settings.py::structured_output_forbids_
reasoning). Bu dosya o kurali test ETMEZ -- build_agent/pydantic-ai'nin isi.
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_BACKEND_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fastapi  # noqa: E402

from src.core.database import db  # noqa: E402
from src.core.redis import r  # noqa: E402
from src.llm import crypto  # noqa: E402
from src.llm.agents import _sanitize_error  # noqa: E402
from src.llm.settings import (  # noqa: E402
    PURPOSES,
    ResolvedLLM,
    Unconfigured,
    get_selection,
    list_providers,
    mask_secret,
    resolve_purpose,
)
from src.version import VERSION  # noqa: E402

# ``admin_cli.py`` Adim 4'te ayni sekilde ("scripts/" bir paket degil,
# calisan script kendi dizinini sys.path[0] yapar) yeniden yazildi ve
# ``_resolve_decrypted_key`` / ``_fetch_live_models`` / ``_last_token_usage_row``
# / ``KEYLESS_PROVIDERS`` gibi yardimcilari tanimliyor. Bu dosya (doctor.py)
# AYNI seyi (canli roster cekme, keyless saglayici listesi, son token_usage
# satiri) ihtiyac duyuyor -- KOPYALAMAK yerine dogrudan import ediliyor.
# Karar (raporda gerekcelendirildi): ayri bir paylasilan modul cikarmak
# (ornegin src/llm/introspection.py) bu asamada asiri muhendislik olurdu --
# iki dosya da "scripts/" altinda, ikisi de operasyonel CLI'lar, ikisinin de
# tek tuketicisi kendileri. admin_cli.py zaten test edilebilir bir modul
# (tests/test_admin_cli.py importlib ile yukluyor) ve pydantic_ai gibi zaten
# backend bagimliligi olan seyler disinda agir bir yan etkisi yok (modul
# import'unda sadece fonksiyon/sabit tanimlari calisiyor, main() sadece
# ``__name__ == "__main__"`` altinda).
import admin_cli  # noqa: E402

LOG_DIR = os.getenv("LOG_DIR", "/var/log/florence")

# digest slotlarinin saat dilimi -- src/cron/tasks.py::DIGEST_TZ ile AYNI deger
# ama BILINCLI OLARAK ORADAN IMPORT EDILMEDI: o modul pandas/yfinance gibi bu
# saglik kontroluyle alakasiz agir bagimliliklari da modul-seviyesinde import
# ediyor (doctor.py'nin hizli ve dar bir yuzeyi olmasi gerekiyor, bkz. bu
# dosyanin nasil okunacagi -- "gece yarisi bir arizada"). Slot ZAMANLARININ
# kendisi burada HARDCODE EDILMIYOR (get_config()'ten okunuyor); yalniz saat
# dilimi sabiti kucuk ve degismesi neredeyse imkansiz oldugu icin kopyalandi.
_DIGEST_TZ = ZoneInfo("Europe/Istanbul")
_DIGEST_LEAD = timedelta(minutes=15)  # bkz. src/cron/tasks.py::DIGEST_LEAD


def _now_local() -> datetime:
    """``datetime.now(_DIGEST_TZ)`` -- ayri bir fonksiyon olarak tanimli,
    boylece testler gercek saat dilimine bagimli olmadan sabit bir "su an"
    ile ``check_digest_slots``'u deterministik calistirabilir."""
    return datetime.now(_DIGEST_TZ)

# check adi (veya on ek) -> oneri metni. Sadece "sabit" isimli kontroller
# icin -- ``llm:*`` / ``digest:*`` icin dinamik oneriler _suggestion_for()'da.
_STATIC_SUGGESTIONS: dict[str, str] = {
    "db": "POSTGRES_HOST/POSTGRES_PORT/POSTGRES_USER/POSTGRES_PASSWORD ve `docker compose up -d postgres` kontrol et.",
    "redis": "REDIS_HOST/REDIS_PORT/REDIS_PASSWORD ve `docker compose up -d redis` kontrol et.",
    "searxng": "`docker compose up -d searxng`; NEWS_SEARCH_URL dogru mu?",
    "llm_master_key": (
        "FLORENCE_MASTER_KEY .env'de eksik/gecersiz. Yeni anahtar uretmek icin: "
        "`python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"`. "
        "Kaybolan anahtar ana anahtarla sifrelenmis eski satirlari kurtarmaz -- "
        "`admin_cli.py llm provider set <id>` ile anahtarlar yeniden girilmeli."
    ),
    "disk": "Log dizini diskinin dolu olmamasina dikkat; eski loglari temizle.",
    "docker": "`docker compose ps` ile servislerin ayakta oldugundan emin ol.",
    "logs": "Son 24 saatteki ERROR satirlarini incele (florence.log).",
    "digest:errors": "`admin_cli.py llm log --failures --since 48h` ile hata detaylarini incele.",
}


def _suggestion_for(check: dict) -> str | None:
    """``check``'in adina gore oneri metni doner (yoksa ``None``)."""
    name = check["name"]
    if name in _STATIC_SUGGESTIONS:
        return _STATIC_SUGGESTIONS[name]
    if name.startswith("llm:"):
        purpose = name.split(":", 1)[1]
        return (
            f"`admin_cli.py llm show` ile '{purpose}' amacinin secimini incele. "
            f"Yeniden ayarlamak icin `admin_cli.py llm set {purpose} <saglayici> <model>` "
            f"(bu komut saglayici/anahtar/canli-roster dogrulamasini kendisi yapar); "
            f"anahtar eksikse once `admin_cli.py llm provider set <saglayici>`."
        )
    if name.startswith("digest:"):
        slot = name.split(":", 1)[1]
        return (
            f"'{slot}' slotu icin: cron calisiyor mu (`docker compose ps`), "
            f"`admin_cli.py llm log --failures --since 24h` ile son digest hatalarina bak, "
            f"`admin_cli.py llm test digest` ile elle bir cagri dene."
        )
    return None


# ---------------------------------------------------------------------------
# Altyapi kontrolleri (degismedi -- yalniz tuple yerine dict donuyorlar,
# boylece TUM kontroller ayni sema uzerinden akiyor)
# ---------------------------------------------------------------------------


async def check_db() -> dict:
    name = "db"

    async def _probe() -> tuple | None:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
        await db.release_current()
        return row

    try:
        row = await asyncio.wait_for(_probe(), timeout=5)
        if row and row[0] == 1:
            return {"name": name, "status": "OK", "detail": "SELECT 1 ok"}
        return {"name": name, "status": "FAIL", "detail": "SELECT 1 beklenen sonucu donmedi"}
    except asyncio.TimeoutError:
        return {"name": name, "status": "FAIL", "detail": "DB baglantisi 5 sn icinde kurulamadi (zaman asimi)"}
    except Exception as e:
        return {"name": name, "status": "FAIL", "detail": f"{e.__class__.__name__}: {e}"}


async def check_redis() -> dict:
    name = "redis"
    try:
        conn = await r._get_conn()
        if conn is None:
            return {"name": name, "status": "FAIL", "detail": "Baglanti kurulamadi (proxy disabled)"}
        ok = await conn.ping()
        if ok:
            return {"name": name, "status": "OK", "detail": "ping ok"}
        return {"name": name, "status": "FAIL", "detail": "ping false dondu"}
    except Exception as e:
        return {"name": name, "status": "FAIL", "detail": f"{e.__class__.__name__}: {e}"}


async def check_searxng() -> dict:
    name = "searxng"
    try:
        from src.clients.search import news_search

        items = await news_search("test", limit=1)
        if items:
            return {"name": name, "status": "OK", "detail": f"{len(items)} sonuc dondu"}
        return {"name": name, "status": "WARN", "detail": "Servis calisti ama sonuc donmedi"}
    except Exception as e:
        return {"name": name, "status": "FAIL", "detail": f"{e.__class__.__name__}: {e}"}


def check_disk() -> dict:
    name = "disk"
    try:
        usage = shutil.disk_usage(LOG_DIR if os.path.isdir(LOG_DIR) else "/")
        free_gb = usage.free / (1024**3)
        if free_gb > 0.5:
            return {"name": name, "status": "OK", "detail": f"{free_gb:.2f} GB bos"}
        return {"name": name, "status": "WARN", "detail": f"sadece {free_gb:.2f} GB bos kaldi"}
    except Exception as e:
        return {"name": name, "status": "WARN", "detail": f"okunamadi: {e}"}


def check_docker() -> dict:
    name = "docker"
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return {"name": name, "status": "WARN", "detail": "docker kurulu degil (atlandi)"}
    except subprocess.TimeoutExpired:
        return {"name": name, "status": "WARN", "detail": "docker compose ps zaman asimi (atlandi)"}

    if result.returncode != 0:
        return {"name": name, "status": "WARN", "detail": f"docker compose ps basarisiz: {result.stderr.strip()[:120]}"}

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
        return {"name": name, "status": "WARN", "detail": "calisan servis yok (compose dosyasi bos olabilir)"}
    down = [s for s in states if not s.endswith("=running")]
    if down:
        return {"name": name, "status": "WARN", "detail": "; ".join(states) + " -> durmayan servisler var"}
    return {"name": name, "status": "OK", "detail": "; ".join(states)}


def check_logs() -> dict:
    name = "logs"
    log_dir = LOG_DIR
    if not os.path.isdir(log_dir):
        return {"name": name, "status": "WARN", "detail": f"{log_dir} yok (atlandi)"}
    cutoff = datetime.now() - timedelta(hours=24)
    error_count = 0
    files_seen = 0
    for fname in sorted(os.listdir(log_dir)):
        if not fname.startswith("florence.log"):
            continue
        path = os.path.join(log_dir, fname)
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
        return {"name": name, "status": "WARN", "detail": "son 24 saatte log dosyasi yok"}
    if error_count == 0:
        return {"name": name, "status": "OK", "detail": f"{files_seen} dosya tarandi, ERROR yok"}
    return {"name": name, "status": "WARN", "detail": f"son 24 saatte {error_count} ERROR satiri"}


# ---------------------------------------------------------------------------
# LLM kontrolleri -- REFACTOR_PLAN.md Adim 5 (yeni katman)
# ---------------------------------------------------------------------------


async def check_llm_master_key() -> dict:
    """``FLORENCE_MASTER_KEY``'in tanimli VE gecerli oldugunu dogrular.

    AYRI bir kontrol (``llm:<purpose>`` icine gomulmedi) cunku bu, tum
    saglayicilarin anahtarini AYNI ANDA cozemez hale getiren, cok yaygin
    olmasi beklenen tek bir arizadir -- gorevin acikca istedigi gibi net ve
    ayri raporlanir. Gercek ``crypto.encrypt``'i (bos bir prob degeriyle)
    cagirarak ayni dogrulama yolunu (base64 decode + 32 bayt kontrolu)
    dogrudan egzersiz eder; ``crypto``'nun private ``_load_master_key``'ine
    dokunmaz.
    """
    name = "llm_master_key"
    try:
        crypto.encrypt("doctor-probe", aad="__doctor__")
        return {"name": name, "status": "OK", "detail": "tanimli ve gecerli (base64, 32 bayt)"}
    except crypto.MasterKeyMissing:
        try:
            providers = await list_providers()
        except Exception:
            providers = []
        any_stored_key = any(p.get("api_key_encrypted") for p in providers)
        if any_stored_key:
            return {
                "name": name,
                "status": "FAIL",
                "detail": (
                    "tanimli DEGIL ama llm_providers'ta sifreli anahtar(lar) kayitli -- "
                    "hicbiri cozulemez, o saglayicilari kullanan TUM LLM cagrilari basarisiz olur."
                ),
            }
        return {
            "name": name,
            "status": "WARN",
            "detail": "tanimli degil (henuz sifreli anahtar da yok -- ilk 'llm provider set' oncesi .env'e eklenmeli)",
        }
    except crypto.MasterKeyInvalid as e:
        return {"name": name, "status": "FAIL", "detail": f"tanimli ama gecersiz: {e}"}
    except Exception as e:
        return {"name": name, "status": "FAIL", "detail": f"beklenmeyen hata: {_sanitize_error(e)}"}


async def check_llm_purpose(purpose: str) -> dict:
    """Bir amac (``digest``/``report``/``embedding``) icin tam saglik raporu.

    WARN/FAIL kararinin gerekcesi modul docstring'inde. Ilk bulunan FAIL
    kosulunda erken doner (5 madde arasinda oncelik sirasi yok -- hepsi ayni
    agirlikta bir "bozuk" durumu).
    """
    name = f"llm:{purpose}"
    try:
        selection = await get_selection(purpose)
        if selection is None:
            return {
                "name": name,
                "status": "WARN",
                "detail": "yapilandirilmamis: bu amac icin kayitli bir secim yok (`admin_cli.py llm set` ile ayarlanmali)",
            }

        resolved = await resolve_purpose(purpose)
        if isinstance(resolved, Unconfigured):
            return {"name": name, "status": "FAIL", "detail": f"secim var ama cozulemiyor: {resolved.reason}"}

        assert isinstance(resolved, ResolvedLLM)
        provider = resolved.provider
        details = [f"{provider.id}/{resolved.model}", f"base_url={resolved.base_url}"]

        # 2) anahtar var mi / gerekiyor mu (Adim 4 bulgusu: opencode-zen/-go
        # anahtarsiz roster donduruyor ama chat/completions 401 veriyor --
        # KEYLESS_PROVIDERS'a girmiyorlar, bu yuzden onlar icin de FAIL).
        if resolved.api_key:
            details.append(f"anahtar={mask_secret(resolved.api_key)}")
        elif provider.id in admin_cli.KEYLESS_PROVIDERS:
            details.append("anahtar=gerekmiyor")
        else:
            return {
                "name": name,
                "status": "FAIL",
                "detail": (
                    f"{provider.id}/{resolved.model}: saglayici anahtar gerektiriyor ama "
                    f"llm_providers'ta kayitli anahtar yok (chat/completions cagrisi 401 ile "
                    f"basarisiz olur). `admin_cli.py llm provider set {provider.id}`"
                ),
            }

        # 3) model saglayicinin CANLI roster'inda mi -- admin_cli._fetch_live_models
        # ile ayni fonksiyon (KOPYALANMADI, import edildi -- bkz. modul basi notu).
        roster_warn = False
        if provider.models_url:
            models = await admin_cli._fetch_live_models(provider.id, provider.models_url, resolved.api_key)
            if models is None:
                details.append("roster=ulasilamadi (dogrulanamadi)")
                roster_warn = True
            elif resolved.model not in models:
                return {
                    "name": name,
                    "status": "FAIL",
                    "detail": (
                        f"{provider.id}/{resolved.model}: model saglayicinin canli roster'inda YOK "
                        f"({len(models)} model listelendi). `admin_cli.py llm models {provider.id}` "
                        f"ile mevcut modelleri gor."
                    ),
                }
            else:
                details.append(f"roster=OK ({len(models)} model)")
        else:
            details.append("roster=ucnokta yok (dogrulanamadi)")

        # 4) son cagri (token_usage) durumu -- admin_cli._last_token_usage_row
        # ile ayni fonksiyon (KOPYALANMADI, import edildi).
        last = await admin_cli._last_token_usage_row(purpose)
        if last is None:
            details.append("son_cagri=hic yok")
        elif last["status"] == "error":
            err = (last.get("error") or "(detay yok)")[:200]
            return {
                "name": name,
                "status": "FAIL",
                "detail": f"{provider.id}/{resolved.model}: son cagri basarisiz ({last['created_at']}): {err}",
            }
        else:
            details.append(f"son_cagri=OK {last['created_at']}")

        status = "WARN" if roster_warn else "OK"
        return {"name": name, "status": status, "detail": "; ".join(details)}
    except Exception as e:
        return {"name": name, "status": "FAIL", "detail": f"kontrol basarisiz: {e.__class__.__name__}: {_sanitize_error(e)}"}


# ---------------------------------------------------------------------------
# Digest slot sagligi -- REFACTOR_PLAN.md Adim 5 (yeni kontrol, 2026-08-26
# arizasinin 36 saat GORUNMEZ kalmasinin dogrudan sebebi buydu)
# ---------------------------------------------------------------------------


async def check_digest_slots() -> list[dict]:
    """Slot basina (``digest.slot_times``'tan) bugun uretilip uretilmedigini kontrol eder.

    Slot saatleri src/core/config.py::get_config()["digest"]["slot_times"]'tan
    okunur (HARDCODE EDILMEDI). Uretim penceresi mantigi src/cron/tasks.py::
    _due_digest_slot ile AYNI kural (slot saatinden ~15 dk once baslar) ama bu
    fonksiyon "su an pencerede miyiz" degil "pencere GECTI mi ve uretim YOK mu"
    sorusunu soruyor -- gecmis bir slot atlandiysa, artik pencerede olmasa bile
    FAIL vermesi gerekiyor (gorev: "gecmis bir slot uretilmemisse FAIL ver --
    sessiz kalma").
    """
    from src.core.config import get_config

    try:
        slot_times: dict[str, str] = get_config()["digest"]["slot_times"]
    except Exception as e:
        return [{"name": "digest", "status": "FAIL", "detail": f"digest.slot_times config'ten okunamadi: {_sanitize_error(e)}"}]

    now_local = _now_local()
    today = now_local.date()

    try:
        async with db.cursor(row_factory=None) as cur:
            await cur.execute("SELECT slot, MAX(created_at) FROM digests GROUP BY slot")
            last_by_slot = dict(await cur.fetchall())
            await cur.execute("SELECT slot FROM digests WHERE date = %s", (today,))
            today_slots = {row[0] for row in await cur.fetchall()}
        await db.release_current()
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        err = _sanitize_error(e)
        return [
            {"name": f"digest:{slot}", "status": "FAIL", "detail": f"digests tablosu okunamadi: {err}"}
            for slot in slot_times
        ]

    checks = []
    for slot, hhmm in slot_times.items():
        name = f"digest:{slot}"
        try:
            hour, minute = map(int, hhmm.split(":"))
        except Exception:
            checks.append({"name": name, "status": "FAIL", "detail": f"gecersiz slot saati: {hhmm!r}"})
            continue

        slot_dt = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_start = slot_dt - _DIGEST_LEAD
        last_at = last_by_slot.get(slot)
        last_desc = f"son uretim: {last_at}" if last_at else "son uretim: HIC YOK"

        if slot in today_slots:
            checks.append({"name": name, "status": "OK", "detail": f"bugun uretildi ({last_desc})"})
        elif now_local >= slot_dt:
            checks.append({
                "name": name,
                "status": "FAIL",
                "detail": (
                    f"bugun ({today}) {hhmm} TRT penceresi gecti ama uretim YOK! {last_desc}. "
                    f"Kontrol: cron calisiyor mu (`docker compose ps`), "
                    f"`admin_cli.py llm log --failures --since 24h`, `admin_cli.py llm test digest`."
                ),
            })
        else:
            state = "uretim penceresinde" if window_start <= now_local < slot_dt else "pencere henuz gelmedi"
            checks.append({
                "name": name,
                "status": "OK",
                "detail": f"{state} ({hhmm} TRT bekleniyor). {last_desc}",
            })
    return checks


async def check_digest_errors() -> dict:
    """Son 48 saatteki ``purpose='digest' status='error'`` satirlarini ozetler.

    ``digest:<slot>`` FAIL vermiyor olsa bile (ornegin bir slot ilk denemede
    hata verip 10 dk sonraki cron tick'inde basarili olduysa) bu bilgi
    gizli kalmamali -- bu yuzden AYRI bir WARN kontrolu (bir hata olmus
    olmasi tek basina "su an bozuk" anlamina gelmez, bu yuzden FAIL degil).
    """
    name = "digest:errors"
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        async with db.cursor() as cur:
            await cur.execute(
                "SELECT created_at, provider, model, error FROM token_usage "
                "WHERE purpose = 'digest' AND status = 'error' AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT 5",
                (cutoff,),
            )
            rows = await cur.fetchall()
        await db.release_current()
        if not rows:
            return {"name": name, "status": "OK", "detail": "son 48 saatte digest hatasi yok"}
        summary = "; ".join(
            f"{row['created_at']} {row.get('provider')}/{row.get('model')}: {(row.get('error') or '')[:120]}"
            for row in rows
        )
        return {"name": name, "status": "WARN", "detail": f"son 48 saatte {len(rows)} digest hatasi (en yenisi ustte): {summary}"}
    except Exception as e:
        try:
            await db.rollback()
        except Exception:
            pass
        return {"name": name, "status": "FAIL", "detail": f"token_usage sorgusu basarisiz: {_sanitize_error(e)}"}


def get_versions() -> dict:
    return {
        "python": platform.python_version(),
        "fastapi": fastapi.__version__,
        "florence": VERSION,
    }


# ---------------------------------------------------------------------------
# Fix seviyeleri (degismedi)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cikti
# ---------------------------------------------------------------------------

_GROUP_ORDER = ("infra", "llm", "digest", "ops")


def _group_of(name: str) -> str:
    if name in ("db", "redis", "searxng"):
        return "infra"
    if name == "llm_master_key" or name.startswith("llm:"):
        return "llm"
    if name.startswith("digest:"):
        return "digest"
    return "ops"


def _format_human(checks: list[dict], versions: dict, suggestions: list[dict], fix_result: str | None) -> str:
    lines = [f"Florence Doctor — {datetime.now().isoformat(timespec='seconds')}"]
    grouped: dict[str, list[dict]] = {g: [] for g in _GROUP_ORDER}
    for c in checks:
        grouped[_group_of(c["name"])].append(c)
    for group in _GROUP_ORDER:
        rows = grouped[group]
        if not rows:
            continue
        lines.append("")
        lines.append(f"--- {group} ---")
        for c in rows:
            lines.append(f"[{c['status']:4}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("Versions: python {python} | fastapi {fastapi} | florence {florence}".format(**versions))
    if suggestions:
        lines.append("")
        lines.append("=== Oneriler ===")
        lines.append("| Durum | Kontrol | Oneri |")
        lines.append("|---|---|---|")
        for s in suggestions:
            lines.append(f"| {s['status']} | {s['check']} | {s['suggestion']} |")
    if fix_result:
        lines.append("")
        lines.append(f"Fix: {fix_result}")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Florence saglik kontrolu -- --json semasi ve WARN/FAIL kurallari icin bu dosyanin docstring'ine bakin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", action="store_true", help="JSON cikti")
    parser.add_argument(
        "--fix", type=int, default=0, choices=[0, 1, 2],
        help="1=guvenli oto-duzeltme (Redis proxy sifirlama), 2=docker compose restart (onay sorar)",
    )
    args = parser.parse_args()

    checks: list[dict] = []
    checks.append(await check_db())
    checks.append(await check_redis())
    checks.append(await check_searxng())
    checks.append(await check_llm_master_key())
    for purpose in PURPOSES:
        checks.append(await check_llm_purpose(purpose))
    checks.extend(await check_digest_slots())
    checks.append(await check_digest_errors())
    checks.append(check_disk())
    checks.append(check_docker())
    checks.append(check_logs())

    versions = get_versions()

    suggestions: list[dict] = []
    for c in checks:
        if c["status"] == "OK":
            continue
        suggestion = _suggestion_for(c)
        if suggestion:
            suggestions.append({"check": c["name"], "status": c["status"], "suggestion": suggestion})

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
