import base64
import binascii
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import (
    CLICK_MERCHANT_ID,
    CLICK_MERCHANT_USER_ID,
    CLICK_PAYMENT_BASE_URL,
    CLICK_RETURN_URL,
    CLICK_SECRET_KEY,
    CLICK_SERVICE_ID,
    MYID_BASE_URL,
    MYID_CLIENT_ID,
    MYID_CLIENT_SECRET,
    MYID_MAX_RETRIES,
    MYID_METHOD,
    MYID_REDIRECT_URL,
    MYID_SCOPE,
    MYID_WEB_BASE_URL,
)
from ..orders_database import (
    create_order,
    get_order,
    get_order_by_prepare_id,
    mark_order_cancelled,
    mark_order_completed,
    update_order,
)

router = APIRouter()
click_router = APIRouter(prefix="/click")

CLICK_ERROR_SIGNATURE = -1
CLICK_ERROR_AMOUNT = -2
CLICK_ERROR_ACTION = -3
CLICK_ERROR_ALREADY_PAID = -4
CLICK_ERROR_ORDER_NOT_FOUND = -5
CLICK_ERROR_TRANSACTION_NOT_FOUND = -6
CLICK_ERROR_REQUEST = -8
CLICK_ERROR_CANCELLED = -9

MYID_CLIENT_TOKEN_CACHE: dict[str, Any] = {
    "access_token": "",
    "expires_at": None,
}

MYID_REASON_CODE_LABELS = {
    -1: "User did not consent to personal data processing",
    2: "Passport data was entered incorrectly",
    3: "Liveness check failed",
    4: "Face recognition failed",
    5: "State service is unavailable or working incorrectly",
    6: "User is deceased",
    7: "Government photo was not received",
    11: "Service cannot process the request right now",
    18: "Liveness service cannot process the request",
    19: "Face recognition request could not be processed",
    20: "Poor or blurry image",
    21: "Face is not fully visible in the frame",
    22: "Multiple faces detected in the frame",
    23: "Grayscale image provided, color image required",
    24: "Dark glasses detected",
    26: "Eyes are closed or not visible",
    27: "Head is turned sideways",
    28: "Face was not detected or only partially detected",
    29: "Minor artifact, finger, device or reflection detected",
    30: "Face is partially covered",
    31: "The central face is not the largest in the frame",
    34: "Identity document has expired",
}


class ClickCheckoutPayload(BaseModel):
    name: str
    phone: str
    products: list[dict[str, Any]] = Field(default_factory=list)
    total: float = 0
    locale: str = "uz"
    return_url: str | None = None


class MyIdCheckoutPayload(BaseModel):
    name: str
    phone: str
    products: list[dict[str, Any]] = Field(default_factory=list)
    total: float = 0
    locale: str = "uz"
    pinfl: str | None = None
    pass_data: str | None = None
    birth_date: str
    redirect_uri: str | None = None
    is_resident: bool | None = None
    lang: str | None = None


class MyIdFinalizePayload(BaseModel):
    session_id: str | None = None
    order_id: int | None = None
    auth_code: str | None = None
    reason_code: int | None = None


class MyIdSessionClosePayload(BaseModel):
    code: int = 3


def _normalize_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("998") and len(digits) == 12:
        return digits
    if len(digits) == 9:
        return f"998{digits}"
    return digits


def _click_is_configured() -> bool:
    return (
        CLICK_SERVICE_ID > 0
        and CLICK_MERCHANT_ID > 0
        and bool(CLICK_SECRET_KEY)
        and bool(CLICK_PAYMENT_BASE_URL)
    )


def _myid_is_configured() -> bool:
    return (
        bool(MYID_BASE_URL)
        and bool(MYID_WEB_BASE_URL)
        and bool(MYID_CLIENT_ID)
        and bool(MYID_CLIENT_SECRET)
    )


def _build_click_amount(value: float | int | str) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "0"

    if amount == amount.to_integral_value():
        return str(int(amount))

    normalized = amount.quantize(Decimal("0.01")).normalize()
    return format(normalized, "f")


def _amounts_match(order_total: Any, click_amount: Any) -> bool:
    try:
        left = Decimal(str(order_total))
        right = Decimal(str(click_amount))
    except (InvalidOperation, ValueError):
        return False

    return left == right


