import json
from fastapi import APIRouter, Depends, HTTPException
from src.core.database import db
from src.api.deps import get_current_user

router = APIRouter()


@router.get("/user/export")
def export_user_data(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute(
                "SELECT username, email, credits FROM users WHERE id = %s",
                (current_user_id,),
            )
            profile_row = cur.fetchone()
            if not profile_row:
                raise HTTPException(status_code=404, detail="User not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    profile = {
        "username": profile_row[0],
        "email": profile_row[1],
        "credits": float(profile_row[2]),
    }

    favorites = []
    with db.cursor() as cur:
        try:
            cur.execute(
                "SELECT ticker_code, created_at FROM favorites WHERE user_id = %s ORDER BY created_at",
                (current_user_id,),
            )
            for row in cur.fetchall():
                favorites.append({
                    "ticker_code": row[0],
                    "created_at": row[1].isoformat(),
                })
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    reports = []
    with db.cursor() as cur:
        try:
            cur.execute(
                """SELECT id, ticker, type, title, token_usage, content, created_at
                   FROM reports WHERE user_id = %s
                   ORDER BY created_at DESC""",
                (current_user_id,),
            )
            for row in cur.fetchall():
                tu = row[4]
                if isinstance(tu, str):
                    tu = json.loads(tu) if tu else None
                reports.append({
                    "id": row[0],
                    "ticker": row[1],
                    "type": row[2],
                    "title": row[3],
                    "token_usage": tu,
                    "content": row[5],
                    "created_at": row[6].isoformat(),
                })
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    token_usage = []
    with db.cursor() as cur:
        try:
            cur.execute(
                """SELECT model, prompt_tokens, completion_tokens, total_tokens, endpoint, created_at
                   FROM token_usage WHERE user_id = %s
                   ORDER BY created_at DESC""",
                (current_user_id,),
            )
            for row in cur.fetchall():
                token_usage.append({
                    "model": row[0],
                    "prompt_tokens": row[1],
                    "completion_tokens": row[2],
                    "total_tokens": row[3],
                    "endpoint": row[4],
                    "created_at": row[5].isoformat(),
                })
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return {
        "profile": profile,
        "favorites": favorites,
        "reports": reports,
        "token_usage": token_usage,
    }
