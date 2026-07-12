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
from src.services.company import get_company_info
from src.services.news import get_latest_news
from src.services.report import generate_quick_report, generate_deep_report
from src.services.economy import (
    get_gold_prices,
    get_silver_prices,
    get_currency_symbols,
    get_currency,
)
from src.services.ipo import get_upcoming_ipos
from src.services.scout import scout_best_tickers
from src.services.price import get_price_history
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
def bist_companies():
    return get_bist_companies_as_dict_from_redis()

@router.get("/bist/tickers")
def bist_tickers():
    return get_bist_tickers_as_dict_from_redis()

@router.get("/companies/search/{ticker}")
def search_companies(ticker: str):
    _validate_ticker(ticker)
    return search_companies_by_text(ticker)

@router.get("/companies/info/{ticker}")
def company_info(ticker: str):
    _validate_ticker(ticker)
    return get_company_info(ticker)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@router.get("/generate/report/quick/{ticker}")
def generate_quick_report_mock(ticker: str):
    _validate_ticker(ticker)
    return "Hizli Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."

@router.get("/generate/report/deep/{ticker}")
def generate_deep_report_mock(ticker: str):
    _validate_ticker(ticker)
    return "Derin Rapor: Lorem ipsum dolor sit amet, consectetur adipiscing elit."


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

@router.get("/news/{ticker}")
def news(ticker: str, amount: int = Query(default=10, description="Number of news items")):
    _validate_ticker(ticker)
    return get_latest_news(ticker, amount)


# ---------------------------------------------------------------------------
# Simulations
# ---------------------------------------------------------------------------

@router.get("/simulations/probability/{ticker}")
def probability(ticker: str, days: int = Query(...), target: str = Query(...)):
    _validate_ticker(ticker)
    percent = montecarlo.probability(ticker, days, target)
    return {"percent": percent, "ticker": ticker, "days": days, "target": target}

@router.get("/simulations/confidence-interval/{ticker}")
def confidence_interval(ticker: str, days: int = Query(...), bounds: str = Query(...)):
    _validate_ticker(ticker)
    return montecarlo.confidence_interval(ticker, days, bounds)


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
def ipos_upcoming():
    return json.loads(get_upcoming_ipos())


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
    return get_price_history(ticker, period, interval)


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
def add_favorite(ticker: str):
    _validate_ticker(ticker)
    return {"message": f"{ticker} favorilere eklendi!"}

@router.delete("/favorites/{ticker}")
def remove_favorite(ticker: str):
    _validate_ticker(ticker)
    return {"message": f"{ticker} favorilerden cikarildi!"}

@router.get("/favorites")
def get_favorites():
    return {"favorites": ["ASELS", "THYAO", "GARAN", "KCHOL", "SISE"]}