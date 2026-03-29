import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..customers_database import (
    authenticate_customer,
    build_telegram_placeholder_phone,
    get_customer_record_by_telegram_username,
    normalize_telegram_username,
    save_customer_account,
    update_customer_password_by_phone,
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


def validate_login_identifier(identifier: str) -> tuple[str, str]:
    raw_identifier = str(identifier or "").strip()

    if not raw_identifier:
        raise HTTPException(
            status_code=400,
            detail="Telegram username or phone number is required",
        )

    normalized_phone = normalize_phone_number(raw_identifier)
    if len(normalized_phone) == 12 and normalized_phone.startswith("998"):
        return ("phone", normalized_phone)

    normalized_username = normalize_telegram_username(raw_identifier)
    if len(normalized_username) >= 3:
        return ("telegram", normalized_username)

    raise HTTPException(
        status_code=400,
        detail="Enter a valid Telegram username or Uzbekistan phone number",
    )


def validate_telegram_username(username: str) -> str:
    normalized_username = normalize_telegram_username(username)

    if len(normalized_username) < 3:
        raise HTTPException(
            status_code=400,
            detail="Telegram username must be at least 3 characters long",
        )

    allowed_characters = set("abcdefghijklmnopqrstuvwxyz0123456789_")
    if any(character not in allowed_characters for character in normalized_username):
        raise HTTPException(
            status_code=400,
            detail="Telegram username can contain only letters, numbers and underscores",
        )

    return normalized_username


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
    telegram_username: str
    password: str


class TelegramLoginPayload(BaseModel):
    id: int
    first_name: str
    auth_date: int
    hash: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


class CustomerResetPasswordPayload(BaseModel):
    phone: str
    password: str
    reset_token: str | None = None


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
    _, identifier = validate_login_identifier(payload.identifier)
    password = validate_password(payload.password)
    customer = authenticate_customer(identifier, password)

    if not customer:
        raise HTTPException(
            status_code=401,
            detail="Telegram username, phone number or password is incorrect",
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
    telegram_username = normalize_telegram_username(verified_payload.get("username") or "")
    customer_record = (
        get_customer_record_by_telegram_username(telegram_username)
        if telegram_username
        else None
    )

    if customer_record:
        customer = {
            "id": customer_record["id"],
            "name": str(customer_record["name"] or full_name or "Telegram User"),
            "phone": str(customer_record["phone"] or "") if str(customer_record["phone"] or "").isdigit() else "",
            "telegram_id": verified_payload["id"],
            "telegram_username": customer_record["telegram_username"],
            "username": verified_payload.get("username"),
            "photo_url": verified_payload.get("photo_url"),
            "has_password": bool(str(customer_record["password_hash"] or "").strip()),
            "created_at": customer_record["created_at"],
            "updated_at": customer_record["updated_at"],
        }
    else:
        customer = {
            "id": f"telegram:{verified_payload['id']}",
            "name": full_name or str(verified_payload.get("username") or "Telegram User"),
            "phone": "",
            "telegram_id": verified_payload["id"],
            "telegram_username": telegram_username or None,
            "username": verified_payload.get("username"),
            "photo_url": verified_payload.get("photo_url"),
            "has_password": False,
        }

    return {
        "success": True,
        "customer": customer,
    }


@router.post("/register")
def register_customer(payload: CustomerRegisterPayload):
    name = payload.name.strip()
    telegram_username = validate_telegram_username(payload.telegram_username)
    password = validate_password(payload.password)
    existing_customer = get_customer_record_by_telegram_username(telegram_username)

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name, Telegram username and password are required",
        )

    if existing_customer and str(existing_customer["password_hash"] or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This Telegram username is already registered",
        )

    try:
        customer = save_customer_account(
            name,
            build_telegram_placeholder_phone(telegram_username),
            password,
            telegram_username=telegram_username,
        )
    except ValueError as error:
        if str(error) == "telegram_username_taken":
            raise HTTPException(
                status_code=409,
                detail="This Telegram username is already registered",
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
