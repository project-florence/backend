"""
Bot kullanıcısı oluşturur.

Kullanım:
  python scripts/create_bot.py --username mybot
  python scripts/create_bot.py --username mybot --password gucluSifre123

Çıktı: oluşturulan kullanıcı adı ve şifre.
"""

import argparse
import asyncio
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from argon2 import PasswordHasher
from src.core.database import db

ph = PasswordHasher()


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


async def create_bot(username: str, password: str | None = None) -> str:
    if password is None:
        password = _random_password()

    email = f"{username}@bot.florence"
    hashed = await asyncio.to_thread(ph.hash, password)

    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT id FROM users WHERE username = %s OR email = %s",
            (username, email),
        )
        if await cur.fetchone():
            print(f"HATA: '{username}' kullanıcı adı zaten mevcut.")
            sys.exit(1)

        try:
            await cur.execute(
                "INSERT INTO users (username, email, hashed_pw, user_type) VALUES (%s, %s, %s, 'bot') RETURNING id",
                (username, email, hashed),
            )
            user_id = (await cur.fetchone())[0]
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"HATA: {e}")
            sys.exit(1)

    return password


async def main():
    parser = argparse.ArgumentParser(description="Bot kullanıcısı oluşturur")
    parser.add_argument("--username", required=True, help="Bot kullanıcı adı")
    parser.add_argument("--password", default=None, help="Şifre (belirtilmezse rastgele üretilir)")
    args = parser.parse_args()

    password = await create_bot(args.username, args.password)
    print(f"Bot oluşturuldu:")
    print(f"  Kullanıcı adı: {args.username}")
    print(f"  Şifre:         {password}")
    print(f"  Tür:           bot")


if __name__ == "__main__":
    asyncio.run(main())
