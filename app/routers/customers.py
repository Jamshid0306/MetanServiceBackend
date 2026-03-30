import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import TELEGRAM_LOGIN_BOT_TOKEN, TELEGRAM_LOGIN_BOT_USERNAME
from ..customers_database import (
    authenticate_customer,
    get_customer_record_by_phone,
    normalize_telegram_username,
    save_or_update_customer_from_telegram,
    save_customer_account,
    update_customer_password_by_phone,
)

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


def validate_login_identifier(identifier: str) -> tuple[str, str]:
    raw_identifier = str(identifier or "").strip()

    if not raw_identifier:
        raise HTTPException(
            status_code=400,
            detail="Phone number or username is required",
        )

    normalized_phone = normalize_phone_number(raw_identifier)
    if len(normalized_phone) == 12 and normalized_phone.startswith("998"):
        return ("phone", normalized_phone)

    normalized_username = normalize_telegram_username(raw_identifier)
    if len(normalized_username) >= 3:
        return ("username", normalized_username)

    raise HTTPException(
        status_code=400,
        detail="Enter a valid username or Uzbekistan phone number",
    )


def validate_password(password: str) -> str:
    normalized_password = str(password or "").strip()

    if len(normalized_password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters long",
        )

    return normalized_password


class CustomerLoginPayload(BaseModel):
    identifier: str
    password: str


class CustomerRegisterPayload(BaseModel):
    name: str
    phone: str
    password: str


class CustomerResetPasswordPayload(BaseModel):
    phone: str
    password: str
    reset_token: str | None = None


class CustomerTelegramLoginPayload(BaseModel):
    id: int | str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int | str
    hash: str


def build_telegram_data_check_string(payload: CustomerTelegramLoginPayload) -> str:
    data = {
        "auth_date": str(payload.auth_date),
        "first_name": str(payload.first_name or "").strip(),
        "id": str(payload.id),
        "last_name": str(payload.last_name or "").strip(),
        "photo_url": str(payload.photo_url or "").strip(),
        "username": str(payload.username or "").strip(),
    }
    return "\n".join(
        f"{key}={value}"
        for key, value in sorted(data.items())
        if value
    )


def validate_telegram_login(payload: CustomerTelegramLoginPayload) -> None:
    if not TELEGRAM_LOGIN_BOT_USERNAME or not TELEGRAM_LOGIN_BOT_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Telegram login is not configured",
        )

    try:
        auth_date = int(str(payload.auth_date).strip())
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="Telegram auth date is invalid",
        ) from error

    if auth_date < int(time.time()) - 86400:
        raise HTTPException(
            status_code=401,
            detail="Telegram login data is expired",
        )

    secret_key = hashlib.sha256(TELEGRAM_LOGIN_BOT_TOKEN.encode("utf-8")).digest()
    data_check_string = build_telegram_data_check_string(payload)
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, str(payload.hash or "").strip()):
        raise HTTPException(
            status_code=401,
            detail="Telegram login verification failed",
        )


@router.get("/telegram-login/config")
def get_telegram_login_config():
    return {
        "enabled": bool(TELEGRAM_LOGIN_BOT_USERNAME and TELEGRAM_LOGIN_BOT_TOKEN),
        "bot_username": TELEGRAM_LOGIN_BOT_USERNAME or None,
    }


@router.post("/login")
def login_customer(payload: CustomerLoginPayload):
    _, identifier = validate_login_identifier(payload.identifier)
    password = validate_password(payload.password)
    customer = authenticate_customer(identifier, password)

    if not customer:
        raise HTTPException(
            status_code=401,
            detail="Phone number, username or password is incorrect",
        )

    return {
        "success": True,
        "customer": customer,
    }


@router.post("/telegram-login")
def login_customer_with_telegram(payload: CustomerTelegramLoginPayload):
    validate_telegram_login(payload)

    try:
        customer = save_or_update_customer_from_telegram(
            telegram_id=str(payload.id),
            first_name=str(payload.first_name or "").strip(),
            last_name=str(payload.last_name or "").strip(),
            telegram_username=payload.username,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail="Telegram account could not be linked",
        ) from error

    return {
        "success": True,
        "customer": customer,
    }


@router.post("/register")
def register_customer(payload: CustomerRegisterPayload):
    name = payload.name.strip()
    normalized_phone = normalize_phone_number(payload.phone)
    password = validate_password(payload.password)

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name is required",
        )

    if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid Uzbekistan phone number",
        )

    existing_customer = get_customer_record_by_phone(normalized_phone)
    if existing_customer and str(existing_customer["password_hash"] or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This phone number is already registered",
        )

    try:
        customer = save_customer_account(
            name,
            normalized_phone,
            password,
            telegram_username=None,
        )
    except ValueError as error:
        if str(error) == "telegram_username_taken":
            raise HTTPException(
                status_code=409,
                detail="This profile could not be created",
            ) from error
        raise

    return {
        "success": True,
        "customer": customer,
    }


@router.post("/reset-password")
def reset_customer_password(payload: CustomerResetPasswordPayload):
    normalized_phone = normalize_phone_number(payload.phone)
    password = validate_password(payload.password)

    if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid Uzbekistan phone number",
        )

    customer = update_customer_password_by_phone(normalized_phone, password)
    if not customer:
        raise HTTPException(
            status_code=404,
            detail="Customer profile was not found for this phone number",
        )

    return {
        "success": True,
        "customer": customer,
    }
