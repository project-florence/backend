from enum import Enum

from fastapi import FastAPI, Query, HTTPException, Body
from src.core.database import db
from src.core.config import reload_config
from src.core.redis import r
from src.clients.llm import health_check
from src.clients.search import news_search
import yfinance as yf

admin_app = FastAPI()

class GiftTarget(str, Enum):
    EVERYONE = "everyone"

@admin_app.post("/gift-credits")
def gift_credits(
    user_type:  = Query(...),
    amount: int = Query(..., gt=1),
    filters: dict = Body({})
):
    try:
        if user_type == GiftTarget.EVERYONE:
            with db.cursor() as cur:
                cur.execute("""
                    UPDATE users SET credits = credits + %s
                """, (amount,))
                db.commit()
                return {
                    "success": True,
                }
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid type. Allowed values: 'everyone'"
            )
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error")

@admin_app.post("/config-reload")
def config_reload():
    try:
        reload_config()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@admin_app.post("/healthcheck")
def healthcheck():

    db_health : bool = True
    redis_health : bool = True
    llm_health : bool = True
    news_health : bool = False
    yfinance_health : bool = False

    # db check
    with db.cursor() as cur:
        cur.execute("""
            SELECT 1;
        """)
        output = cur.fetchone()
        if output[0] != 1:
            db_health = False

    # redis check
    try:
        redis_health = r.ping()
    except Exception as e:
        redis_health = False

    # llm check
    llm_health = health_check()

    # news health
    news = news_search("news", 1)
    if len(news) > 0:
        news_health = True

    # yfinance health
    info = yf.Ticker("ASELS.IS").info
    if info is not None:
        yfinance_health = True

    return {
        "db_health": db_health,
        "redis_health": redis_health,
        "llm_health": llm_health,
        "news_health": news_health,
        "status": "OK" if (db_health and redis_health and llm_health and news_health and yfinance_health) else "ERROR"
    }