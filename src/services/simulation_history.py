import json
from src.core.database import db


def save_simulation(
    user_id: int,
    ticker: str,
    days: int,
    bounds: str,
    target: str | None,
    result: dict,
    cost: float,
) -> int | None:
    with db.cursor() as cur:
        try:
            cur.execute("""
                INSERT INTO simulations (user_id, ticker, days, bounds, target, result, cost)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, ticker, days, bounds, target, json.dumps(result, ensure_ascii=False), cost))
            row = cur.fetchone()
            db.commit()
            return row[0] if row else None
        except Exception:
            db.rollback()
            return None


def get_simulation_history(
    user_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    with db.cursor() as cur:
        try:
            cur.execute("""
                SELECT id, ticker, days, bounds, target, cost, created_at
                FROM simulations
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset))
            rows = cur.fetchall()
        except Exception:
            return []

    return [
        {
            "id": r[0],
            "ticker": r[1],
            "days": r[2],
            "bounds": r[3],
            "target": r[4],
            "cost": float(r[5]) if r[5] is not None else None,
            "created_at": r[6].isoformat(),
        }
        for r in rows
    ]


def get_simulation_detail(
    user_id: int,
    sim_id: int,
) -> dict | None:
    with db.cursor() as cur:
        try:
            cur.execute("""
                SELECT id, ticker, days, bounds, target, result, cost, created_at
                FROM simulations
                WHERE id = %s AND user_id = %s
            """, (sim_id, user_id))
            row = cur.fetchone()
        except Exception:
            return None

    if not row:
        return None

    result_raw = row[5]
    if isinstance(result_raw, str):
        result_raw = json.loads(result_raw) if result_raw else {}

    return {
        "id": row[0],
        "ticker": row[1],
        "days": row[2],
        "bounds": row[3],
        "target": row[4],
        "result": result_raw,
        "cost": float(row[6]) if row[6] is not None else None,
        "created_at": row[7].isoformat(),
    }
