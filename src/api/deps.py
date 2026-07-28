import os
from datetime import datetime, timezone
from fastapi import Depends, HTTPException, status, Header
from fastapi.security import OAuth2PasswordBearer
import jwt

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = "HS256"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


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

        token_iat = payload.get("iat")
        if token_iat is not None:
            from src.core.database import db
            with db.cursor() as cur:
                cur.execute("SELECT password_changed_at FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if row is None:
                    raise credentials_exception
                changed_at = row[0]
                if changed_at is not None:
                    if isinstance(changed_at, datetime):
                        changed_ts = changed_at.replace(tzinfo=timezone.utc).timestamp()
                    else:
                        changed_ts = changed_at.timestamp()
                    if token_iat < changed_ts:
                        raise credentials_exception

        return user_id
    except jwt.PyJWTError:
        raise credentials_exception


def verify_admin_token(x_admin_token: str = Header(...)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="ADMIN_TOKEN not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    return True


def validate_ticker(ticker: str):
    from src.services.bist import is_valid_bist_ticker
    if not is_valid_bist_ticker(ticker):
        raise HTTPException(status_code=404, detail=f"Invalid BIST ticker: {ticker}")
