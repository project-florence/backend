import os

from fastapi import APIRouter, Depends, Query, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
import psycopg2
from psycopg2 import errors
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
import jwt
import datetime
import json
import os
from src.core.database import db

from src.services.bist import (
    get_bist_companies_as_dict_from_redis,
    get_bist_tickers_as_dict_from_redis,
    search_companies_by_text,
    is_valid_bist_ticker,
)
from src.services.company import get_company_info, get_companies_summary
from src.services.news import get_latest_news
from src.services.report import generate_quick_report, generate_deep_report
from src.services.economy import (
    get_gold_prices,
    get_silver_prices,
    get_currency_symbols,
    get_currency,
)
from src.services.ipo import get_upcoming_ipos, get_draft_ipos, get_ipo_detail_by_slug
from src.services.scout import scout_best_tickers
from src.services.price import get_price_history
from src.services.stats import increment_stat, get_top_tickers, get_ticker_stats, get_all_stats
import src.simulation.montecarlo as montecarlo

router = APIRouter(prefix="/api/v1")

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"

ph = PasswordHasher()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

class UserRegister(BaseModel):
    username: str
    email: str
    password: str

def create_jwt_token(user_id: int):
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if user_id is None:
            raise credentials_exception
        return user_id
    except jwt.PyJWTError:
        raise credentials_exception

def _validate_ticker(ticker: str):
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")


# ---------------------------------------------------------------------------
# BIST / Companies
# ---------------------------------------------------------------------------

@router.get("/bist/companies")
def bist_companies(sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular")):
    companies = get_bist_companies_as_dict_from_redis()
    if sort == "popular":
        stats = get_all_stats()
        ticker_order = {s["ticker"]: i for i, s in enumerate(stats)}
        companies.sort(key=lambda c: (ticker_order.get(c["ticker"], 999), c["ticker"]))
    return companies

@router.get("/bist/tickers")
def bist_tickers(sort: str = Query(default="alphabetical", description="Sort order: alphabetical or popular")):
    tickers = get_bist_tickers_as_dict_from_redis()
    if sort == "popular":
        stats = get_all_stats()
        ticker_order = {s["ticker"]: i for i, s in enumerate(stats)}
        tickers.sort(key=lambda t: (ticker_order.get(t, 999), t))
    return tickers

@router.get("/companies/search")
def search_companies(query: str = Query(...)):
    return search_companies_by_text(query)

@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    _validate_ticker(ticker)
    result = get_company_info(ticker)
    increment_stat(ticker, "info_count")
    return result

@router.get("/companies/summary")
def companies_summary(
    limit: int = Query(default=50, description="Number of companies"),
    sort: str = Query(default="popular", description="Sort order: popular or alphabetical"),
    tickers: str | None = Query(default=None, description="Comma-separated ticker filter"),
):
    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    return get_companies_summary(limit=limit, sort=sort, tickers_filter=ticker_list)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.get("/generate/report/quick/{ticker}")
def generate_quick_report_mock(ticker: str):
    _validate_ticker(ticker)
    increment_stat(ticker, "report_count")
    return "Hizli Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."

@router.get("/generate/report/deep/{ticker}")
def generate_deep_report_mock(ticker: str):
    _validate_ticker(ticker)
    increment_stat(ticker, "report_count")
    return "Derin Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    _validate_ticker(ticker)
    result = get_latest_news(ticker, amount)
    increment_stat(ticker, "news_count")
    return result


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------

@router.get("/simulations/probability/{ticker}")
def probability(ticker: str, days: int = Query(...), target: str = Query(...)):
    _validate_ticker(ticker)
    percent = montecarlo.probability(ticker, days, target)
    increment_stat(ticker, "simulation_count")
    return {"percent": percent, "ticker": ticker, "days": days, "target": target}

@router.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, days: int = Query(...), bounds: str = Query(...)):
    _validate_ticker(ticker)
    result = montecarlo.confidence_interval(ticker, days, bounds)
    increment_stat(ticker, "simulation_count")
    return result


# ---------------------------------------------------------------------------
# Economy
# ---------------------------------------------------------------------------

