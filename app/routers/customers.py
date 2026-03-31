import base64
import hashlib
import hmac
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt  # type: ignore
import requests  # type: ignore
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import verify_token
from ..config import (
    TELEGRAM_LOGIN_BOT_TOKEN,
    TELEGRAM_LOGIN_BOT_USERNAME,
    TELEGRAM_LOGIN_CLIENT_ID,
    TELEGRAM_LOGIN_CLIENT_SECRET,
)
from ..customers_database import (
    authenticate_customer,
    delete_customer_registration_session,
    get_all_customers,
    get_customer_record_by_phone,
    get_customer_registration_session,
    hash_password,
    normalize_telegram_username,
    save_customer_registration_session,
    save_or_update_customer_from_telegram,
    save_customer_account,
    update_customer_password_by_phone,
)

router = APIRouter()
TELEGRAM_OIDC_AUTH_URL = "https://oauth.telegram.org/auth"
TELEGRAM_OIDC_TOKEN_URL = "https://oauth.telegram.org/token"
TELEGRAM_OIDC_JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
TELEGRAM_OIDC_ISSUER = "https://oauth.telegram.org"
TELEGRAM_OIDC_SCOPE = "openid profile phone"
telegram_jwk_client = jwt.PyJWKClient(TELEGRAM_OIDC_JWKS_URL)


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


def validate_redirect_uri(value: str) -> str:
    redirect_uri = str(value or "").strip()
    parsed_uri = urlparse(redirect_uri)

    if parsed_uri.scheme not in {"http", "https"} or not parsed_uri.netloc:
        raise HTTPException(
            status_code=400,
            detail="Redirect URI is invalid",
        )

    return redirect_uri


def is_telegram_registration_configured() -> bool:
    return bool(TELEGRAM_LOGIN_CLIENT_ID and TELEGRAM_LOGIN_CLIENT_SECRET)


def build_pkce_challenge(code_verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("utf-8")).digest()
    ).decode("utf-8").rstrip("=")


def build_telegram_registration_url(
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    return (
        f"{TELEGRAM_OIDC_AUTH_URL}?"
        + urlencode(
            {
                "client_id": TELEGRAM_LOGIN_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": TELEGRAM_OIDC_SCOPE,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def exchange_telegram_code_for_id_token(
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    credentials = base64.b64encode(
        f"{TELEGRAM_LOGIN_CLIENT_ID}:{TELEGRAM_LOGIN_CLIENT_SECRET}".encode("utf-8")
    ).decode("utf-8")

    try:
        response = requests.post(
            TELEGRAM_OIDC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": TELEGRAM_LOGIN_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
    except requests.RequestException as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram token exchange failed",
        ) from error

    if not response.ok:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        detail = (
            payload.get("error_description")
            or payload.get("error")
            or "Telegram token exchange failed"
        )
        raise HTTPException(status_code=502, detail=str(detail))

    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram token response is invalid",
        ) from error

    id_token = str(payload.get("id_token") or "").strip()
    if not id_token:
        raise HTTPException(
            status_code=502,
            detail="Telegram token response is invalid",
        )

    return id_token


def decode_telegram_id_token(id_token: str, nonce: str) -> dict[str, Any]:
    try:
        signing_key = telegram_jwk_client.get_signing_key_from_jwt(id_token)
        algorithm = str(jwt.get_unverified_header(id_token).get("alg") or "RS256")
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=[algorithm],
            audience=TELEGRAM_LOGIN_CLIENT_ID,
            issuer=TELEGRAM_OIDC_ISSUER,
        )
    except jwt.InvalidTokenError as error:
        raise HTTPException(
            status_code=401,
            detail="Telegram ID token is invalid",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="Telegram identity could not be verified",
        ) from error

    if str(payload.get("nonce") or "").strip() != str(nonce or "").strip():
        raise HTTPException(
            status_code=401,
            detail="Telegram verification nonce mismatch",
        )

    return payload


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


class CustomerRegisterPayload(BaseModel):
    name: str
    phone: str
    password: str


class CustomerTelegramRegistrationStartPayload(BaseModel):
    name: str
    password: str
    redirect_uri: str


class CustomerTelegramRegistrationCompletePayload(BaseModel):
    code: str
    state: str


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


@router.get("/register/telegram/config")
def get_telegram_registration_config():
    return {
        "enabled": is_telegram_registration_configured(),
    }


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


@router.post(
    "/register/telegram/start",
    summary="Start Telegram registration",
    description="Starts the Telegram phone verification flow for customer registration.",
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
    redirect_uri = validate_redirect_uri(payload.redirect_uri)

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Name is required",
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = build_pkce_challenge(code_verifier)

    save_customer_registration_session(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        name=name,
        password_hash=hash_password(password),
    )

    return {
        "success": True,
        "auth_url": build_telegram_registration_url(
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=code_challenge,
        ),
    }


@router.post(
    "/register/telegram/complete",
    summary="Complete Telegram registration",
    description="Completes customer registration after Telegram redirects back with an authorization code.",
)
def complete_customer_registration_with_telegram(
    payload: CustomerTelegramRegistrationCompletePayload,
):
    if not is_telegram_registration_configured():
        raise HTTPException(
            status_code=503,
            detail="Telegram registration is not configured",
        )

    code = str(payload.code or "").strip()
    state = str(payload.state or "").strip()

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Telegram authorization payload is incomplete",
        )

    registration_session = get_customer_registration_session(state)
    if not registration_session:
        raise HTTPException(
            status_code=410,
            detail="Telegram registration session expired",
        )

    id_token = exchange_telegram_code_for_id_token(
        code=code,
        redirect_uri=str(registration_session["redirect_uri"] or "").strip(),
        code_verifier=str(registration_session["code_verifier"] or "").strip(),
    )
    telegram_payload = decode_telegram_id_token(
        id_token=id_token,
        nonce=str(registration_session["nonce"] or "").strip(),
    )

    normalized_phone = normalize_phone_number(telegram_payload.get("phone_number"))
    if len(normalized_phone) != 12 or not normalized_phone.startswith("998"):
        raise HTTPException(
            status_code=400,
            detail="Please confirm a valid Uzbekistan phone number in Telegram",
        )

    existing_customer = get_customer_record_by_phone(normalized_phone)
    if existing_customer and str(existing_customer["password_hash"] or "").strip():
        raise HTTPException(
            status_code=409,
            detail="This phone number is already registered",
        )

    try:
        customer = save_customer_account(
            name=str(registration_session["name"] or "").strip(),
            phone=normalized_phone,
            password_hash=str(registration_session["password_hash"] or "").strip(),
            telegram_username=str(telegram_payload.get("preferred_username") or "").strip()
            or None,
            telegram_id=str(telegram_payload.get("id") or "").strip() or None,
        )
    except ValueError as error:
        if str(error) in {"telegram_username_taken", "telegram_id_taken"}:
            raise HTTPException(
                status_code=409,
                detail="This Telegram account is already linked to another profile",
            ) from error
        raise

    delete_customer_registration_session(state)

    return {
        "success": True,
        "customer": customer,
    }


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
