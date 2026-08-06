import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from src.core.database import db


def refresh_token_ttl_days() -> int:
    try:
        return int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "30"))
    except ValueError:
        return 30


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_refresh_token(user_id: int, device: str | None = None) -> str:
    token = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=refresh_token_ttl_days())
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO refresh_tokens (user_id, token_hash, device, expires_at)
               VALUES (%s, %s, %s, %s)""",
            (user_id, hash_token(token), device, expires_at),
        )
        db.commit()
    return token


def _user_id_for_valid_token(token: str, cur) -> int | None:
    cur.execute(
        """SELECT user_id FROM refresh_tokens
           WHERE token_hash = %s
             AND revoked_at IS NULL
             AND expires_at > NOW()""",
        (hash_token(token),),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_user_for_token(token: str) -> int | None:
    if not token:
        return None
    with db.cursor() as cur:
        return _user_id_for_valid_token(token, cur)


def revoke_token(token: str) -> bool:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
            (hash_token(token),),
        )
        db.commit()
        return cur.rowcount > 0


def revoke_all_user_tokens(user_id: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        db.commit()
        return cur.rowcount


def rotate_token(token: str) -> tuple[str, int] | None:
    if not token:
        return None
    with db.cursor() as cur:
        user_id = _user_id_for_valid_token(token, cur)
        if user_id is None:
            return None
        cur.execute(
            "UPDATE refresh_tokens SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL",
            (hash_token(token),),
        )
        new_token = generate_refresh_token()
        expires_at = datetime.now(timezone.utc) + timedelta(days=refresh_token_ttl_days())
        cur.execute(
            """INSERT INTO refresh_tokens (user_id, token_hash, device, expires_at)
               VALUES (%s, %s, NULL, %s)""",
            (user_id, hash_token(new_token), expires_at),
        )
        db.commit()
    return new_token, user_id
