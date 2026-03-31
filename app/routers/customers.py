import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

import requests  # type: ignore
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from ..auth import verify_token
from ..config import TELEGRAM_LOGIN_BOT_TOKEN, TELEGRAM_LOGIN_BOT_USERNAME
from ..customers_database import (
    authenticate_customer,
    complete_customer_registration_session,
    complete_customer_login_session,
    delete_customer_by_id,
    get_all_customers,
    get_customer_by_id,
    get_customer_by_phone,
    get_customer_login_session,
    get_customer_record_by_phone,
    get_latest_customer_login_session_by_telegram_id,
    get_customer_registration_session,
    get_latest_customer_registration_session_by_telegram_id,
    hash_password,
    mark_customer_login_session_awaiting_contact,
    mark_customer_login_session_failed,
    mark_customer_registration_session_awaiting_contact,
    mark_customer_registration_session_failed,
    normalize_telegram_username,
    save_customer_login_session,
    save_customer_registration_session,
    save_or_update_customer_from_telegram,
    save_customer_account,
    update_customer_telegram_by_phone,
    update_customer_password_by_phone,
)

router = APIRouter()
logger = logging.getLogger(__name__)
TELEGRAM_BOT_API_BASE_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_LOGIN_BOT_TOKEN}"
    if TELEGRAM_LOGIN_BOT_TOKEN
    else ""
)
TELEGRAM_REGISTRATION_DEEP_LINK_PREFIX = "register_"
TELEGRAM_LOGIN_DEEP_LINK_PREFIX = "login_"


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


def is_telegram_registration_configured() -> bool:
    return bool(TELEGRAM_LOGIN_BOT_USERNAME and TELEGRAM_LOGIN_BOT_TOKEN)


def build_telegram_registration_deep_link(state: str) -> str:
    return f"https://t.me/{TELEGRAM_LOGIN_BOT_USERNAME}?start={TELEGRAM_REGISTRATION_DEEP_LINK_PREFIX}{state}"


def build_telegram_login_deep_link(state: str) -> str:
    return f"https://t.me/{TELEGRAM_LOGIN_BOT_USERNAME}?start={TELEGRAM_LOGIN_DEEP_LINK_PREFIX}{state}"


