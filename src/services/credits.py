import os

from src.core.database import db


def _get_max_free() -> int:
    return int(os.getenv("FREE_CREDIT_MAX", "25"))


def _get_daily_refill() -> int:
    return int(os.getenv("DAILY_FREE_CREDIT_REFILL", "5"))


async def _resolve_owner(user_id: int) -> int:
    """Bot hesaplari owner'in kredisinden harcar.

    ``user_type='bot'`` olan kullanici icin ``owner_id``'yi, degilse ayni
    ``user_id``'yi dondurur. Tum kredi islemleri (spend/get_total/refund/...)
    girisinde bu cozumlemeyi yapar; boylece rapor/simulasyon akislari
    degismeden calisir.
    """
    async with db.cursor(row_factory=None) as cur:
        await cur.execute(
            "SELECT user_type, owner_id FROM users WHERE id = %s", (user_id,)
        )
        row = await cur.fetchone()
        if row is not None and row[0] == "bot" and row[1] is not None:
            return row[1]
    return user_id


async def get_total(user_id: int) -> float:
    user_id = await _resolve_owner(user_id)
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT COALESCE(SUM(amount), 0) FROM user_credits WHERE user_id = %s", (user_id,))
        return float((await cur.fetchone())[0])


async def spend(user_id: int, amount: float) -> tuple[bool, float]:
    user_id = await _resolve_owner(user_id)
    async with db.cursor(row_factory=None) as cur:
        for credit_type in ("free_credits", "gift_credits"):
            await cur.execute("""
                UPDATE user_credits
                SET amount = amount - %s
                WHERE user_id = %s AND credit_type = %s AND amount >= %s
                RETURNING amount
            """, (amount, user_id, credit_type, amount))
            row = await cur.fetchone()
            if row is not None:
                await db.commit()
                remaining = await get_total(user_id)
                return True, remaining
        await db.rollback()
        return False, await get_total(user_id)


async def refund(user_id: int, amount: float):
    user_id = await _resolve_owner(user_id)
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'free_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        await db.commit()


async def daily_refill() -> int:
    max_free = _get_max_free()
    refill_amount = _get_daily_refill()
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            SELECT id, 'free_credits', %s FROM users
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = LEAST(user_credits.amount + %s, %s)
        """, (refill_amount, refill_amount, max_free))
        rowcount = cur.rowcount
        await db.commit()
        return rowcount


async def add_free_credits(user_id: int, amount: float):
    user_id = await _resolve_owner(user_id)
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'free_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        await db.commit()


async def add_gift_credits(user_id: int, amount: float):
    user_id = await _resolve_owner(user_id)
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("""
            INSERT INTO user_credits (user_id, credit_type, amount)
            VALUES (%s, 'gift_credits', %s)
            ON CONFLICT (user_id, credit_type) DO UPDATE
            SET amount = user_credits.amount + %s
        """, (user_id, amount, amount))
        await db.commit()