def _extract_upstream_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "error_description"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value

        errors = payload.get("errors")
        if isinstance(errors, list):
            rendered = [str(item).strip() for item in errors if str(item).strip()]
            if rendered:
                return "; ".join(rendered)

    text = response.text.strip()
    return text[:500] if text else "Upstream request failed."


def _build_return_url(base_url: str | None, order_id: int) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return ""

    separator = "&" if "?" in normalized else "?"
    return f"{normalized}{separator}order_id={order_id}&payment=click"


def _resolve_return_url(request: Request, explicit_url: str | None, order_id: int) -> str:
    if explicit_url:
        return _build_return_url(explicit_url, order_id)

    if CLICK_RETURN_URL:
        return _build_return_url(CLICK_RETURN_URL, order_id)

    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        referer = str(request.headers.get("referer") or "").strip()
        if referer.startswith("http://") or referer.startswith("https://"):
            origin = referer.split("://", 1)[0] + "://" + referer.split("://", 1)[1].split("/", 1)[0]

    if not origin:
        return ""

    return _build_return_url(f"{origin}/checkout", order_id)


def _build_click_payment_url(order_id: int, total: float, return_url: str) -> str:
    params = {
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "amount": _build_click_amount(total),
        "transaction_param": order_id,
    }

    if CLICK_MERCHANT_USER_ID:
        params["merchant_user_id"] = CLICK_MERCHANT_USER_ID

    if return_url:
        params["return_url"] = return_url

    return f"{CLICK_PAYMENT_BASE_URL}?{urlencode(params)}"


def _normalize_myid_lang(value: Any) -> str:
    lang = str(value or "").strip().lower()
    return lang if lang in {"uz", "ru", "en", "eng"} else "ru"


def _normalize_pass_data(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum())


def _normalize_birth_date(value: Any) -> str:
    raw = str(value or "").strip()
    parts = raw.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return ""

    year, month, day = (int(part) for part in parts)
    if year <= 1900 or month < 1 or month > 12 or day < 1 or day > 31:
        return ""

    return f"{year:04d}-{month:02d}-{day:02d}"


def _get_request_origin(request: Request) -> str:
    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    if origin:
        return origin

    referer = str(request.headers.get("referer") or "").strip()
    if referer.startswith("http://") or referer.startswith("https://"):
        return referer.split("://", 1)[0] + "://" + referer.split("://", 1)[1].split("/", 1)[0]

    return ""


def _resolve_myid_redirect_uri(request: Request, explicit_url: str | None) -> str:
    configured = str(explicit_url or "").strip() or MYID_REDIRECT_URL
    if configured:
        return configured

    origin = _get_request_origin(request)
    return f"{origin}/checkout" if origin else ""


def _extract_client_ip(request: Request) -> str:
    cloudflare_ip = str(request.headers.get("cf-connecting-ip") or "").strip()
    if cloudflare_ip:
        return cloudflare_ip

    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip

    real_ip = str(request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip

    return str(request.client.host).strip() if request.client else ""


def _myid_cached_client_token() -> str:
    access_token = str(MYID_CLIENT_TOKEN_CACHE.get("access_token") or "").strip()
    expires_at = MYID_CLIENT_TOKEN_CACHE.get("expires_at")

    if not access_token or not isinstance(expires_at, datetime):
        return ""

    now = datetime.now(timezone.utc)
    if expires_at <= now + timedelta(seconds=30):
        return ""

    return access_token


def _store_myid_client_token(access_token: str, expires_in: Any) -> None:
    try:
        ttl_seconds = int(expires_in)
    except (TypeError, ValueError):
        ttl_seconds = 3600

    MYID_CLIENT_TOKEN_CACHE["access_token"] = access_token
    MYID_CLIENT_TOKEN_CACHE["expires_at"] = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, ttl_seconds)
    )