def call_telegram_bot_api(
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not TELEGRAM_BOT_API_BASE_URL:
        logger.warning("Telegram bot API call skipped for %s because bot token is missing", method)
        return None

    try:
        response = requests.post(
            f"{TELEGRAM_BOT_API_BASE_URL}/{method}",
            json=payload or {},
            timeout=15,
        )
    except requests.RequestException as error:
        logger.exception("Telegram bot API request failed for %s: %s", method, error)
        return None

    try:
        response_payload = response.json()
    except ValueError:
        logger.error(
            "Telegram bot API returned non-JSON response for %s: status=%s body=%s",
            method,
            response.status_code,
            response.text[:500],
        )
        return None

    if response.status_code >= 400 or not response_payload.get("ok"):
        logger.error(
            "Telegram bot API call failed for %s: status=%s payload=%s response=%s",
            method,
            response.status_code,
            payload,
            response_payload,
        )

    return response_payload


def get_telegram_api_error_detail(
    response_payload: dict[str, Any] | None,
    fallback: str,
) -> str:
    if not response_payload:
        return fallback

    detail = str(response_payload.get("description") or "").strip()
    return detail or fallback


def send_telegram_text_message(
    chat_id: str,
    text: str,
    *,
    request_contact: bool = False,
    remove_keyboard: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }

    if request_contact:
        payload["reply_markup"] = {
            "keyboard": [
                [
                    {
                        "text": "Telefon raqamni yuborish",
                        "request_contact": True,
                    }
                ]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    elif remove_keyboard:
        payload["reply_markup"] = {
            "remove_keyboard": True,
        }

    call_telegram_bot_api("sendMessage", payload)


class CustomerLoginPayload(BaseModel):
    identifier: str
    password: str


class CustomerResponse(BaseModel):
    id: int | None = None
    name: str
    phone: str
    telegram_id: str | None = None
    telegram_username: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CustomerListResponse(BaseModel):
    success: bool
    customers: list[CustomerResponse]
    total: int


class CustomerDeleteResponse(BaseModel):
    success: bool
    customer: CustomerResponse
    detail: str


class CustomerRegisterPayload(BaseModel):
    name: str
    phone: str
    password: str


class CustomerTelegramRegistrationStartPayload(BaseModel):
    name: str
    password: str


class CustomerTelegramRegistrationStatusResponse(BaseModel):
    success: bool
    state: str
    status: str
    bot_url: str | None = None
    error: str | None = None
    customer: CustomerResponse | None = None


class CustomerTelegramLoginStatusResponse(BaseModel):
    success: bool
    state: str
    status: str
    bot_url: str | None = None
    error: str | None = None
    customer: CustomerResponse | None = None


class CustomerResetPasswordPayload(BaseModel):
    phone: str
    password: str
    reset_token: str | None = None


class TelegramBotWebhookInfoResponse(BaseModel):
    success: bool
    configured: bool
    bot_username: str | None = None
    webhook_url: str | None = None
    pending_update_count: int = 0
    last_error_date: int | None = None
    last_error_message: str | None = None


class TelegramBotWebhookSetPayload(BaseModel):
    url: str


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


@router.get("/telegram-login/config", include_in_schema=False)
def get_telegram_login_config():
    return {
        "enabled": bool(TELEGRAM_LOGIN_BOT_USERNAME and TELEGRAM_LOGIN_BOT_TOKEN),
        "bot_username": TELEGRAM_LOGIN_BOT_USERNAME or None,
    }


@router.get("/register/telegram/config")
def get_telegram_registration_config():
    return {
        "enabled": is_telegram_registration_configured(),
        "bot_username": TELEGRAM_LOGIN_BOT_USERNAME or None,
    }


@router.get(
    "/telegram-bot/webhook-info",
    response_model=TelegramBotWebhookInfoResponse,
    summary="Get Telegram bot webhook info",
    description="Returns the webhook status reported by Telegram Bot API. Requires an admin bearer token.",
)
def get_telegram_bot_webhook_info(token: dict = Depends(verify_token)):
    if not is_telegram_registration_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram registration is not configured",
        )

    response_payload = call_telegram_bot_api("getWebhookInfo")
    if not response_payload or not response_payload.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=get_telegram_api_error_detail(
                response_payload,
                "Telegram webhook info could not be loaded",
            ),
        )

    result = response_payload.get("result") or {}
    webhook_url = str(result.get("url") or "").strip() or None
    return {
        "success": True,
        "configured": bool(webhook_url),
        "bot_username": TELEGRAM_LOGIN_BOT_USERNAME or None,
        "webhook_url": webhook_url,
        "pending_update_count": int(result.get("pending_update_count") or 0),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": str(result.get("last_error_message") or "").strip() or None,
    }


@router.post(
    "/telegram-bot/webhook/setup",
    response_model=TelegramBotWebhookInfoResponse,
    summary="Set Telegram bot webhook",
    description="Registers a webhook URL in Telegram Bot API for customer registration updates. Requires an admin bearer token.",
)
def set_telegram_bot_webhook(
    payload: TelegramBotWebhookSetPayload,
    token: dict = Depends(verify_token),
):
    if not is_telegram_registration_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram registration is not configured",
        )

    webhook_url = str(payload.url or "").strip()
    if not webhook_url.startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="Webhook URL must start with https://",
        )

    response_payload = call_telegram_bot_api(
        "setWebhook",
        {"url": webhook_url},
    )
    if not response_payload or not response_payload.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=get_telegram_api_error_detail(
                response_payload,
                "Telegram webhook could not be configured",
            ),
        )

    return get_telegram_bot_webhook_info(token)


