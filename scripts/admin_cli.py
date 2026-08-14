#!/usr/bin/env python3
"""Florence admin CLI — dogrudan DB/Redis uzerinden yonetim islemleri.

Kullanim:
    python scripts/admin_cli.py users list
    python scripts/admin_cli.py users freeze <username>
    python scripts/admin_cli.py users unfreeze <username>
    python scripts/admin_cli.py credits give <username> <amount> [--type gift|free]
    python scripts/admin_cli.py announcement add <title> <content>
    python scripts/admin_cli.py maintenance <feature> enable|disable
    python scripts/admin_cli.py stats

Guvenlik notu: komutlar API uzerinden DEGIL, dogrudan veritabanina yazar.
Ortamda ADMIN_TOKEN tanimliysa ve --admin-token verilirse eslesme zorunludur;
verilmezse uyari basiliir (opsiyonel koruma).
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg  # noqa: E402

from src.core.database import db  # noqa: E402
from src.core.redis import r  # noqa: E402

USERS_LIST_COLUMNS = ("id", "username", "email", "user_type", "created_at", "is_frozen", "credits")


def _verify_admin_token(args) -> None:
    """--admin-token verildiyse ADMIN_TOKEN ile eslestir (opsiyonel)."""
    env_token = os.getenv("ADMIN_TOKEN")
    if not env_token:
        print("[UYARI] ADMIN_TOKEN ortam degiskeni tanimli degil; token kontrolu yapilamadi.")
        return
    if args.admin_token and args.admin_token != env_token:
        print("HATA: --admin-token ADMIN_TOKEN ile eslesmiyor.")
        sys.exit(1)
    if not args.admin_token:
        print("[NOT] ADMIN_TOKEN tanimli; islem icin --admin-token verilebilir (opsiyonel).")


async def users_list() -> int:
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            SELECT u.id, u.username, u.email, u.user_type, u.created_at, u.is_frozen,
                   COALESCE((SELECT SUM(amount) FROM user_credits WHERE user_id = u.id), 0) AS credits
            FROM users u
            ORDER BY u.id
        """)
        rows = await cur.fetchall()
    await db.release_current()

    if not rows:
        print("Kullanici yok.")
        return 0

    header = " | ".join(USERS_LIST_COLUMNS)
    print(header)
    print("-" * len(header))
    for row in rows:
        created = row[4].isoformat() if row[4] else "-"
        print(" | ".join([
            str(row[0]), row[1], row[2] or "-", row[3], created,
            "frozen" if row[5] else "active", f"{row[6]:.2f}",
        ]))
    return 0


async def users_set_frozen(username: str, frozen: bool) -> int:
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
        # Frozen cache'ini temizle ki etki aninda olsun (best-effort).
        try:
            await r.delete(f"user:frozen:{row[0]}")
        except Exception:
            pass
    action = "donduruldu" if frozen else "cozuldu"
    print(f"Kullanici '{username}' {action}.")
    return 0


async def credits_give(username: str, amount: float, credit_type: str) -> int:
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
    print(f"{username} kullanicisina {amount} kredi eklendi ({credit_type}).")
    return 0


async def announcement_add(title: str, content: str) -> int:
    from src.services.announcement import create_announcement

    ann = await create_announcement(title, content, sent_by=None)
    if ann is None:
        print("Duyuru olusturulamadi.")
        return 1
    print(f"Duyuru eklendi (id={ann.id}): {ann.title}")
    return 0


async def maintenance_toggle(feature: str, action: str) -> int:
    from src.services.maintenance import toggle

    try:
        result = await toggle(feature, action)
    except Exception as e:
        print(f"HATA: {e}")
        return 1
    print(f"Maintenance: {result['feature']} {'devre disi' if result['disabled'] else 'aktif'}.")
    return 0


async def stats() -> int:
    from src.services.token import get_token_summary

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

    print(f"Kullanicilar: {users}")
    print(f"Raporlar: {reports}")
    print(f"Simulasyonlar: {simulations}")
    print(f"Duyurular: {announcements}")
    print("Token kullanim:", token_summary)
    return 0


async def export_stats() -> int:
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

    print("=== Export istatistikleri ===")
    print(f"Toplam export: {total}")
    if status_rows:
        print("Durum dagilimi:")
        for status, n in status_rows:
            print(f"  {status}: {n}")
    if year_rows:
        print("Yil dagilimi:")
        for year, n in year_rows:
            print(f"  {year}: {n}")
    print(f"Toplam satir: {total_rows}")
    print(f"Toplam boyut: {total_bytes} bayt ({total_bytes / 1024 / 1024:.1f} MiB)")
    print(f"Toplam indirme: {total_downloads}")
    if total:
        print(f"Basarı oranı (ready+sent): {100.0 * succeeded / total:.1f}%")
    else:
        print("Basarı oranı: - (export yok)")
    if top_users:
        print("En aktif 5 kullanici:")
        for username, n in top_users:
            print(f"  {username}: {n} export")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Florence admin CLI (dogrudan DB)")
    parser.add_argument("--admin-token", default=None, help="ADMIN_TOKEN ile eslesme (opsiyonel)")
    sub = parser.add_subparsers(dest="command", required=True)

    users_p = sub.add_parser("users", help="kullanici islemleri")
    users_sub = users_p.add_subparsers(dest="users_command", required=True)
    users_sub.add_parser("list", help="kullanicilari listele")
    for cmd in ("freeze", "unfreeze"):
        p = users_sub.add_parser(cmd, help=f"kullaniciyi {cmd} et")
        p.add_argument("username")

    credits_p = sub.add_parser("credits", help="kredi islemleri")
    credits_sub = credits_p.add_subparsers(dest="credits_command", required=True)
    give_p = credits_sub.add_parser("give", help="kredi ver")
    give_p.add_argument("username")
    give_p.add_argument("amount", type=float)
    give_p.add_argument("--type", dest="credit_type", choices=["gift", "free"], default="free")

    ann_p = sub.add_parser("announcement", help="duyuru islemleri")
    ann_sub = ann_p.add_subparsers(dest="announcement_command", required=True)
    add_p = ann_sub.add_parser("add", help="duyuru ekle")
    add_p.add_argument("title")
    add_p.add_argument("content")

    maint_p = sub.add_parser("maintenance", help="ozellik bakim modu")
    maint_p.add_argument("feature", choices=["report_generate", "simulation", "news", "advisor"])
    maint_p.add_argument("action", choices=["enable", "disable"])

    sub.add_parser("stats", help="istatistikler")

    export_p = sub.add_parser("export", help="veri disa aktarim islemleri")
    export_sub = export_p.add_subparsers(dest="export_command", required=True)
    export_sub.add_parser("stats", help="export istatistikleri")

    args = parser.parse_args()
    _verify_admin_token(args)

    if args.command == "users":
        if args.users_command == "list":
            return await users_list()
        frozen = args.users_command == "freeze"
        return await users_set_frozen(args.username, frozen)
    if args.command == "credits":
        return await credits_give(args.username, args.amount, args.credit_type)
    if args.command == "announcement":
        return await announcement_add(args.title, args.content)
    if args.command == "maintenance":
        return await maintenance_toggle(args.feature, args.action)
    if args.command == "stats":
        return await stats()
    if args.command == "export":
        if args.export_command == "stats":
            return await export_stats()
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
