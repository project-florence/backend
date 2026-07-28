from enum import Enum

from fastapi import FastAPI, Query, HTTPException, Body, Depends
from src.core.database import db
from src.core.config import reload_config
from src.core.redis import r
from src.clients.llm import health_check
from src.clients.search import news_search
from src.services.token import get_token_summary
from src.services.credits import add_free_credits, add_gift_credits, get_total as get_credits
from src.api.deps import verify_admin_token
from src.core.config import is_production
import yfinance as yf

docs_enabled = not is_production()
admin_app = FastAPI(docs_url="/docs" if docs_enabled else None,
                    redoc_url="/redoc" if docs_enabled else None,
                    openapi_url="/openapi.json" if docs_enabled else None)

class GiftTarget(str, Enum):
    EVERYONE = "everyone"
    USER = "user"

@admin_app.post("/gift-credits")
def gift_credits(
    _: bool = Depends(verify_admin_token),
    user_type: str = Query(...),
    amount: int = Query(..., gt=1),
    username: str | None = Query(default=None),
    credit_type: str = Query(default="free_credits", description="free_credits or gift_credits"),
    filters: dict = Body({})
):
    try:
        if user_type == GiftTarget.EVERYONE:
            with db.cursor() as cur:
                cur.execute("SELECT id, username FROM users")
                for row in cur.fetchall():
                    if credit_type == "gift_credits":
                        add_gift_credits(row[0], amount)
                    else:
                        add_free_credits(row[0], amount)
                return {"success": True}
        elif user_type == GiftTarget.USER:
            if not username:
                raise HTTPException(status_code=400, detail="username is required for user type")
            with db.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="User not found")

            if credit_type == "gift_credits":
                add_gift_credits(row[0], amount)
            else:
                add_free_credits(row[0], amount)

            return {"success": True, "user": {"username": username, "credits": get_credits(row[0])}}
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid type. Allowed values: 'everyone', 'user'"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")

@admin_app.post("/config-reload")
def config_reload(_: bool = Depends(verify_admin_token)):
    try:
        reload_config()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@admin_app.post("/healthcheck")
def healthcheck(_: bool = Depends(verify_admin_token)):

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

@admin_app.post("/token-usage")
def token_usage(
    _: bool = Depends(verify_admin_token),
    since: str | None = Query(None, description="ISO format datetime, e.g. 2024-01-01T00:00:00Z"),
    endpoint: str | None = Query(None),
):
    try:
        from datetime import datetime
        since_dt = datetime.fromisoformat(since) if since else None
        summary = get_token_summary(since=since_dt, endpoint=endpoint)
        return summary
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format. Use ISO format.")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")