@router.get(
    "/all",
    response_model=CustomerListResponse,
    summary="Get all customers",
    description="Returns the full customer list. Requires an admin bearer token.",
)
def list_all_customers(token: dict = Depends(verify_token)):
    customers = get_all_customers()
    return {
        "success": True,
        "customers": customers,
        "total": len(customers),
    }


@router.delete(
    "/{customer_id}",
    response_model=CustomerDeleteResponse,
    summary="Delete customer by ID",
    description="Deletes a customer by ID. Requires an admin bearer token.",
)
def delete_customer(customer_id: int, token: dict = Depends(verify_token)):
    customer = delete_customer_by_id(customer_id)
    if not customer:
        raise HTTPException(
            status_code=404,
            detail="Customer was not found",
        )

    return {
        "success": True,
        "customer": customer,
        "detail": "Customer deleted successfully",
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
    raise HTTPException(
        status_code=410,
        detail="Telegram widget login is disabled. Use Telegram bot phone verification instead.",
    )


@router.post(
    "/login/telegram/start",
    summary="Start Telegram bot login",
    description="Creates a pending login session and returns a Telegram deep link for phone verification via bot contact sharing.",
)
def start_customer_login_with_telegram():
    if not is_telegram_registration_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram login is not configured",
        )

    state = secrets.token_urlsafe(32)
    save_customer_login_session(state=state)

    return {
        "success": True,
        "state": state,
        "bot_url": build_telegram_login_deep_link(state),
    }


@router.get(
    "/login/telegram/status/{state}",
    response_model=CustomerTelegramLoginStatusResponse,
    summary="Check Telegram bot login status",
    description="Returns the current state of a pending Telegram bot login.",
)
def get_customer_login_status(state: str):
    login_session = get_customer_login_session(state)
    if not login_session:
        raise HTTPException(
            status_code=410,
            detail="Telegram login session expired",
        )

    customer = get_customer_by_id(login_session["customer_id"])
    return {
        "success": True,
        "state": str(login_session["state"] or "").strip(),
        "status": str(login_session["status"] or "pending").strip() or "pending",
        "bot_url": build_telegram_login_deep_link(state),
        "error": str(login_session["last_error"] or "").strip() or None,
        "customer": customer,
    }


@router.post(
    "/register/telegram/start",
    summary="Start Telegram registration",
    description="Creates a pending registration and returns a Telegram deep link for phone verification via bot contact sharing.",
)
def start_customer_registration_with_telegram(
    payload: CustomerTelegramRegistrationStartPayload,
):
    if not is_telegram_registration_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram registration is not configured",
        )

    name = str(payload.name or "").strip()
    password = validate_password(payload.password)

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name is required",
        )

    state = secrets.token_urlsafe(32)
    save_customer_registration_session(
        state=state,
        name=name,
        password_hash=hash_password(password),
    )

    return {
        "success": True,
        "state": state,
        "bot_url": build_telegram_registration_deep_link(state),
    }


@router.get(
    "/register/telegram/status/{state}",
    response_model=CustomerTelegramRegistrationStatusResponse,
    summary="Check Telegram registration status",
    description="Returns the current state of a pending Telegram-based registration.",
)
def get_customer_registration_status(state: str):
    registration_session = get_customer_registration_session(state)
    if not registration_session:
        raise HTTPException(
            status_code=410,
            detail="Telegram registration session expired",
        )

    customer = get_customer_by_id(registration_session["customer_id"])
    return {
        "success": True,
        "state": str(registration_session["state"] or "").strip(),
        "status": str(registration_session["status"] or "pending").strip() or "pending",
        "bot_url": build_telegram_registration_deep_link(state),
        "error": str(registration_session["last_error"] or "").strip() or None,
        "customer": customer,
    }


