from fastapi import APIRouter, Depends, HTTPException
from src.core.database import db
from src.services.stats import increment_stat
from src.api.deps import get_current_user, validate_ticker

router = APIRouter()


@router.post("/favorites/{ticker}")
def add_favorite(ticker: str, current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)
    with db.cursor() as cur:
        try:
            cur.execute("""
                INSERT INTO favorites (user_id, ticker_code)
                VALUES (%s, %s)
                ON CONFLICT (user_id, ticker_code) DO NOTHING
            """, (current_user_id, ticker))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=400, detail="Could not add to favorites")

    increment_stat(ticker, "favorite_count")
    return {"message": f"Added favorite {ticker} or already been added"}


@router.delete("/favorites/{ticker}")
def remove_favorite(ticker: str, current_user_id: int = Depends(get_current_user)):
    validate_ticker(ticker)

    with db.cursor() as cur:
        try:
            cur.execute("""
            DELETE FROM favorites
            WHERE user_id = %s AND ticker_code = %s
            """, (current_user_id, ticker))
            db.commit()
        except Exception as e:
            db.rollback()

    return {"message": f"Removed {ticker} from favorites"}


@router.get("/favorites")
def get_favorites(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                SELECT ticker_code FROM favorites WHERE user_id = %s
            """, (current_user_id,))
            rows = cur.fetchall()
            favorites_list = [row[0] for row in rows]
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return {"favorites": favorites_list}
