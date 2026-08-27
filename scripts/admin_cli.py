#!/usr/bin/env python3
"""Florence admin CLI — dogrudan DB/Redis uzerinden yonetim islemleri.

Kullanim (mevcut komut grubu, davranis korunuyor):
    python scripts/admin_cli.py users list|create|freeze|unfreeze
    python scripts/admin_cli.py credits give <username> <amount> [--type gift|free]
    python scripts/admin_cli.py announcement add <title> <content>
    python scripts/admin_cli.py maintenance <feature> enable|disable
    python scripts/admin_cli.py stats
    python scripts/admin_cli.py export stats

LLM saglayici/model yonetimi (REFACTOR_PLAN.md Adim 4 + Adim 6.5, bkz. PROVIDERS.md):
    python scripts/admin_cli.py llm providers
    python scripts/admin_cli.py llm provider set <id> [--base-url URL]
    python scripts/admin_cli.py llm provider rm <id>
    python scripts/admin_cli.py llm models <provider> [--free]
    python scripts/admin_cli.py llm model show
    python scripts/admin_cli.py llm model set <provider>/<model> [--reasoning V] [--timeout N]
    python scripts/admin_cli.py llm test <purpose>
    python scripts/admin_cli.py llm usage [--since 24h] [--by provider|model|purpose]
    python scripts/admin_cli.py llm log [--failures] [--since 24h]
    python scripts/admin_cli.py llm rotate-key

Adim 6.5 (2026-08-27 kullanici itirazi): amac-basina secim kalkti. TEK bir
``llm model set`` digest VE report'u AYNI ANDA gunceller -- ikisini ayri ayri
ayarlayip birini guncelleyip digerini unutmak (CUSTOM_MODEL/CUSTOM_URL
ayrismasinin bir kat yukarida yeniden uretilmesi) yapisal olarak imkansiz
hale geldi. Amaca gore secim yapan bir CLI bayragi HICBIR YERDE yok. Gozlemlenebilirlik
(``token_usage.purpose``, ``llm log``/``llm usage --by purpose``) AYRI bir
eksen ve KORUNDU -- digest/report'un maliyeti ve hatalari hala ayri ayri
gorunur, yalniz AYARIN kendisi artik tek.

Her komut --json ile makine-okunur cikti verebilir. Her yikici (mutating) komut
--yes ile onay istemini atlayabilir.

Guvenlik notu — ADMIN_TOKEN kapisi (bu dosyanin eski surumunden FARKLI davranir,
bkz. REFACTOR_PLAN.md Adim 4): ortamda ``ADMIN_TOKEN`` tanimliysa, calistirilan
komut ne olursa olsun ``--admin-token`` ile ESLESMESI ZORUNLUDUR (sabit-zamanli
karsilastirma, ``hmac.compare_digest``). ``ADMIN_TOKEN`` tanimli DEGILSE: salt-
okunur komutlar (list/show/stats/usage/log/...) calisir, ama YIKICI komutlar
(kullanici olusturma/dondurma, kredi verme, duyuru/bakim degisikligi, LLM
saglayici/secim yazma-silme, anahtar dondurme, gercek LLM cagrisi) REDDEDILIR.
Eski davranis (token tanimsizsa sadece uyari basip GECMEK) bilerek kaldirildi
-- bu bir koruma degildi.
"""

import argparse
import asyncio
import base64
import getpass
import hmac
import json as json_module
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402
import psycopg  # noqa: E402
from pydantic_ai import Agent as PydanticAgent  # noqa: E402

from src.clients.http import get_client  # noqa: E402
from src.core.database import db  # noqa: E402
from src.core.redis import r  # noqa: E402
from src.llm import crypto  # noqa: E402
from src.llm.agents import LLMPurposeUnconfigured, build_agent, elapsed_ms, log_llm_call  # noqa: E402
from src.llm.providers import PROVIDERS, InvalidModelSpec, resolve as resolve_spec  # noqa: E402
from src.llm.settings import (  # noqa: E402
    PURPOSES,
    ResolvedLLM,
    Unconfigured,
    get_provider_row,
    list_providers,
    mask_secret,
    remove_provider,
    resolve_llm,
    set_selection,
    structured_output_forbids_reasoning,
    upsert_provider,
)
from src.services.token import get_token_summary  # noqa: E402

USERS_LIST_COLUMNS = ("id", "username", "email", "user_type", "created_at", "is_frozen", "credits")

# Saglayicilar ki anahtarsiz calisabilir (bkz. PROVIDERS.md "reasoning_param
# neden bazilarinda bos" ve REFACTOR_PLAN.md 2.6 madde 2) -- ``llm set``
# dogrulamasinin 2. adimi bunlar icin atlanir.
# Gercekten anahtar istemeyen saglayicilar. DIKKAT: opencode-zen/opencode-go
# BURAYA AIT DEGIL -- /models roster ucnoktasi anahtarsiz cevap veriyor ama
# /chat/completions 401 "Invalid API key" donuyor (2026-08-27 canli deneme).
# Onlari keyless saymak, dogrulamayi gecip her cagrida 401 alan bir secim
# yazilmasina izin verirdi.
KEYLESS_PROVIDERS = frozenset({"ollama-local"})


# ---------------------------------------------------------------------------
# Ortak yardimcilar: cikti, onay, ADMIN_TOKEN kapisi, sir okuma
# ---------------------------------------------------------------------------


def _out(args: argparse.Namespace, data: dict, *human_lines: str) -> None:
    """--json verildiyse ``data``'yi JSON olarak, degilse insan-okunur satirlari basar."""
    if getattr(args, "json", False):
        print(json_module.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        for line in human_lines:
            print(line)


def _print_table(headers: tuple, rows: list[tuple]) -> None:
    if not rows:
        print("(kayit yok)")
        return
    widths = [
        max(len(str(headers[i])), *(len(str(row[i])) for row in rows))
        for i in range(len(headers))
    ]
    header_line = " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(" | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))


