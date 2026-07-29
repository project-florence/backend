import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.database import db


def promote(username_or_email: str):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE users SET user_type = 'admin' WHERE username = %s OR email = %s",
            (username_or_email, username_or_email),
        )
        if cur.rowcount == 0:
            print(f"User '{username_or_email}' not found.")
        else:
            db.commit()
            print(f"User '{username_or_email}' promoted to admin.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/promote_user.py <username_or_email>")
        sys.exit(1)
    promote(sys.argv[1])
