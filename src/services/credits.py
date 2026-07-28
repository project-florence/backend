import os
from src.core.database import db


def _get_max_free() -> int:
    return int(os.getenv("FREE_CREDIT_MAX", "25"))


def _get_daily_refill() -> int:
    return int(os.getenv("DAILY_FREE_CREDIT_REFILL", "5"))


def get_total(user_id: int) -> float:
    with db.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM user_credits WHERE user_id = %s", (user_id,))
        return float(cur.fetchone()[0])


def spend(user_id: int, amount: float) -> tuple[bool, float]:
    with db.cursor() as cur:
        for credit_type in ("free_credits", "gift_credits"):
            cur.execute("""
                UPDATE user_credits
                SET amount = amount - %s
                WHERE user_id = %s AND credit_type = %s AND amount >= %s
                RETURNING amount
            """, (amount, user_id, credit_type, amount))
            row = cur.fetchone()
            if row is not None:
                db.commit()
                remaining = get_total(user_id)
                return True, remaining
        db.rollback()
        return False, get_total(user_id)


def refund(user_id: int, amount: float):
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'free_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        db.commit()


def daily_refill() -> int:
    max_free = _get_max_free()
    refill_amount = _get_daily_refill()
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            SELECT id, 'free_credits', %s FROM users
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = LEAST(user_credits.amount + %s, %s)
        """, (refill_amount, refill_amount, max_free))
        db.commit()
        return cur.rowcount


def add_free_credits(user_id: int, amount: float):
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'free_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        db.commit()


def add_gift_credits(user_id: int, amount: float):
    with db.cursor() as cur:
        cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'gift_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        db.commit()