@router.post("/telegram-bot/webhook", include_in_schema=False)
def handle_telegram_bot_webhook(update: dict[str, Any] = Body(default={})):
    if not is_telegram_registration_configured():
        logger.warning("Ignored Telegram update because registration is not configured")
        return {"ok": True}

    message = update.get("message") or {}
    if not isinstance(message, dict):
        return {"ok": True}

    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text") or "").strip()
    contact = message.get("contact") or {}
    telegram_id = str(sender.get("id") or "").strip()
    chat_id = str(chat.get("id") or "").strip()
    telegram_username = str(sender.get("username") or "").strip() or None

    if text.startswith("/start"):
        start_payload = text.split(maxsplit=1)[1].strip() if " " in text else ""
        logger.info(
            "Received Telegram /start for chat_id=%s payload=%s",
            chat_id or None,
            start_payload or None,
        )
        if start_payload.startswith(TELEGRAM_LOGIN_DEEP_LINK_PREFIX):
            state = start_payload.removeprefix(TELEGRAM_LOGIN_DEEP_LINK_PREFIX).strip()
            login_session = get_customer_login_session(state)
            if not login_session:
                send_telegram_text_message(
                    chat_id,
                    "Sessiya tugagan yoki topilmadi. Saytdan kirishni qaytadan boshlang.",
                )
                return {"ok": True}

            mark_customer_login_session_awaiting_contact(
                state=state,
                telegram_id=telegram_id,
                telegram_username=telegram_username,
                telegram_chat_id=chat_id,
            )
            send_telegram_text_message(
                chat_id,
                "Kirishni davom ettirish uchun pastdagi tugma orqali telefon raqamingizni yuboring.",
                request_contact=True,
            )
            return {"ok": True}

        if not start_payload.startswith(TELEGRAM_REGISTRATION_DEEP_LINK_PREFIX):
            send_telegram_text_message(
                chat_id,
                "Ro'yxatdan o'tish uchun saytdagi Telegram tugmasini bosing va qaytadan urinib ko'ring.",
            )
            return {"ok": True}

        state = start_payload.removeprefix(TELEGRAM_REGISTRATION_DEEP_LINK_PREFIX).strip()
        registration_session = get_customer_registration_session(state)
        if not registration_session:
            send_telegram_text_message(
                chat_id,
                "Sessiya tugagan yoki topilmadi. Saytdan ro'yxatdan o'tishni qaytadan boshlang.",
            )
            return {"ok": True}

        mark_customer_registration_session_awaiting_contact(
            state=state,
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            telegram_chat_id=chat_id,
        )
        send_telegram_text_message(
            chat_id,
            "Ro'yxatdan o'tishni davom ettirish uchun pastdagi tugma orqali telefon raqamingizni yuboring.",
            request_contact=True,
        )
        return {"ok": True}

    if contact and telegram_id:
        logger.info(
            "Received Telegram contact for chat_id=%s telegram_id=%s",
            chat_id or None,
            telegram_id,
        )
        contact_user_id = str(contact.get("user_id") or "").strip()
        if contact_user_id and contact_user_id != telegram_id:
            send_telegram_text_message(
                chat_id,
                "Faqat o'zingizning telefon raqamingizni yuborishingiz mumkin.",
            )
            return {"ok": True}

        login_session = get_latest_customer_login_session_by_telegram_id(telegram_id)
        if login_session:
            state = str(login_session["state"] or "").strip()
            normalized_phone = normalize_phone_number(contact.get("phone_number"))
            if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
                mark_customer_login_session_failed(
                    state,
                    "Faqat O'zbekiston telefon raqami bilan kirish mumkin.",
                )
                send_telegram_text_message(
                    chat_id,
                    "Faqat O'zbekiston telefon raqami bilan kirish mumkin.",
                    remove_keyboard=True,
                )
                return {"ok": True}

            customer = get_customer_by_phone(normalized_phone)
            if not customer:
                mark_customer_login_session_failed(
                    state,
                    "Bu telefon raqami bilan akkaunt topilmadi.",
                )
                send_telegram_text_message(
                    chat_id,
                    "Bu telefon raqami bilan akkaunt topilmadi.",
                    remove_keyboard=True,
                )
                return {"ok": True}

            try:
                customer = update_customer_telegram_by_phone(
                    phone=normalized_phone,
                    telegram_id=telegram_id,
                    telegram_username=telegram_username,
                ) or customer
            except ValueError:
                mark_customer_login_session_failed(
                    state,
                    "Telegram profilingiz boshqa akkauntga bog'langan.",
                )
                send_telegram_text_message(
                    chat_id,
                    "Telegram profilingiz boshqa akkauntga bog'langan.",
                    remove_keyboard=True,
                )
                return {"ok": True}

            complete_customer_login_session(
                state=state,
                phone=normalized_phone,
                telegram_id=telegram_id,
                telegram_username=telegram_username,
                customer_id=customer["id"],
            )
            send_telegram_text_message(
                chat_id,
                "Telefon raqamingiz tasdiqlandi. Endi saytga qaytib kirishni yakunlang.",
                remove_keyboard=True,
            )
            return {"ok": True}

        registration_session = get_latest_customer_registration_session_by_telegram_id(telegram_id)
        if not registration_session:
            send_telegram_text_message(
                chat_id,
                "Aktiv sessiya topilmadi. Avval saytdan qaytadan boshlang.",
            )
            return {"ok": True}

        state = str(registration_session["state"] or "").strip()
        normalized_phone = normalize_phone_number(contact.get("phone_number"))
        if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
            mark_customer_registration_session_failed(
                state,
                "Faqat O'zbekiston telefon raqami bilan ro'yxatdan o'tish mumkin.",
            )
            send_telegram_text_message(
                chat_id,
                "Faqat O'zbekiston telefon raqami bilan ro'yxatdan o'tish mumkin.",
                remove_keyboard=True,
            )
            return {"ok": True}

        existing_customer = get_customer_record_by_phone(normalized_phone)
        if existing_customer and str(existing_customer["password_hash"] or "").strip():
            mark_customer_registration_session_failed(
                state,
                "Bu telefon raqami bilan akkaunt allaqachon mavjud.",
            )
            send_telegram_text_message(
                chat_id,
                "Bu telefon raqami bilan akkaunt allaqachon mavjud.",
                remove_keyboard=True,
            )
            return {"ok": True}

        try:
            customer = save_customer_account(
                name=str(registration_session["name"] or "").strip(),
                phone=normalized_phone,
                password_hash=str(registration_session["password_hash"] or "").strip(),
                telegram_username=telegram_username,
                telegram_id=telegram_id,
            )
        except ValueError:
            mark_customer_registration_session_failed(
                state,
                "Telegram profilingiz boshqa akkauntga bog'langan.",
            )
            send_telegram_text_message(
                chat_id,
                "Telegram profilingiz boshqa akkauntga bog'langan.",
                remove_keyboard=True,
            )
            return {"ok": True}

        complete_customer_registration_session(
            state=state,
            phone=normalized_phone,
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            customer_id=customer["id"],
        )
        send_telegram_text_message(
            chat_id,
            "Telefon raqamingiz tasdiqlandi. Endi saytga qaytib ro'yxatdan o'tishni yakunlang.",
            remove_keyboard=True,
        )
        return {"ok": True}

    return {"ok": True}


@router.post("/register", include_in_schema=False)
def register_customer(payload: CustomerRegisterPayload):
    raise HTTPException(
        status_code=403,
        detail="Registration is only available through Telegram verification",
    )


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
@router.get("/login/telegram/config")
def get_telegram_bot_login_config():
    return {
        "enabled": is_telegram_registration_configured(),
        "bot_username": TELEGRAM_LOGIN_BOT_USERNAME or None,
    }

