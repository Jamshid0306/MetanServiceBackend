from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta
import jwt # type: ignore
from pydantic import BaseModel

from ..config import ADMIN_PASSWORD, ADMIN_USERNAME, SECRET_KEY

router = APIRouter()

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 7

FAKE_ADMIN = {
    "username": ADMIN_USERNAME,
    "password": ADMIN_PASSWORD
}

class LoginData(BaseModel):
    username: str
    password: str

def create_access_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

@router.post("/login")
def login(data: LoginData):
    if data.username == FAKE_ADMIN["username"] and data.password == FAKE_ADMIN["password"]:
        access_token = create_access_token(
            data={"sub": data.username},
            expires_delta=timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
        )
        return {"access_token": access_token, "token_type": "bearer"}
    raise HTTPException(status_code=401, detail="Invalid credentials")
