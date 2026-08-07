import asyncio
from enum import Enum

import yfinance as yf
from fastapi import FastAPI, Query, HTTPException, Body, Depends

from src.api.deps import verify_admin_token
from src.clients.llm import health_check
from src.clients.search import news_search
from src.core.config import reload_config, is_production
from src.core.database import db
from src.core.redis import r
from src.services.credits import add_free_credits, add_gift_credits, get_total as get_credits
from src.services.token import get_token_summary

docs_enabled = not is_production()
admin_app = FastAPI(docs_url="/docs" if docs_enabled else None,
                    redoc_url="/redoc" if docs_enabled else None,
                    openapi_url="/openapi.json" if docs_enabled else None)


@admin_app.middleware("http")
async def admin_release_middleware(request, call_next):
    """Her admin isteginden sonra task baglantisini havuza iade et (sizinti onleme)."""
    try:
        return await call_next(request)
    finally:
        await db.release_current()


class GiftTarget(str, Enum):
    EVERYONE = "everyone"
    USER = "user"


@admin_app.post("/gift-credits")
async def gift_credits(
    _: bool = Depends(verify_admin_token),
    user_type: str = Query(...),
    amount: int = Query(..., gt=1),
    username: str | None = Query(default=None),
    credit_type: str = Query(default="free_credits", description="free_credits or gift_credits"),
    filters: dict = Body({})
):
    try:
        if user_type == GiftTarget.EVERYONE:
            async with db.cursor(row_factory=None) as cur:
                await cur.execute("SELECT id, username FROM users")
                rows = await cur.fetchall()
            for row in rows:
                if credit_type == "gift_credits":
                    await add_gift_credits(row[0], amount)
                else:
                    await add_free_credits(row[0], amount)
            return {"success": True}
        elif user_type == GiftTarget.USER:
            if not username:
                raise HTTPException(status_code=400, detail="username is required for user type")
            async with db.cursor(row_factory=None) as cur:
                await cur.execute("SELECT id FROM users WHERE username = %s", (username,))
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="User not found")

            if credit_type == "gift_credits":
                await add_gift_credits(row[0], amount)
            else:
                await add_free_credits(row[0], amount)

            return {"success": True, "user": {"username": username, "credits": await get_credits(row[0])}}
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
async def config_reload(_: bool = Depends(verify_admin_token)):
    try:
        reload_config()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")


@admin_app.post("/healthcheck")
async def healthcheck(_: bool = Depends(verify_admin_token)):

    db_health: bool = True
    redis_health: bool = True
    llm_health: bool = True
    news_health: bool = False
    yfinance_health: bool = False

    # db check
    async with db.cursor(row_factory=None) as cur:
        await cur.execute("SELECT 1;")
        output = await cur.fetchone()
        if output[0] != 1:
            db_health = False

    # redis check
    try:
        conn = await r._get_conn()
        redis_health = await conn.ping() if conn is not None else False
    except Exception as e:
        redis_health = False

    # llm check
    llm_health = await health_check()

    # news health
    news = await news_search("news", 1)
    if len(news) > 0:
        news_health = True

    # yfinance health
    info = await asyncio.to_thread(lambda: yf.Ticker("ASELS.IS").info)
    if info is not None:
        yfinance_health = True

    return {
        "db_health": db_health,
        "redis_health": redis_health,
        "llm_health": llm_health,
        "news_health": news_health,
        "yfinance_health": yfinance_health,
        "status": "OK" if (db_health and redis_health and llm_health and news_health and yfinance_health) else "ERROR"
    }


@admin_app.post("/token-usage")
async def token_usage(
    _: bool = Depends(verify_admin_token),
    since: str | None = Query(None, description="ISO format datetime, e.g. 2024-01-01T00:00:00Z"),
    endpoint: str | None = Query(None),
):
    try:
        from datetime import datetime
        since_dt = datetime.fromisoformat(since) if since else None
        summary = await get_token_summary(since=since_dt, endpoint=endpoint)
        return summary
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format. Use ISO format.")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")


@admin_app.post("/maintenance/toggle")
async def maintenance_toggle(
    _: bool = Depends(verify_admin_token),
    feature: str = Query(...),
    action: str = Query(...),
):
    from src.services.maintenance import toggle as toggle_maintenance
    return await toggle_maintenance(feature, action)
