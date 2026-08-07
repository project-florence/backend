import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.database import db


async def promote(username_or_email: str):
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "UPDATE users SET user_type = 'admin' WHERE username = %s OR email = %s",
            (username_or_email, username_or_email),
        )
        if cur.rowcount == 0:
            print(f"User '{username_or_email}' not found.")
        else:
            await db.commit()
            print(f"User '{username_or_email}' promoted to admin.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/promote_user.py <username_or_email>")
        sys.exit(1)
    asyncio.run(promote(sys.argv[1]))