def _myid_request_access_token(
    *,
    grant_type: str,
    code: str | None = None,
) -> dict[str, Any]:
    if not _myid_is_configured():
        raise HTTPException(status_code=500, detail="MyID integration is not configured")

    if grant_type == "client_credentials":
        cached_token = _myid_cached_client_token()
        if cached_token:
            return {
                "access_token": cached_token,
                "token_type": "Bearer",
                "expires_in": 0,
                "cached": True,
            }

    form_data: dict[str, Any] = {
        "grant_type": grant_type,
        "client_id": MYID_CLIENT_ID,
        "client_secret": MYID_CLIENT_SECRET,
    }

    if grant_type == "authorization_code":
        form_data["code"] = str(code or "").strip()
        form_data["method"] = MYID_METHOD
        form_data["scope"] = MYID_SCOPE

    try:
        response = requests.post(
            f"{MYID_BASE_URL}/api/v1/oauth2/access-token",
            data=form_data,
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"MyID token request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        raise HTTPException(status_code=502, detail=f"MyID token request failed: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MyID token response is invalid") from exc

    if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
        raise HTTPException(status_code=502, detail="MyID token response is incomplete")

    if grant_type == "client_credentials":
        _store_myid_client_token(
            str(payload.get("access_token") or "").strip(),
            payload.get("expires_in"),
        )

    return payload


def _myid_create_session(request: Request) -> tuple[str, str]:
    token_payload = _myid_request_access_token(grant_type="client_credentials")
    access_token = str(token_payload.get("access_token") or "").strip()
    external_id = str(uuid4())
    payload = {
        "max_retries": max(1, MYID_MAX_RETRIES),
        "external_id": external_id,
        "ip_address": _extract_client_ip(request),
    }

    try:
        response = requests.post(
            f"{MYID_BASE_URL}/api/v1/web/sessions",
            json=payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"MyID session creation failed: {exc}") from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        raise HTTPException(status_code=502, detail=f"MyID session creation failed: {detail}")

    try:
        session_payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MyID session response is invalid") from exc

    session_id = str(session_payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=502, detail="MyID session_id was not returned")

    return session_id, external_id


def _build_myid_redirect_url(
    *,
    session_id: str,
    pinfl: str,
    pass_data: str,
    birth_date: str,
    redirect_uri: str,
    is_resident: bool | None,
    lang: str,
) -> str:
    params: dict[str, Any] = {
        "session_id": session_id,
        "birth_date": birth_date,
        "redirect_uri": redirect_uri,
        "lang": _normalize_myid_lang(lang),
    }

    if pinfl:
        params["pinfl"] = pinfl
    elif pass_data:
        params["pass_data"] = pass_data

    if is_resident is False:
        params["is_resident"] = "false"

    return f"{MYID_WEB_BASE_URL}/?{urlencode(params)}"


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    token_parts = str(token or "").split(".")
    if len(token_parts) < 2:
        return {}

    padded = token_parts[1].replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)

    try:
        payload = base64.b64decode(padded)
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return {}

    return decoded if isinstance(decoded, dict) else {}


