import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
import jwt

from src.core.database import db
from src.api.deps import SECRET_KEY, ALGORITHM, get_current_user

ph = PasswordHasher()
router = APIRouter()


class UserRegister(BaseModel):
    username: str
    email: str
    password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class UpdateEmail(BaseModel):
    new_email: EmailStr
    current_password: str


class UpdateUsername(BaseModel):
    new_username: str
    current_password: str


def create_jwt_token(user_id: int):
    payload = {
        "user_id": user_id,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


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
    with db.cursor() as cur:
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
    with db.cursor() as cur:
        try:
            cur.execute("DELETE FROM users WHERE id = %s", (current_user_id,))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=400, detail="Database error")
    return {"message": f"Deleted user {current_user_id}"}


@router.put("/auth/change-password")
def change_password(payload: ChangePassword, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = cur.fetchone()

        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        db_password_hash = user_row[0]

        try:
            ph.verify(db_password_hash, payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        new_hashed_pw = ph.hash(payload.new_password)
        try:
            cur.execute("UPDATE users SET hashed_pw = %s WHERE id = %s", (new_hashed_pw, current_user_id))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Password changed successfully"}


@router.put("/auth/change-email")
def change_email(payload: UpdateEmail, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            ph.verify(user_row[0], payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        cur.execute("SELECT id FROM users WHERE email = %s AND id != %s", (payload.new_email, current_user_id))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Email already in use")

        try:
            cur.execute("UPDATE users SET email = %s WHERE id = %s", (payload.new_email, current_user_id))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Email changed successfully", "new_email": payload.new_email}


@router.put("/auth/change-username")
def change_username(payload: UpdateUsername, current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        cur.execute("SELECT hashed_pw FROM users WHERE id = %s", (current_user_id,))
        user_row = cur.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            ph.verify(user_row[0], payload.current_password)
        except VerificationError:
            raise HTTPException(status_code=400, detail="Current password is incorrect")

        cur.execute("SELECT id FROM users WHERE username = %s AND id != %s", (payload.new_username, current_user_id))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Username already in use")

        try:
            cur.execute("UPDATE users SET username = %s WHERE id = %s", (payload.new_username, current_user_id))
            db.commit()
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail="Database error")

    return {"message": "Username changed successfully", "new_username": payload.new_username}


@router.get("/profile")
def get_profile(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                SELECT username, email, credits FROM users WHERE id = %s
            """, (current_user_id,))
            rows = cur.fetchone()

        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return {
        "username": rows[0],
        "email": rows[1],
        "credits": rows[2]
    }


@router.get("/credits")
def get_credits(current_user_id: int = Depends(get_current_user)):
    with db.cursor() as cur:
        try:
            cur.execute("""
                SELECT credits FROM users WHERE id = %s
            """, (current_user_id,))
            rows = cur.fetchone()

        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error")

    return {
        "credits": rows[0]
    }
