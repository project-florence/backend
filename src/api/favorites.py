from fastapi import APIRouter, Depends, HTTPException
from src.core.database import db
from src.services.stats import increment_stat
from src.services.analytics import track_event
from src.api.deps import get_current_user, validate_ticker

router = APIRouter()


@router.post("/favorites/{ticker}")
async def add_favorite(ticker: str, current_user_id: int = Depends(get_current_user)):
    await validate_ticker(ticker)
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
                INSERT INTO favorites (user_id, ticker_code)
                VALUES (%s, %s)
                ON CONFLICT (user_id, ticker_code) DO NOTHING
            """, (current_user_id, ticker))
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=400, detail="Could not add to favorites")

    await increment_stat(ticker, "favorite_count")
    await track_event("favorite_toggle", user_id=current_user_id, ticker=ticker, details={"action": "add"})
    return {"message": f"Added favorite {ticker} or already been added"}


@router.delete("/favorites/{ticker}")
async def remove_favorite(ticker: str, current_user_id: int = Depends(get_current_user)):
    await validate_ticker(ticker)

    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
            DELETE FROM favorites
            WHERE user_id = %s AND ticker_code = %s
            """, (current_user_id, ticker))
            await db.commit()
        except Exception as e:
            await db.rollback()

    return {"message": f"Removed {ticker} from favorites"}


@router.get("/favorites")
async def get_favorites(current_user_id: int = Depends(get_current_user)):
    async with db.cursor(row_factory=None) as cur:
        try:
            await cur.execute("""
                SELECT ticker_code FROM favorites WHERE user_id = %s
            """, (current_user_id,))
            rows = await cur.fetchall()
            favorites_list = [row[0] for row in rows]
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return {"favorites": favorites_list}