def _myid_fetch_user_profile(access_token: str) -> dict[str, Any]:
    try:
        response = requests.get(
            f"{MYID_BASE_URL}/api/v1/users/me",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"MyID profile request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        raise HTTPException(status_code=502, detail=f"MyID profile request failed: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MyID profile response is invalid") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="MyID profile response is invalid")

    return payload


def _myid_client_bearer_headers() -> dict[str, str]:
    token_payload = _myid_request_access_token(grant_type="client_credentials")
    access_token = str(token_payload.get("access_token") or "").strip()
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


def _myid_fetch_session_result(session_id: str) -> dict[str, Any]:
    try:
        response = requests.post(
            f"{MYID_BASE_URL}/api/v1/web/sessions/{session_id}/result",
            headers=_myid_client_bearer_headers(),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"MyID session result request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        raise HTTPException(status_code=502, detail=f"MyID session result request failed: {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MyID session result response is invalid") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="MyID session result response is invalid")

    return payload


def _myid_close_session(session_id: str, close_code: int) -> dict[str, Any]:
    try:
        response = requests.post(
            f"{MYID_BASE_URL}/api/v1/web/sessions/{session_id}/client/close",
            json={"code": close_code},
            headers=_myid_client_bearer_headers(),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"MyID session close request failed: {exc}") from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        raise HTTPException(status_code=502, detail=f"MyID session close request failed: {detail}")

    if not response.text.strip():
        return {"success": True}

    try:
        payload = response.json()
    except ValueError:
        return {"success": True, "raw": response.text.strip()[:500]}

    return payload if isinstance(payload, dict) else {"success": True}


def _build_myid_reason_note(reason_code: int) -> str:
    return MYID_REASON_CODE_LABELS.get(reason_code, "MyID verification was not completed")


def _build_prepare_signature(data: dict[str, Any]) -> str:
    raw = (
        f"{data.get('click_trans_id', '')}"
        f"{data.get('service_id', '')}"
        f"{CLICK_SECRET_KEY}"
        f"{data.get('merchant_trans_id', '')}"
        f"{data.get('amount', '')}"
        f"{data.get('action', '')}"
        f"{data.get('sign_time', '')}"
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _build_complete_signature(data: dict[str, Any]) -> str:
    raw = (
        f"{data.get('click_trans_id', '')}"
        f"{data.get('service_id', '')}"
        f"{CLICK_SECRET_KEY}"
        f"{data.get('merchant_trans_id', '')}"
        f"{data.get('merchant_prepare_id', '')}"
        f"{data.get('amount', '')}"
        f"{data.get('action', '')}"
        f"{data.get('sign_time', '')}"
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _validate_signature(data: dict[str, Any], expected_action: int) -> bool:
    provided = str(data.get("sign_string") or "").strip().lower()
    expected = (
        _build_prepare_signature(data)
        if expected_action == 0
        else _build_complete_signature(data)
    ).lower()
    return bool(provided) and provided == expected


def _build_prepare_response(
    *,
    click_trans_id: str,
    merchant_trans_id: str,
    merchant_prepare_id: int | None,
    error: int,
    error_note: str,
) -> dict[str, Any]:
    return {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_prepare_id": merchant_prepare_id,
        "error": error,
        "error_note": error_note,
    }


def _build_complete_response(
    *,
    click_trans_id: str,
    merchant_trans_id: str,
    merchant_confirm_id: int | None,
    error: int,
    error_note: str,
) -> dict[str, Any]:
    return {
        "click_trans_id": click_trans_id,
        "merchant_trans_id": merchant_trans_id,
        "merchant_confirm_id": merchant_confirm_id,
        "error": error,
        "error_note": error_note,
    }


def _prepare_error(
    data: dict[str, Any],
    error: int,
    error_note: str,
    merchant_prepare_id: int | None = None,
) -> dict[str, Any]:
    return _build_prepare_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(data.get("merchant_trans_id") or ""),
        merchant_prepare_id=merchant_prepare_id,
        error=error,
        error_note=error_note,
    )


def _complete_error(
    data: dict[str, Any],
    error: int,
    error_note: str,
    merchant_confirm_id: int | None = None,
) -> dict[str, Any]:
    return _build_complete_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(data.get("merchant_trans_id") or ""),
        merchant_confirm_id=merchant_confirm_id,
        error=error,
        error_note=error_note,
    )


def _parse_order_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() else None


async def _read_click_form(request: Request) -> dict[str, Any]:
    form = await request.form()
    return {key: value for key, value in form.items()}


@router.get("/click/meta")
def get_click_meta() -> dict[str, Any]:
    return {
        "enabled": _click_is_configured(),
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
    }


@router.get("/myid/meta")
def get_myid_meta() -> dict[str, Any]:
    return {
        "enabled": _myid_is_configured(),
        "base_url": MYID_WEB_BASE_URL,
    }


@router.get("/orders/{order_id}")
def get_public_order_status(order_id: int) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    payment_method = order.get("payment_method") or "cash"
    status_note = ""

    if payment_method == "click":
        status_note = order.get("click_error_note") or ""
    elif payment_method == "myid":
        status_note = order.get("myid_result_note") or ""

    return {
        "id": order["id"],
        "status": order["status"],
        "payment_method": payment_method,
        "total": order.get("total") or 0,
        "click_error": order.get("click_error"),
        "click_error_note": order.get("click_error_note") or "",
        "myid_result_code": order.get("myid_result_code"),
        "myid_result_note": order.get("myid_result_note") or "",
        "status_note": status_note,
    }


@router.post("/click/initiate")
def initiate_click_payment(payload: ClickCheckoutPayload, request: Request) -> dict[str, Any]:
    name = str(payload.name or "").strip()
    phone = _normalize_phone(payload.phone)
    products = payload.products or []

    if not _click_is_configured():
        raise HTTPException(status_code=500, detail="CLICK integration is not configured")

    if not name or len(phone) != 12 or not products:
        raise HTTPException(status_code=400, detail="Name, phone and products are required")

    if float(payload.total or 0) <= 0:
        raise HTTPException(status_code=400, detail="Order total must be greater than zero")

    order = create_order(
        name=name,
        phone=phone,
        products=products,
        total=float(payload.total or 0),
        locale=(payload.locale or "uz").strip() or "uz",
        status="pending",
        payment_method="click",
    )

    merchant_prepare_id = int(order["id"])
    return_url = _resolve_return_url(request, payload.return_url, int(order["id"]))
    payment_url = _build_click_payment_url(int(order["id"]), float(order["total"] or 0), return_url)

    order = update_order(
        int(order["id"]),
        merchant_prepare_id=merchant_prepare_id,
        click_error=0,
        click_error_note="",
    ) or order

    return {
        "success": True,
        "order_id": order["id"],
        "merchant_prepare_id": merchant_prepare_id,
        "payment_url": payment_url,
        "return_url": return_url,
    }


@router.post("/myid/initiate")
def initiate_myid_payment(payload: MyIdCheckoutPayload, request: Request) -> dict[str, Any]:
    name = str(payload.name or "").strip()
    phone = _normalize_phone(payload.phone)
    products = payload.products or []
    pinfl = "".join(ch for ch in str(payload.pinfl or "") if ch.isdigit())
    pass_data = _normalize_pass_data(payload.pass_data)
    birth_date = _normalize_birth_date(payload.birth_date)
    redirect_uri = _resolve_myid_redirect_uri(request, payload.redirect_uri)

    if not _myid_is_configured():
        raise HTTPException(status_code=500, detail="MyID integration is not configured")

    if not name or len(phone) != 12 or not products:
        raise HTTPException(status_code=400, detail="Name, phone and products are required")

    if float(payload.total or 0) <= 0:
        raise HTTPException(status_code=400, detail="Order total must be greater than zero")

    if not birth_date or not (len(pinfl) == 14 or pass_data):
        raise HTTPException(
            status_code=400,
            detail="PINFL or passport data together with birth date is required",
        )

    if not redirect_uri:
        raise HTTPException(status_code=400, detail="MyID redirect URI could not be resolved")

    session_id, external_id = _myid_create_session(request)
    redirect_url = _build_myid_redirect_url(
        session_id=session_id,
        pinfl=pinfl,
        pass_data=pass_data,
        birth_date=birth_date,
        redirect_uri=redirect_uri,
        is_resident=payload.is_resident,
        lang=payload.lang or payload.locale,
    )

    return {
        "success": True,
        "session_id": session_id,
        "external_id": external_id,
        "redirect_uri": redirect_uri,
        "redirect_url": redirect_url,
    }


@router.post("/myid/finalize")
def finalize_myid_payment(payload: MyIdFinalizePayload) -> dict[str, Any]:
    session_id = str(payload.session_id or "").strip()

    if payload.reason_code is not None and not str(payload.auth_code or "").strip():
        reason_note = _build_myid_reason_note(int(payload.reason_code))
        return {
            "success": False,
            "status": "cancelled",
            "payment_method": "myid",
            "session_id": session_id,
            "result_code": int(payload.reason_code),
            "result_note": reason_note,
        }

    auth_code = str(payload.auth_code or "").strip()
    if not auth_code and session_id:
        session_result = _myid_fetch_session_result(session_id)
        auth_code = str(session_result.get("auth_code") or "").strip()

    if not auth_code:
        raise HTTPException(status_code=400, detail="MyID auth_code is required")

    token_payload = _myid_request_access_token(
        grant_type="authorization_code",
        code=auth_code,
    )
    access_token = str(token_payload.get("access_token") or "").strip()
    profile = _myid_fetch_user_profile(access_token)
    job_id = str(_decode_jwt_payload(access_token).get("job_id") or "").strip()

    return {
        "success": True,
        "status": "completed",
        "payment_method": "myid",
        "session_id": session_id,
        "job_id": job_id,
        "result_code": 1,
        "result_note": "MyID verification completed successfully",
        "profile": profile,
    }


@router.post("/myid/sessions/{session_id}/result")
def get_myid_session_result(session_id: str) -> dict[str, Any]:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="MyID session_id is required")

    session_result = _myid_fetch_session_result(normalized_session_id)
    jobs = session_result.get("jobs")
    auth_code = str(session_result.get("auth_code") or "").strip()

    return {
        "success": True,
        "session_id": normalized_session_id,
        "auth_code": auth_code,
        "jobs": jobs if isinstance(jobs, list) else [],
    }


@router.post("/myid/sessions/{session_id}/close")
def close_myid_session(session_id: str, payload: MyIdSessionClosePayload) -> dict[str, Any]:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise HTTPException(status_code=400, detail="MyID session_id is required")

    response_payload = _myid_close_session(normalized_session_id, int(payload.code or 3))
    return {
        "success": True,
        "session_id": normalized_session_id,
        "response": response_payload,
    }


def _handle_prepare(data: dict[str, Any]) -> dict[str, Any]:
    if not _click_is_configured():
        return _prepare_error(data, CLICK_ERROR_REQUEST, "CLICK integration is not configured")

    action = str(data.get("action") or "").strip()
    if action != "0":
        return _prepare_error(data, CLICK_ERROR_ACTION, "Invalid action")

    if str(data.get("service_id") or "").strip() != str(CLICK_SERVICE_ID):
        return _prepare_error(data, CLICK_ERROR_REQUEST, "Invalid service ID")

    if not _validate_signature(data, 0):
        return _prepare_error(data, CLICK_ERROR_SIGNATURE, "Invalid signature")

    order_id = _parse_order_id(data.get("merchant_trans_id"))
    if order_id is None:
        return _prepare_error(data, CLICK_ERROR_ORDER_NOT_FOUND, "Order not found")

    order = get_order(order_id)
    if not order or str(order.get("payment_method") or "") != "click":
        return _prepare_error(data, CLICK_ERROR_ORDER_NOT_FOUND, "Order not found")

    if not _amounts_match(order.get("total") or 0, data.get("amount") or 0):
        return _prepare_error(
            data,
            CLICK_ERROR_AMOUNT,
            "Incorrect amount",
            merchant_prepare_id=order.get("merchant_prepare_id"),
        )

    if str(order.get("status") or "").strip() == "completed":
        return _prepare_error(
            data,
            CLICK_ERROR_ALREADY_PAID,
            "Order already paid",
            merchant_prepare_id=order.get("merchant_prepare_id") or order["id"],
        )

    if str(order.get("status") or "").strip() == "cancelled":
        return _prepare_error(
            data,
            CLICK_ERROR_CANCELLED,
            "Order cancelled",
            merchant_prepare_id=order.get("merchant_prepare_id") or order["id"],
        )

    merchant_prepare_id = int(order.get("merchant_prepare_id") or order["id"])
    updated = update_order(
        int(order["id"]),
        status="prepared",
        click_trans_id=str(data.get("click_trans_id") or ""),
        click_paydoc_id=str(data.get("click_paydoc_id") or ""),
        merchant_prepare_id=merchant_prepare_id,
        click_error=0,
        click_error_note="",
    ) or order

    return _build_prepare_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(updated["id"]),
        merchant_prepare_id=merchant_prepare_id,
        error=0,
        error_note="Success",
    )


def _handle_complete(data: dict[str, Any]) -> dict[str, Any]:
    if not _click_is_configured():
        return _complete_error(data, CLICK_ERROR_REQUEST, "CLICK integration is not configured")

    action = str(data.get("action") or "").strip()
    if action != "1":
        return _complete_error(data, CLICK_ERROR_ACTION, "Invalid action")

    if str(data.get("service_id") or "").strip() != str(CLICK_SERVICE_ID):
        return _complete_error(data, CLICK_ERROR_REQUEST, "Invalid service ID")

    if not _validate_signature(data, 1):
        return _complete_error(data, CLICK_ERROR_SIGNATURE, "Invalid signature")

    order_id = _parse_order_id(data.get("merchant_trans_id"))
    merchant_prepare_id = _parse_order_id(data.get("merchant_prepare_id"))

    if order_id is None or merchant_prepare_id is None:
        return _complete_error(data, CLICK_ERROR_TRANSACTION_NOT_FOUND, "Transaction not found")

    order = get_order(order_id)
    if not order or str(order.get("payment_method") or "") != "click":
        return _complete_error(data, CLICK_ERROR_ORDER_NOT_FOUND, "Order not found")

    prepared_order = get_order_by_prepare_id(merchant_prepare_id)
    if not prepared_order or int(prepared_order["id"]) != int(order["id"]):
        return _complete_error(data, CLICK_ERROR_TRANSACTION_NOT_FOUND, "Transaction not found")

    if not _amounts_match(order.get("total") or 0, data.get("amount") or 0):
        return _complete_error(
            data,
            CLICK_ERROR_AMOUNT,
            "Incorrect amount",
            merchant_confirm_id=order.get("merchant_confirm_id"),
        )

    current_status = str(order.get("status") or "").strip()
    if current_status == "completed":
        return _complete_error(
            data,
            CLICK_ERROR_ALREADY_PAID,
            "Order already paid",
            merchant_confirm_id=order.get("merchant_confirm_id") or order["id"],
        )

    if current_status == "cancelled":
        return _complete_error(
            data,
            CLICK_ERROR_CANCELLED,
            "Order cancelled",
            merchant_confirm_id=order.get("merchant_confirm_id") or order["id"],
        )

    click_error = int(str(data.get("error") or "0").strip() or "0")
    error_note = str(data.get("error_note") or "").strip()

    if click_error <= -1:
        cancelled = mark_order_cancelled(
            int(order["id"]),
            click_error=click_error,
            click_error_note=error_note or "Payment cancelled by CLICK",
        ) or order
        update_order(
            int(cancelled["id"]),
            click_trans_id=str(data.get("click_trans_id") or ""),
            click_paydoc_id=str(data.get("click_paydoc_id") or ""),
            merchant_prepare_id=merchant_prepare_id,
        )
        return _complete_error(
            data,
            CLICK_ERROR_CANCELLED,
            error_note or "Payment cancelled by CLICK",
            merchant_confirm_id=cancelled.get("merchant_confirm_id"),
        )

    completed = mark_order_completed(int(order["id"])) or order
    merchant_confirm_id = int(completed.get("merchant_confirm_id") or completed["id"])
    completed = update_order(
        int(order["id"]),
        click_trans_id=str(data.get("click_trans_id") or ""),
        click_paydoc_id=str(data.get("click_paydoc_id") or ""),
        merchant_prepare_id=merchant_prepare_id,
        merchant_confirm_id=merchant_confirm_id,
        click_error=0,
        click_error_note="Success",
    ) or completed

    return _build_complete_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(completed["id"]),
        merchant_confirm_id=merchant_confirm_id,
        error=0,
        error_note="Success",
    )


@router.post("/click/prepare")
async def click_prepare(request: Request) -> dict[str, Any]:
    data = await _read_click_form(request)
    return _handle_prepare(data)


@router.post("/click/complete")
async def click_complete(request: Request) -> dict[str, Any]:
    data = await _read_click_form(request)
    return _handle_complete(data)


@click_router.post("/prepare")
async def click_prepare_alias(request: Request) -> dict[str, Any]:
    data = await _read_click_form(request)
    return _handle_prepare(data)


@click_router.post("/complete")
async def click_complete_alias(request: Request) -> dict[str, Any]:
    data = await _read_click_form(request)
    return _handle_complete(data)


@router.post("/click/callback")
async def click_callback(request: Request) -> dict[str, Any]:
    data = await _read_click_form(request)
    action = str(data.get("action") or "").strip()
    if action == "0":
        return _handle_prepare(data)
    if action == "1":
        return _handle_complete(data)
    return _complete_error(data, CLICK_ERROR_ACTION, "Invalid action")
