from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta
import jwt # type: ignore
from pydantic import BaseModel

from ..config import (
    ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES,
    ADMIN_PASSWORD,
    ADMIN_REFRESH_TOKEN_EXPIRE_DAYS,
    ADMIN_USERNAME,
    SECRET_KEY,
)

router = APIRouter()

ALGORITHM = "HS256"

FAKE_ADMIN = {
    "username": ADMIN_USERNAME,
    "password": ADMIN_PASSWORD
}

class LoginData(BaseModel):
    username: str
    password: str

class RefreshTokenData(BaseModel):
    refresh_token: str

def create_token(data: dict, expires_delta: timedelta, token_type: str):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire, "type": token_type})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def build_auth_response(username: str) -> dict:
    access_token = create_token(
        data={"sub": username},
        expires_delta=timedelta(minutes=ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES),
        token_type="access",
    )
    refresh_token = create_token(
        data={"sub": username},
        expires_delta=timedelta(days=ADMIN_REFRESH_TOKEN_EXPIRE_DAYS),
        token_type="refresh",
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }

def decode_refresh_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Refresh token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid token type")

    if payload.get("sub") != FAKE_ADMIN["username"]:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    return payload

@router.post("/login")
def login(data: LoginData):
    if data.username == FAKE_ADMIN["username"] and data.password == FAKE_ADMIN["password"]:
        return build_auth_response(data.username)
    raise HTTPException(status_code=401, detail="Invalid credentials")

@router.post("/refresh")
def refresh_admin_token(data: RefreshTokenData):
    payload = decode_refresh_token(data.refresh_token)
    return build_auth_response(payload["sub"])