def _confirm(prompt: str, yes: bool) -> bool:
    """Yikici bir islem icin gercek onay ister; ``--yes`` verildiyse atlanir.

    TTY degilse (boru/betik) ve ``--yes`` verilmediyse ONAYLANMAZ -- sessiz
    varsayilan-evet YOK, betikli kullanim acikca ``--yes`` gecmeli.
    """
    if yes:
        return True
    if not sys.stdin.isatty():
        print("HATA: bu islem onay gerektiriyor ama girdi bir terminal degil; --yes kullanin.")
        return False
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def _read_secret_optional(prompt: str) -> str | None:
    """API anahtarini stdin'den okur -- ASLA argumandan (ps/shell gecmisinde gorunur).

    TTY ise gizli (echo'suz) prompt; boru ise tek satir okunur. Bos girdi
    ``None`` doner (cagiran "mevcut anahtari koru" olarak yorumlar).
    """
    if sys.stdin.isatty():
        value = getpass.getpass(prompt)
    else:
        value = sys.stdin.readline().rstrip("\n")
    return value if value else None


def _check_admin_token(args: argparse.Namespace, *, destructive: bool) -> int | None:
    """ADMIN_TOKEN kapisi. Gecerse ``None``, reddedilirse cikis kodu doner."""
    env_token = os.getenv("ADMIN_TOKEN")
    if env_token:
        provided = getattr(args, "admin_token", None)
        if not provided or not hmac.compare_digest(provided, env_token):
            print(
                "HATA: ADMIN_TOKEN ortam degiskeni tanimli; --admin-token ile eslesen "
                "bir deger vermelisiniz."
            )
            return 1
        return None
    if destructive:
        print(
            "HATA: ADMIN_TOKEN ortam degiskeni tanimli degil; bu korumasiz durumda "
            "yikici komutlar REDDEDILIR. Salt-okunur komutlar (list/show/stats/usage/"
            "log/...) ADMIN_TOKEN olmadan da calisir."
        )
        return 1
    return None


async def _dispatch(handler, args: argparse.Namespace, *, destructive: bool) -> int:
    gate = _check_admin_token(args, destructive=destructive)
    if gate is not None:
        return gate
    return await handler(args)


def _parse_since(value: str) -> datetime:
    """``--since`` degerini cozer: '24h' / '7d' / '2w' / '30m' veya ISO tarih."""
    value = value.strip()
    match = re.fullmatch(r"(\d+)([hdwm])", value)
    if match:
        n, unit = int(match.group(1)), match.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(minutes=n),
        }[unit]
        return datetime.now(timezone.utc) - delta
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"gecersiz --since degeri: {value!r} (ornek: '24h', '7d', '2026-08-20')"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