@router.get("/economy/gold-prices")
def gold_prices():
    return get_gold_prices()

@router.get("/economy/silver-prices")
def silver_prices():
    return get_silver_prices()

@router.get("/economy/symbols")
def symbols():
    return get_currency_symbols()

@router.get("/economy/currency")
def currency():
    return get_currency()


# ---------------------------------------------------------------------------
# Macro Economy
# ---------------------------------------------------------------------------

@router.get("/macroeconomy/all")
def macroeconomy_all():
    return {"henuz implement edilmedi. key value seklinde faiz, gdp, issizlik, cari acik, enflasyon vb. gibi verileri dondurur."}


# ---------------------------------------------------------------------------
# IPO
# ---------------------------------------------------------------------------

@router.get("/ipos/upcoming")
def ipos_upcoming(after: str | None = Query(default=None, description="ISO date filter (e.g. 2026-06-01T00:00:00). Defaults to last 30 days.")):
    return json.loads(get_upcoming_ipos(after=after))

@router.get("/ipos/draft")
def ipos_draft(after: str | None = Query(default=None, description="ISO date filter. Defaults to last 30 days.")):
    return json.loads(get_draft_ipos(after=after))

@router.get("/ipos/{slug}")
def ipos_detail(slug: str):
    data = json.loads(get_ipo_detail_by_slug(slug))
    if data is None:
        raise HTTPException(status_code=404, detail="IPO not found")
    return data


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/stats/top")
def stats_top(limit: int = Query(default=50, description="Number of top tickers")):
    return get_top_tickers(limit=limit)

@router.get("/stats/{ticker}")
def stats_ticker(ticker: str):
    _validate_ticker(ticker)
    stats = get_ticker_stats(ticker)
    stats["ticker"] = ticker.upper()
    return stats


# ---------------------------------------------------------------------------
# Scout / Stock Picker
# ---------------------------------------------------------------------------

@router.get("/scout/best-tickers")
def best_tickers(
    investment_budget: int = Query(...),
    investment_horizon: str = Query(...),
    risk_tolerance: str = Query(...),
):
    return json.loads(scout_best_tickers())


# ---------------------------------------------------------------------------
# Price History
# ---------------------------------------------------------------------------

@router.get("/price/history/{ticker}")
def price_history(ticker: str, period: str = Query("1mo"), interval: str = Query("1d")):
    _validate_ticker(ticker)
    result = get_price_history(ticker, period, interval)
    increment_stat(ticker, "history_count")
    return result


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/auth/register")
def auth_register(user: UserRegister):
    with db.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s OR email = %s", (user.username, user.email))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=400, detail="Email or username already in use")

        hashed_pw = ph.hash(user.password)

        try:
            cur.execute(
                "INSERT INTO users (username, email, hashed_pw) VALUES (%s, %s, %s) RETURNING id",
                (user.username, user.email, hashed_pw)
            )
            new_user_id = cur.fetchone()[0]
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Register successful", "user_id": new_user_id}
@router.post("/auth/login")
def auth_login(form_data: OAuth2PasswordRequestForm = Depends()):
    cur = db.cursor()

    cur.execute("SELECT id, hashed_pw FROM users WHERE username = %s", (form_data.username,))
    user_row = cur.fetchone()

    if not user_row:
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    user_id, db_password_hash = user_row
    try:
        ph.verify(db_password_hash, form_data.password)
    except VerificationError:
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_jwt_token(user_id)
    return {"access_token": access_token, "token_type": "bearer"}

@router.delete("/auth/delete")
def auth_delete(current_user_id: int = Depends(get_current_user)):
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM users WHERE id = %s", (current_user_id,))
        db.commit()
    except Exception as e:
        db.rollback()
        cur.close()
        raise HTTPException(status_code=400, detail="Database error")
    return {"message": f"Deleted user {current_user_id}"}


# ---------------------------------------------------------------------------
# Favorites (Mock)
# ---------------------------------------------------------------------------

@router.post("/favorites/{ticker}")
def add_favorite(ticker: str, current_user_id: int = Depends(get_current_user)):
    _validate_ticker(ticker)
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
    _validate_ticker(ticker)

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