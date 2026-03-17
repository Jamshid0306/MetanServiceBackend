import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..customers_database import (
    authenticate_customer,
    get_customer_record_by_phone,
    save_customer_account,
)
from ..config import TELEGRAM_LOGIN_BOT_TOKEN, TELEGRAM_LOGIN_MAX_AGE_SECONDS

router = APIRouter()


def normalize_phone_number(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""

    if digits.startswith("998") and len(digits) == 12:
        return digits

    if len(digits) == 9:
        return f"998{digits}"

    return digits


def validate_customer_phone(phone: str) -> str:
    normalized_phone = normalize_phone_number(phone)

    if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
        raise HTTPException(
            status_code=400,
            detail="A valid Uzbekistan phone number is required",
        )

    return normalized_phone


def validate_password(password: str) -> str:
    normalized_password = str(password or "").strip()

    if len(normalized_password) < 4:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 4 characters long",
        )

    return normalized_password


class CustomerLoginPayload(BaseModel):
    phone: str
    password: str


class CustomerRegisterPayload(BaseModel):
    name: str
    phone: str
    password: str


class TelegramLoginPayload(BaseModel):
    id: int
    first_name: str
    auth_date: int
    hash: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


def verify_telegram_login(payload: TelegramLoginPayload) -> dict[str, Any]:
    if not TELEGRAM_LOGIN_BOT_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Telegram login is not configured on the server",
        )

    payload_data = payload.model_dump(exclude_none=True)
    received_hash = str(payload_data.pop("hash", "")).strip()

    if not received_hash:
        raise HTTPException(status_code=400, detail="Telegram hash is required")

    data_check_string = "\n".join(
        f"{key}={payload_data[key]}"
        for key in sorted(payload_data.keys())
    )
    secret_key = hashlib.sha256(TELEGRAM_LOGIN_BOT_TOKEN.encode("utf-8")).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram login verification failed")

    auth_age = int(time.time()) - int(payload.auth_date)
    if auth_age > TELEGRAM_LOGIN_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram login request expired")

    return payload_data


@router.post("/login")
def login_customer(payload: CustomerLoginPayload):
    normalized_phone = validate_customer_phone(payload.phone)
    password = validate_password(payload.password)
    customer = authenticate_customer(normalized_phone, password)

    if not customer:
        raise HTTPException(
            status_code=401,
            detail="Phone number or password is incorrect",
        )

    return {
        "success": True,
        "customer": customer,
    }


@router.post("/telegram")
def telegram_customer_login(payload: TelegramLoginPayload):
    verified_payload = verify_telegram_login(payload)
    full_name = " ".join(
        part.strip()
        for part in [
            str(verified_payload.get("first_name") or ""),
            str(verified_payload.get("last_name") or ""),
        ]
        if part.strip()
    ).strip()

    return {
        "success": True,
        "customer": {
            "id": f"telegram:{verified_payload['id']}",
            "name": full_name or str(verified_payload.get("username") or "Telegram User"),
            "phone": "",
            "telegram_id": verified_payload["id"],
            "username": verified_payload.get("username"),
            "photo_url": verified_payload.get("photo_url"),
        },
    }


@router.post("/register")
def register_customer(payload: CustomerRegisterPayload):
    name = payload.name.strip()
    normalized_phone = validate_customer_phone(payload.phone)
    password = validate_password(payload.password)
    existing_customer = get_customer_record_by_phone(normalized_phone)

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name, phone and password are required",
        )

    if existing_customer and str(existing_customer["password_hash"] or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This phone number is already registered",
        )

    customer = save_customer_account(name, normalized_phone, password)
    return {
        "success": True,
        "customer": customer,
    }