async def users_list(args: argparse.Namespace) -> int:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            SELECT u.id, u.username, u.email, u.user_type, u.created_at, u.is_frozen,
                   COALESCE((SELECT SUM(amount) FROM user_credits WHERE user_id = u.id), 0) AS credits
            FROM users u
            ORDER BY u.id
        """)
        rows = await cur.fetchall()
    await db.release_current()

    if getattr(args, "json", False):
        payload = [dict(zip(USERS_LIST_COLUMNS, row)) for row in rows]
        print(json_module.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    if not rows:
        print("Kullanici yok.")
        return 0
    table_rows = [
        (
            row[0], row[1], row[2] or "-", row[3],
            row[4].isoformat() if row[4] else "-",
            "frozen" if row[5] else "active", f"{row[6]:.2f}",
        )
        for row in rows
    ]
    _print_table(USERS_LIST_COLUMNS, table_rows)
    return 0


async def users_set_frozen(args: argparse.Namespace, *, frozen: bool) -> int:
    username = args.username
    if not _confirm(f"Kullanici '{username}' {'dondurulacak' if frozen else 'cozulecek'}.", args.yes):
        print("Iptal edildi.")
        return 1
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "UPDATE users SET is_frozen = %s WHERE username = %s RETURNING id",
            (frozen, username),
        )
        row = await cur.fetchone()
        if not row:
            await db.rollback()
            print(f"Kullanici bulunamadi: {username}")
            return 1
        await db.commit()
        try:
            await r.delete(f"user:frozen:{row[0]}")
        except Exception:
            pass
    action = "donduruldu" if frozen else "cozuldu"
    _out(args, {"username": username, "frozen": frozen}, f"Kullanici '{username}' {action}.")
    return 0


async def users_create(args: argparse.Namespace) -> int:
    """Kullanici olusturur ve DEFAULT_CREDITS baslangic kredisini verir.

    Register API'siyle ayni davranis: yeni kullanici kredili baslar.
    """
    username, email, password = args.username, args.email.lower(), args.password
    if not _confirm(f"Kullanici olusturulacak: {username} <{email}>.", args.yes):
        print("Iptal edildi.")
        return 1

    import asyncio as _asyncio

    from argon2 import PasswordHasher

    from src.services.credits import init_user_credits

    ph = PasswordHasher()
    hashed_pw = await _asyncio.to_thread(ph.hash, password)

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id FROM users WHERE username = %s OR lower(email) = %s",
            (username, email),
        )
        if await cur.fetchone() is not None:
            await db.release_current()
            print(f"HATA: '{username}' veya '{email}' zaten mevcut.")
            return 1
        try:
            await cur.execute(
                """INSERT INTO users (username, email, hashed_pw, email_verified)
                   VALUES (%s, %s, %s, TRUE) RETURNING id""",
                (username, email, hashed_pw),
            )
            row = await cur.fetchone()
            if row is None:
                await db.rollback()
                await db.release_current()
                print("HATA: Kullanici satiri dondurulemedi.")
                return 1
            user_id = row[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            await db.release_current()
            print(f"HATA: {e}")
            return 1
    await db.release_current()

    await init_user_credits(user_id)
    _out(
        args,
        {"username": username, "email": email, "id": user_id},
        f"Kullanici olusturuldu: {username} (id={user_id}) — baslangic kredisi verildi.",
    )
    return 0


# ---------------------------------------------------------------------------
# credits / announcement / maintenance / stats / export
# ---------------------------------------------------------------------------


async def credits_give(args: argparse.Namespace) -> int:
    username, amount, credit_type = args.username, args.amount, args.credit_type
    if not _confirm(f"'{username}' kullanicisina {amount} kredi ({credit_type}) verilecek.", args.yes):
        print("Iptal edildi.")
        return 1

    from src.services.credits import add_free_credits, add_gift_credits

    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT id FROM users WHERE username = %s", (username,))
        row = await cur.fetchone()
        if not row:
            await db.release_current()
            print(f"Kullanici bulunamadi: {username}")
            return 1
        user_id = row[0]
    await db.release_current()

    if credit_type == "gift":
        await add_gift_credits(user_id, amount)
    else:
        await add_free_credits(user_id, amount)
    _out(
        args,
        {"username": username, "amount": amount, "type": credit_type},
        f"{username} kullanicisina {amount} kredi eklendi ({credit_type}).",
    )
    return 0


async def announcement_add(args: argparse.Namespace) -> int:
    title, content = args.title, args.content
    if not _confirm(f"Duyuru eklenecek: '{title}'.", args.yes):
        print("Iptal edildi.")
        return 1

    from src.services.announcement import create_announcement

    ann = await create_announcement(title, content, sent_by=None)
    if ann is None:
        print("Duyuru olusturulamadi.")
        return 1
    _out(args, {"id": ann.id, "title": ann.title}, f"Duyuru eklendi (id={ann.id}): {ann.title}")
    return 0


async def maintenance_toggle(args: argparse.Namespace) -> int:
    feature, action = args.feature, args.action
    if not _confirm(f"Bakim modu: '{feature}' -> {action}.", args.yes):
        print("Iptal edildi.")
        return 1

    from src.services.maintenance import toggle

    try:
        result = await toggle(feature, action)
    except Exception as e:
        print(f"HATA: {e}")
        return 1
    _out(
        args,
        result,
        f"Maintenance: {result['feature']} {'devre disi' if result['disabled'] else 'aktif'}.",
    )
    return 0


async def stats(args: argparse.Namespace) -> int:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT COUNT(*) FROM users")
        users = (await cur.fetchone())[0]
        await cur.execute("SELECT COUNT(*) FROM reports")
        reports = (await cur.fetchone())[0]
        await cur.execute("SELECT COUNT(*) FROM simulations")
        simulations = (await cur.fetchone())[0]
        await cur.execute("SELECT COUNT(*) FROM announcements")
        announcements = (await cur.fetchone())[0]
    await db.release_current()

    token_summary = await get_token_summary()

    payload = {
        "users": users,
        "reports": reports,
        "simulations": simulations,
        "announcements": announcements,
        "token_usage": token_summary,
    }
    _out(
        args,
        payload,
        f"Kullanicilar: {users}",
        f"Raporlar: {reports}",
        f"Simulasyonlar: {simulations}",
        f"Duyurular: {announcements}",
        f"Token kullanim: {token_summary}",
    )
    return 0


async def export_stats(args: argparse.Namespace) -> int:
    """Veri disa aktarim istatistikleri (exports tablosu)."""
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT COUNT(*) FROM exports")
        row = await cur.fetchone()
        total = row[0] if row else 0

        await cur.execute(
            "SELECT status, COUNT(*) FROM exports GROUP BY status ORDER BY COUNT(*) DESC"
        )
        status_rows = await cur.fetchall()

        await cur.execute(
            "SELECT year, COUNT(*) FROM exports GROUP BY year ORDER BY year"
        )
        year_rows = await cur.fetchall()

        await cur.execute(
            "SELECT COALESCE(SUM(row_count), 0), COALESCE(SUM(size_bytes), 0), "
            "COALESCE(SUM(downloaded_count), 0) FROM exports"
        )
        sums = await cur.fetchone()
        total_rows, total_bytes, total_downloads = (sums[0], sums[1], sums[2]) if sums else (0, 0, 0)

        await cur.execute("SELECT COUNT(*) FROM exports WHERE status IN ('ready', 'sent')")
        succ_row = await cur.fetchone()
        succeeded = succ_row[0] if succ_row else 0

        await cur.execute(
            "SELECT u.username, COUNT(e.id) AS n FROM exports e "
            "JOIN users u ON u.id = e.user_id "
            "GROUP BY u.username ORDER BY n DESC LIMIT 5"
        )
        top_users = await cur.fetchall()
    await db.release_current()

    success_rate = (100.0 * succeeded / total) if total else None
    payload = {
        "total": total,
        "by_status": {s: n for s, n in status_rows},
        "by_year": {y: n for y, n in year_rows},
        "total_rows": total_rows,
        "total_bytes": total_bytes,
        "total_downloads": total_downloads,
        "success_rate_pct": success_rate,
        "top_users": [{"username": u, "exports": n} for u, n in top_users],
    }

    if getattr(args, "json", False):
        print(json_module.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=== Export istatistikleri ===")
    print(f"Toplam export: {total}")
    if status_rows:
        print("Durum dagilimi:")
        for status_name, n in status_rows:
            print(f"  {status_name}: {n}")
    if year_rows:
        print("Yil dagilimi:")
        for year, n in year_rows:
            print(f"  {year}: {n}")
    print(f"Toplam satir: {total_rows}")
    print(f"Toplam boyut: {total_bytes} bayt ({total_bytes / 1024 / 1024:.1f} MiB)")
    print(f"Toplam indirme: {total_downloads}")
    print(f"Basarı oranı (ready+sent): {success_rate:.1f}%" if success_rate is not None else "Basarı oranı: - (export yok)")
    if top_users:
        print("En aktif 5 kullanici:")
        for username, n in top_users:
            print(f"  {username}: {n} export")
    return 0


# ---------------------------------------------------------------------------
# llm: ortak yardimcilar (roster/anahtar cozumleme)
# ---------------------------------------------------------------------------


async def _resolve_decrypted_key(provider_id: str) -> str | None:
    """Saglayicinin sifreli anahtarini coze cozer; yoksa/cozulmezse ``None``.

    Cozulemeyen bir anahtar sessizce yutulmaz -- stderr'e [UYARI] basilir,
    ama cagiran taraf (roster/test akislari) ``None``'i "anahtarsiz dene"
    olarak yorumlayabilsin diye istisna FIRLATILMAZ.
    """
    row = await get_provider_row(provider_id)
    if row is None or not row.get("api_key_encrypted"):
        return None
    try:
        return crypto.decrypt(bytes(row["api_key_encrypted"]), aad=provider_id)
    except crypto.LLMCryptoError as exc:
        print(f"[UYARI] '{provider_id}' anahtari cozulemedi: {exc}", file=sys.stderr)
        return None


async def _fetch_live_models(provider_id: str, models_url: str, api_key: str | None) -> list[str] | None:
    """Saglayicinin canli model roster'ini ceker. Basarisizsa ``None`` doner.

    Iki bilinen liste sekli desteklenir: OpenAI-uyumlu ``{"data": [{"id": ...}]}``
    ve Ollama'nin yerel ``{"models": [{"name": ...}]}`` sekli (bkz.
    ``ollama-local`` katalog girdisi, ``models_url`` = ``/api/tags``).
    """
    spec = PROVIDERS[provider_id]
    headers: dict[str, str] = {}
    if api_key:
        if spec.api_style == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    try:
        client = await get_client()
        response = await client.get(models_url, headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        print(f"[UYARI] '{provider_id}' roster istegi basarisiz: {exc}", file=sys.stderr)
        return None

    if response.status_code >= 400:
        print(
            f"[UYARI] '{provider_id}' roster istegi {response.status_code} dondu: "
            f"{response.text[:200]}",
            file=sys.stderr,
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        print(f"[UYARI] '{provider_id}' roster yaniti JSON degil.", file=sys.stderr)
        return None

    items = payload.get("data")
    if items is None:
        items = payload.get("models")
    if not isinstance(items, list):
        print(f"[UYARI] '{provider_id}' roster yaniti beklenmeyen bicimde.", file=sys.stderr)
        return None

    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
            if model_id:
                ids.append(model_id)
    return ids


async def _last_token_usage_row(purpose: str) -> dict | None:
    async with db.cursor() as cur:
        await cur.execute(
            "SELECT provider, model, status, error, duration_ms, created_at "
            "FROM token_usage WHERE purpose = %s ORDER BY created_at DESC LIMIT 1",
            (purpose,),
        )
        return await cur.fetchone()


# ---------------------------------------------------------------------------
# llm providers / provider set|rm / models
# ---------------------------------------------------------------------------


async def llm_providers_list(args: argparse.Namespace) -> int:
    rows = {row["provider"]: row for row in await list_providers()}

    entries = []
    for provider_id in sorted(PROVIDERS):
        spec = PROVIDERS[provider_id]
        row = rows.get(provider_id)
        has_key = bool(row and row.get("api_key_encrypted"))
        decrypts_ok: bool | None = None
        masked = "(anahtar yok)"
        if has_key:
            try:
                plaintext = crypto.decrypt(bytes(row["api_key_encrypted"]), aad=provider_id)
                decrypts_ok = True
                masked = mask_secret(plaintext)
            except crypto.LLMCryptoError:
                decrypts_ok = False
                masked = "(COZULEMIYOR)"
        resolved_base_url = spec.base_url or (row.get("base_url") if row else None) or "-"
        entries.append({
            "provider": provider_id,
            "api_style": spec.api_style,
            "base_url": resolved_base_url,
            "keyless_ok": provider_id in KEYLESS_PROVIDERS,
            "has_key": has_key,
            "decrypts_ok": decrypts_ok,
            "masked_key": masked,
            "enabled": bool(row["enabled"]) if row else None,
            "verified": spec.verified,
        })

    if getattr(args, "json", False):
        print(json_module.dumps(entries, ensure_ascii=False, indent=2, default=str))
        return 0

    table_rows = [
        (
            e["provider"], e["base_url"], e["masked_key"],
            "-" if e["enabled"] is None else ("evet" if e["enabled"] else "hayir"),
            "evet" if e["verified"] else "hayir",
        )
        for e in entries
    ]
    _print_table(("provider", "base_url", "anahtar", "enabled", "verified"), table_rows)
    return 0


async def llm_provider_set(args: argparse.Namespace) -> int:
    provider_id = args.provider
    if provider_id not in PROVIDERS:
        print(f"HATA: bilinmeyen saglayici: {provider_id!r} (katalogda yok: {sorted(PROVIDERS)})")
        return 1
    spec = PROVIDERS[provider_id]
    base_url = args.base_url

    if base_url and provider_id != "openai-compatible":
        print(
            f"[UYARI] '{provider_id}' icin base_url sabittir ({spec.base_url}); "
            f"girilen deger DIKKATE ALINMAYACAK."
        )
    if provider_id == "openai-compatible" and not base_url:
        existing = await get_provider_row(provider_id)
        if not existing or not existing.get("base_url"):
            print("HATA: 'openai-compatible' icin --base-url zorunlu (henuz kayitli bir URL yok).")
            return 1

    secret = _read_secret_optional(
        f"'{provider_id}' icin API anahtari (bos birakip Enter'a basarsaniz mevcut anahtar korunur): "
    )

    what = "yeni anahtar + ayarlar" if secret else "sadece ayarlar (anahtar degismeyecek)"
    if not _confirm(f"'{provider_id}' saglayicisi icin {what} yazilacak.", args.yes):
        print("Iptal edildi.")
        return 1

    await upsert_provider(provider_id, api_key=secret, base_url=base_url, enabled=True)

    row = await get_provider_row(provider_id)
    masked = "(anahtar yok)"
    if row and row.get("api_key_encrypted"):
        try:
            masked = mask_secret(crypto.decrypt(bytes(row["api_key_encrypted"]), aad=provider_id))
        except crypto.LLMCryptoError:
            masked = "(COZULEMIYOR)"
    resolved_base_url = (row.get("base_url") if row else None) or spec.base_url or "-"
    _out(
        args,
        {"provider": provider_id, "masked_key": masked, "base_url": resolved_base_url},
        f"Saglayici guncellendi: {provider_id} (anahtar: {masked}, base_url: {resolved_base_url})",
    )
    return 0


async def llm_provider_rm(args: argparse.Namespace) -> int:
    provider_id = args.provider
    if not _confirm(f"'{provider_id}' saglayicisi TAMAMEN silinecek (anahtar dahil).", args.yes):
        print("Iptal edildi.")
        return 1
    try:
        await remove_provider(provider_id)
    except psycopg.errors.ForeignKeyViolation:
        print(
            f"HATA: '{provider_id}' su an TEK model ayarinin (digest+report paylasir) "
            f"saglayicisi; once 'llm model set <baska-saglayici>/<model>' ile secimi baska "
            f"bir saglayiciya tasiyin."
        )
        return 1
    _out(args, {"provider": provider_id, "removed": True}, f"Saglayici silindi: {provider_id}")
    return 0


async def llm_models(args: argparse.Namespace) -> int:
    provider_id = args.provider
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        print(f"HATA: bilinmeyen saglayici: {provider_id!r} (katalogda yok: {sorted(PROVIDERS)})")
        return 1
    if not spec.models_url:
        print(f"HATA: '{provider_id}' icin canli roster ucnoktasi tanimli degil (models_url yok).")
        return 1

    api_key = await _resolve_decrypted_key(provider_id)
    ids = await _fetch_live_models(provider_id, spec.models_url, api_key)
    if ids is None:
        print("HATA: roster'a erisilemedi (detay icin yukariya bakin).")
        return 1

    # NOT: saglayici API'lerinin cogunda "ucretsiz" diye bir alan yok --
    # ``--free`` model id'sinde 'free' alt-dizesi arayan bir SEZGI (bkz.
    # opencode-zen/opencode-go roster'i: id'ler acikca '-free' ile bitiyor).
    # Baska saglayicilarda bu sezgi yanlis-negatif/pozitif verebilir.
    if args.free:
        ids = [i for i in ids if "free" in i.lower()]

    if getattr(args, "json", False):
        print(json_module.dumps({"provider": provider_id, "models": ids}, ensure_ascii=False, indent=2))
        return 0

    print(f"{provider_id}: {len(ids)} model" + (" (--free filtreli)" if args.free else ""))
    for model_id in ids:
        print(f"  {model_id}")
    return 0


# ---------------------------------------------------------------------------
# llm model show / model set / test
# ---------------------------------------------------------------------------
#
# REFACTOR_PLAN.md Adim 6.5: amac-basina ``llm show``/``llm set`` kalkti.
# Ayar TEK (``llm_settings`` singleton) -- digest VE report AYNI saglayici/
# modeli kullanir. Amaca gore secim yapan bir CLI bayragi HICBIR YERDE yok. Gozlemlenebilirlik
# (son cagri durumu) AYRI bir eksen olarak ``token_usage.purpose`` uzerinden
# amac basina KALIR -- ``llm_model_show`` bunu asagida hala amac basina
# gosterir, ama YAZDIGI/OKUDUGU AYAR tek.


async def llm_model_show(args: argparse.Namespace) -> int:
    resolved = await resolve_llm()
    if isinstance(resolved, ResolvedLLM):
        entry = {
            "configured": True,
            "provider": resolved.provider.id,
            "model": resolved.model,
            "base_url": resolved.base_url,
            "api_key": mask_secret(resolved.api_key) if resolved.api_key else "(anahtar yok/gerekmiyor)",
            "params": resolved.params,
        }
    else:
        assert isinstance(resolved, Unconfigured)
        entry = {"configured": False, "reason": resolved.reason}

    # Son cagri durumu gozlemlenebilirlik ekseninde amac basina kalir --
    # token_usage.purpose digest/report'u ayirt etmeye devam ediyor.
    entry["last_calls"] = {purpose: await _last_token_usage_row(purpose) for purpose in PURPOSES}

    if getattr(args, "json", False):
        print(json_module.dumps(entry, ensure_ascii=False, indent=2, default=str))
        return 0

    if entry["configured"]:
        print(f"saglayici/model : {entry['provider']}/{entry['model']}")
        print(f"base_url        : {entry['base_url']}")
        print(f"anahtar         : {entry['api_key']}")
        print(f"params          : {entry['params']}")
    else:
        print(f"YAPILANDIRILMAMIS: {entry['reason']}")
    print()
    for purpose, last in entry["last_calls"].items():
        if last:
            print(
                f"son cagri ({purpose}) : {last['created_at']} status={last['status']} "
                f"provider={last['provider']} model={last['model']} "
                f"duration_ms={last['duration_ms']}"
                + (f" error={last['error']}" if last.get("error") else "")
            )
        else:
            print(f"son cagri ({purpose}) : (hic cagri yok)")
    return 0


async def llm_model_set(args: argparse.Namespace) -> int:
    """TEK saglayici+model ayarini yazar -- yazmadan ONCE bes dogrulama.

    Sira (REFACTOR_PLAN.md 2.6, Adim 6.5): (1) saglayici katalogda mi,
    (2) anahtar var/cozulebiliyor mu (keyless saglayicilarda atlanir),
    (3) model canli roster'da mi, (4) reasoning degeri saglayicinin kabul
    ettigi kumede mi, (5) reasoning aciliyorsa -- yapilandirilmis cikti
    kullanan amaclardan EN AZ BIRI (bugun: digest VE report ikisi de) bunu
    paylasacagi icin uyar (engelleme). 2026-08-26 arizasi 3. maddede
    takilirdi -- bu yuzden 3. madde ``--force`` ile SADECE "roster'a
    erisilemedi" durumunda atlanabilir, "model roster'da yok" durumunda
    ASLA atlanamaz.

    Amac-basina degil TEK bir yazma -- ``args.spec`` ``"saglayici/model"``
    bicimindedir (``src.llm.providers.resolve`` ile ayristirilir, model adi
    kendi icinde "/" icerebilir -- ornegin OpenRouter -- bu yuzden yalniz
    ILK "/" ayrac).
    """
    try:
        parsed = resolve_spec(args.spec)
    except InvalidModelSpec as exc:
        print(f"HATA: {exc}")
        return 1
    provider_id, model = parsed.provider.id, parsed.model
    spec = parsed.provider

    # 2) anahtar var mi / cozulebiliyor mu (keyless saglayicilar icin anahtar
    # sartı atlanir -- AMA llm_providers'ta bir SATIR yine de sart: llm_settings.
    # provider bu tabloya FK ile bagli (bkz. src/llm/settings.py::set_selection
    # docstring'i), anahtarsiz saglayicilar bile en azindan bos-anahtarli bir
    # satirla "llm provider set <id>" ile once kayda gecirilmis olmali.
    row = await get_provider_row(provider_id)
    if provider_id in KEYLESS_PROVIDERS:
        if row is None:
            print(
                f"HATA: '{provider_id}' icin llm_providers'ta kayit yok (anahtar gerekmese de "
                f"satir gerekli -- llm_settings buna FK ile bagli). Once: llm provider set {provider_id}"
            )
            return 1
    else:
        if row is None or not row.get("api_key_encrypted"):
            print(
                f"HATA: '{provider_id}' icin kayitli bir API anahtari yok. "
                f"Once: llm provider set {provider_id}"
            )
            return 1
        try:
            crypto.decrypt(bytes(row["api_key_encrypted"]), aad=provider_id)
        except crypto.LLMCryptoError as exc:
            print(f"HATA: '{provider_id}' icin API anahtari cozulemiyor: {exc}")
            return 1

    # 3) model canli roster'da mi
    if spec.models_url is None:
        if not args.force:
            print(
                f"HATA: '{provider_id}' icin canli roster ucnoktasi yok; model dogrulanamiyor. "
                f"Bilerek devam etmek icin --force kullanin."
            )
            return 1
        print(f"[UYARI] '{provider_id}' icin roster ucnoktasi yok; model DOGRULANMADAN yaziliyor (--force).")
    else:
        api_key = await _resolve_decrypted_key(provider_id)
        ids = await _fetch_live_models(provider_id, spec.models_url, api_key)
        if ids is None:
            if not args.force:
                print(
                    "HATA: roster'a erisilemedi, model dogrulanamadi. Sessizce atlamak YOK -- "
                    "bilerek devam etmek icin --force kullanin."
                )
                return 1
            print("[UYARI] roster'a erisilemedi; --force ile DOGRULANMADAN devam ediliyor.")
        elif model not in ids:
            print(
                f"HATA: '{model}' su an '{provider_id}' canli roster'inda YOK ({len(ids)} model "
                f"bulundu). Bu, 2026-08-26 arizasinin tam olarak yakalanmasi gereken durumdur -- "
                f"--force ile atlanamaz. Roster'i gormek icin: llm models {provider_id}"
            )
            return 1

    # 4) reasoning degeri
    params: dict = {}
    if args.reasoning is not None:
        if spec.reasoning_param is None:
            print(f"HATA: '{provider_id}' saglayicisinin reasoning_param'i yok; reasoning verilemez.")
            return 1
        if spec.reasoning_values and args.reasoning not in spec.reasoning_values:
            print(
                f"HATA: reasoning={args.reasoning!r} '{provider_id}' icin gecerli degil "
                f"(kabul edilen: {sorted(spec.reasoning_values)})."
            )
            return 1
        params["reasoning"] = args.reasoning

        # 5) yapilandirilmis ciktida reasoning -- UYARI, engel degil (admin
        # override boyle uygulandi, bkz. src/llm/agents.py). Ayar TEK ve
        # PURPOSES icindeki amaclarin tumu (bugun: digest, report) bunu
        # paylasacagi icin, bunlardan en az biri yapilandirilmis cikti
        # kullaniyorsa uyar -- amac-basina kosul YOK (Adim 6.5).
        if any(structured_output_forbids_reasoning(p) for p in PURPOSES):
            print(
                f"[UYARI] tek model ayari {', '.join(PURPOSES)} tarafindan PAYLASILIYOR; "
                f"bunlardan en az biri yapilandirilmis cikti kullaniyor (reasoning normalde "
                f"kapali). Bilerek admin override olarak aciliyorsunuz."
            )

    if args.timeout is not None:
        params["timeout"] = args.timeout

    if not _confirm(
        f"Tek model ayari '{provider_id}/{model}' olarak yazilacak "
        f"({', '.join(PURPOSES)} bunu kullanacak).",
        args.yes,
    ):
        print("Iptal edildi.")
        return 1

    await set_selection(provider_id, model, params=params, updated_by=os.getenv("USER") or "admin_cli")
    _out(
        args,
        {"provider": provider_id, "model": model, "params": params},
        f"Ayarlandi: {provider_id}/{model} (params={params})",
    )
    return 0


async def llm_test(args: argparse.Namespace) -> int:
    """Gercek bir LLM cagrisi yapar -- admin_cli'daki AG KULLANAN TEK komut budur.

    ``llm models``/``llm set``'in 3. dogrulamasi da ag kullanir ama yalniz
    ucretsiz bir GET /models istegi atar; burasi gercek bir tamamlama
    (completion) istegi olusturur, dolayisiyla saglayicidan token/kota
    harcar.
    """
    purpose = args.purpose
    print(
        "[NOT] Bu komut gercek bir LLM cagrisi yapar (saglayicidan token/kota harcar) -- "
        "admin_cli'daki ag kullanan TEK komut budur."
    )

    try:
        built = await build_agent(purpose)
    except LLMPurposeUnconfigured as exc:
        print(f"HATA: {exc}")
        return 1

    test_agent = PydanticAgent(model=built.model, model_settings=built.model_settings or None)
    start = time.monotonic()
    try:
        result = await test_agent.run("Bu bir baglanti testidir. Sadece 'ok' kelimesiyle yanit ver.")
    except Exception as exc:
        duration = elapsed_ms(start)
        await log_llm_call(
            purpose=purpose,
            model_name=built.model_name,
            provider_id=built.provider_id,
            status="error",
            duration_ms=duration,
            error=exc,
        )
        print(f"HATA ({duration}ms) {built.provider_id}/{built.model_name}: {type(exc).__name__}: {exc}")
        return 1

    duration = elapsed_ms(start)
    usage = result.usage
    prompt_tokens = usage.input_tokens or 0
    completion_tokens = usage.output_tokens or 0
    total_tokens = usage.total_tokens or 0

    await log_llm_call(
        purpose=purpose,
        model_name=built.model_name,
        provider_id=built.provider_id,
        status="ok",
        duration_ms=duration,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )

    _out(
        args,
        {
            "purpose": purpose,
            "provider": built.provider_id,
            "model": built.model_name,
            "duration_ms": duration,
            "output": str(result.output),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        f"OK ({duration}ms) {built.provider_id}/{built.model_name}: {result.output!r}",
        f"Token: prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}",
    )
    return 0


# ---------------------------------------------------------------------------
# llm usage / log / rotate-key
# ---------------------------------------------------------------------------


async def llm_usage(args: argparse.Namespace) -> int:
    since = _parse_since(args.since) if args.since else None
    summary = await get_token_summary(since=since, group_by=args.by)

    if getattr(args, "json", False):
        print(json_module.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"Toplam cagri     : {summary['call_count']}")
    print(f"Prompt token     : {summary['total_prompt_tokens']}")
    print(f"Completion token : {summary['total_completion_tokens']}")
    print(f"Toplam token     : {summary['total_tokens']}")
    if "breakdown" in summary:
        print(f"\nKirilim ({args.by}):")
        for row in summary["breakdown"]:
            value = row["value"] or "(bilinmiyor)"
            print(f"  {value}: {row['call_count']} cagri, {row['total_tokens']} token")
    return 0


async def llm_log(args: argparse.Namespace) -> int:
    conditions = []
    params: list = []
    if args.failures:
        conditions.append("status = 'error'")
    if args.since:
        conditions.append("created_at >= %s")
        params.append(_parse_since(args.since))
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with db.cursor() as cur:
        await cur.execute(
            f"""SELECT id, created_at, purpose, provider, model, status, duration_ms, error
                FROM token_usage {where}
                ORDER BY created_at DESC
                LIMIT %s""",
            (*params, args.limit),
        )
        rows = await cur.fetchall()
    await db.release_current()

    if getattr(args, "json", False):
        print(json_module.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0

    if not rows:
        print("(kayit yok)")
        return 0
    for row in rows:
        line = (
            f"[{row['created_at']}] purpose={row['purpose']} provider={row['provider']} "
            f"model={row['model']} status={row['status']} duration_ms={row['duration_ms']}"
        )
        if row.get("error"):
            line += f" error={row['error']}"
        print(line)
    return 0


async def llm_rotate_key(args: argparse.Namespace) -> int:
    """Tum ``llm_providers`` satirlarini yeni bir ana anahtarla yeniden sifreler.

    Kismi basarisizlik davranisi (bilincli tasarim karari, bkz. yorumlar
    asagida): FAZ 1 (eski anahtarla coz) tamamen bellekte yapilir, herhangi
    bir satir cozulemezse DB'ye HIC dokunulmadan durulur. FAZ 2 (yeni
    anahtarla yeniden sifrele + yaz) TEK bir DB transaction'inda yapilir --
    satir satir DEGIL. Yani sonuc ya HEPSI yeni anahtarla ya HICBIRI (eski
    anahtarla) -- iki anahtarla karisik bir DB durumu yapisal olarak
    olusmaz.
    """
    old_key = os.getenv("FLORENCE_MASTER_KEY")
    if not old_key:
        print("HATA: FLORENCE_MASTER_KEY tanimli degil (eski anahtar gerekli).")
        return 1

    new_key = _read_secret_optional("Yeni ana anahtar (base64, 32 bayt): ")
    if not new_key:
        print("HATA: yeni anahtar bos olamaz.")
        return 1
    try:
        raw = base64.b64decode(new_key, validate=True)
    except Exception:
        print("HATA: yeni anahtar gecerli base64 degil.")
        return 1
    if len(raw) != 32:
        print(f"HATA: yeni anahtar {len(raw)} bayta cozuluyor, 32 bayt bekleniyor.")
        return 1
    if new_key == old_key:
        print("HATA: yeni anahtar eski anahtarla ayni; rotasyonun bir anlami yok.")
        return 1

    if not _confirm(
        "TUM saglayici anahtarlari yeni ana anahtarla yeniden sifrelenecek. "
        "Bu geri alinamaz bir islemdir (rotasyon sonrasi FLORENCE_MASTER_KEY'i "
        "guncellemezseniz uygulama anahtarlari cozemez).",
        args.yes,
    ):
        print("Iptal edildi.")
        return 1

    rows = await list_providers()
    to_rotate = [row for row in rows if row.get("api_key_encrypted")]
    if not to_rotate:
        _out(args, {"rotated": 0}, "Sifrelenecek anahtar yok (hicbir saglayicida API anahtari kayitli degil).")
        return 0

    # FAZ 1: eski anahtarla TUMUNU coz (bellekte). Biri bile basarisiz olursa
    # DB'ye hic dokunulmadan durulur.
    decrypted: dict[str, str] = {}
    for row in to_rotate:
        pid = row["provider"]
        try:
            decrypted[pid] = crypto.decrypt(bytes(row["api_key_encrypted"]), aad=pid)
        except crypto.LLMCryptoError as exc:
            print(f"HATA: '{pid}' eski anahtarla cozulemedi, islem DURDURULDU (hicbir satir degismedi): {exc}")
            return 1

    # FAZ 2: yeni anahtarla yeniden sifrele + TEK transaction'da yaz.
    os.environ["FLORENCE_MASTER_KEY"] = new_key
    try:
        async with db.cursor(row_factory=None) as cur:
            for pid, plaintext in decrypted.items():
                new_blob = crypto.encrypt(plaintext, aad=pid)
                await cur.execute(
                    "UPDATE llm_providers SET api_key_encrypted = %s, updated_at = NOW() WHERE provider = %s",
                    (new_blob, pid),
                )
            await db.commit()
    except Exception as exc:
        await db.rollback()
        print(f"HATA: yeniden sifreleme basarisiz, TUM islem geri alindi (hicbir satir degismedi): {exc}")
        return 1
    finally:
        os.environ["FLORENCE_MASTER_KEY"] = old_key

    _out(
        args,
        {"rotated": len(decrypted), "providers": sorted(decrypted)},
        f"{len(decrypted)} saglayicinin anahtari yeni ana anahtarla yeniden sifrelendi.",
        "ONEMLI: FLORENCE_MASTER_KEY'i .env/secret yoneticisinde YENI anahtarla guncelleyin -- "
        "guncellemezseniz uygulama DB'deki anahtarlari artik COZEMEZ (cokme yok, ama "
        "resolve_llm ayari 'yapilandirilmamis' gosterir).",
    )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--admin-token", default=None, help="ADMIN_TOKEN ile eslesme")
    common.add_argument("--json", action="store_true", help="makine okunur JSON cikti")
    common.add_argument("--yes", action="store_true", help="yikici islemde onay istemini atla")

    parser = argparse.ArgumentParser(description="Florence admin CLI (dogrudan DB)", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    # users
    users_p = sub.add_parser("users", help="kullanici islemleri", parents=[common])
    users_sub = users_p.add_subparsers(dest="users_command", required=True)
    users_sub.add_parser("list", help="kullanicilari listele", parents=[common])
    create_p = users_sub.add_parser("create", help="kullanici olustur (baslangic kredisiyle)", parents=[common])
    create_p.add_argument("username")
    create_p.add_argument("email")
    create_p.add_argument("password")
    for cmd in ("freeze", "unfreeze"):
        p = users_sub.add_parser(cmd, help=f"kullaniciyi {cmd} et", parents=[common])
        p.add_argument("username")

    # credits
    credits_p = sub.add_parser("credits", help="kredi islemleri", parents=[common])
    credits_sub = credits_p.add_subparsers(dest="credits_command", required=True)
    give_p = credits_sub.add_parser("give", help="kredi ver", parents=[common])
    give_p.add_argument("username")
    give_p.add_argument("amount", type=float)
    give_p.add_argument("--type", dest="credit_type", choices=["gift", "free"], default="free")

    # announcement
    ann_p = sub.add_parser("announcement", help="duyuru islemleri", parents=[common])
    ann_sub = ann_p.add_subparsers(dest="announcement_command", required=True)
    add_p = ann_sub.add_parser("add", help="duyuru ekle", parents=[common])
    add_p.add_argument("title")
    add_p.add_argument("content")

    # maintenance
    maint_p = sub.add_parser("maintenance", help="ozellik bakim modu", parents=[common])
    maint_p.add_argument("feature", choices=["report_generate", "simulation", "news", "advisor"])
    maint_p.add_argument("action", choices=["enable", "disable"])

    # stats
    sub.add_parser("stats", help="istatistikler", parents=[common])

    # export
    export_p = sub.add_parser("export", help="veri disa aktarim islemleri", parents=[common])
    export_sub = export_p.add_subparsers(dest="export_command", required=True)
    export_sub.add_parser("stats", help="export istatistikleri", parents=[common])

    # llm
    llm_p = sub.add_parser("llm", help="LLM saglayici/model yonetimi", parents=[common])
    llm_sub = llm_p.add_subparsers(dest="llm_command", required=True)

    llm_sub.add_parser("providers", help="katalog + anahtar durumu", parents=[common])

    provider_p = llm_sub.add_parser("provider", help="saglayici anahtar/ayar yonetimi", parents=[common])
    provider_sub = provider_p.add_subparsers(dest="llm_provider_command", required=True)
    prov_set_p = provider_sub.add_parser("set", help="anahtar/base_url ayarla (anahtar stdin'den)", parents=[common])
    prov_set_p.add_argument("provider")
    prov_set_p.add_argument("--base-url", default=None, help="yalniz openai-compatible icin anlamli")
    prov_rm_p = provider_sub.add_parser("rm", help="saglayici satirini sil", parents=[common])
    prov_rm_p.add_argument("provider")

    models_p = llm_sub.add_parser("models", help="canli model roster'i", parents=[common])
    models_p.add_argument("provider")
    models_p.add_argument("--free", action="store_true", help="yalniz id'sinde 'free' gecen modeller (sezgisel)")

    # llm model: TEK ayar (REFACTOR_PLAN.md Adim 6.5) -- amac-basina "llm
    # show"/"llm set" kalkti, amaca gore secim yapan bir CLI bayragi HICBIR
    # YERDE yok.
    model_p = llm_sub.add_parser("model", help="tek saglayici/model ayari (digest+report paylasir)", parents=[common])
    model_sub = model_p.add_subparsers(dest="llm_model_command", required=True)

    model_sub.add_parser("show", help="mevcut tek ayar + amac basina son cagri durumu", parents=[common])

    model_set_p = model_sub.add_parser("set", help="tek ayari yaz (5 dogrulama)", parents=[common])
    model_set_p.add_argument("spec", metavar="provider/model", help="ornek: opencode-zen/deepseek-v4-flash-free")
    model_set_p.add_argument("--reasoning", default=None, help="ornek: low/medium/high (saglayiciya gore)")
    model_set_p.add_argument("--timeout", type=float, default=None, help="istek zaman asimi (saniye)")
    model_set_p.add_argument(
        "--force", action="store_true",
        help="roster'a ERISILEMEDIGINDE devam et (roster'da model YOKSA bu bayrak atlamaya YETMEZ)",
    )

    test_p = llm_sub.add_parser("test", help="gercek bir LLM cagrisi yapar (ag kullanir)", parents=[common])
    test_p.add_argument("purpose", choices=PURPOSES)

    usage_p = llm_sub.add_parser("usage", help="token kullanim ozeti", parents=[common])
    usage_p.add_argument("--since", default=None, help="ornek: 24h, 7d, 2026-08-20")
    usage_p.add_argument("--by", dest="by", default=None, choices=["provider", "model", "purpose"])

    log_p = llm_sub.add_parser("log", help="son LLM cagrilari", parents=[common])
    log_p.add_argument("--failures", action="store_true", help="yalniz basarisiz cagrilar")
    log_p.add_argument("--since", default=None, help="ornek: 24h, 7d, 2026-08-20")
    log_p.add_argument("--limit", type=int, default=20)

    llm_sub.add_parser("rotate-key", help="tum saglayici anahtarlarini yeni ana anahtarla yeniden sifreler", parents=[common])

    return parser


async def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "users":
        if args.users_command == "list":
            return await _dispatch(users_list, args, destructive=False)
        if args.users_command == "create":
            return await _dispatch(users_create, args, destructive=True)
        frozen = args.users_command == "freeze"
        handler = (lambda a: users_set_frozen(a, frozen=frozen))
        return await _dispatch(handler, args, destructive=True)

    if args.command == "credits":
        return await _dispatch(credits_give, args, destructive=True)

    if args.command == "announcement":
        return await _dispatch(announcement_add, args, destructive=True)

    if args.command == "maintenance":
        return await _dispatch(maintenance_toggle, args, destructive=True)

    if args.command == "stats":
        return await _dispatch(stats, args, destructive=False)

    if args.command == "export":
        if args.export_command == "stats":
            return await _dispatch(export_stats, args, destructive=False)

    if args.command == "llm":
        if args.llm_command == "providers":
            return await _dispatch(llm_providers_list, args, destructive=False)
        if args.llm_command == "provider":
            if args.llm_provider_command == "set":
                return await _dispatch(llm_provider_set, args, destructive=True)
            if args.llm_provider_command == "rm":
                return await _dispatch(llm_provider_rm, args, destructive=True)
        if args.llm_command == "models":
            return await _dispatch(llm_models, args, destructive=False)
        if args.llm_command == "model":
            if args.llm_model_command == "show":
                return await _dispatch(llm_model_show, args, destructive=False)
            if args.llm_model_command == "set":
                return await _dispatch(llm_model_set, args, destructive=True)
        if args.llm_command == "test":
            return await _dispatch(llm_test, args, destructive=True)
        if args.llm_command == "usage":
            return await _dispatch(llm_usage, args, destructive=False)
        if args.llm_command == "log":
            return await _dispatch(llm_log, args, destructive=False)
        if args.llm_command == "rotate-key":
            return await _dispatch(llm_rotate_key, args, destructive=True)

    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (psycopg.OperationalError, psycopg.InterfaceError, OSError, TimeoutError) as e:
        # DB baglanti hatalarini traceback yerine tek satirlik mesajla goster.
        print(f"Bağlantı hatası: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nIptal edildi.")
        sys.exit(130)
