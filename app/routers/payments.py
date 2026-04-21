import base64
import binascii
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock, Thread
from time import perf_counter, sleep
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..auth import verify_admin_token
from ..config import (
    CLICK_MERCHANT_ID,
    CLICK_MERCHANT_USER_ID,
    CLICK_PAYMENT_BASE_URL,
    CLICK_RETURN_URL,
    CLICK_SECRET_KEY,
    CLICK_SERVICE_ID,
    ICAN_CREDIT_API_URL,
    ICAN_CREDIT_COMPANY_ID,
    ICAN_CREDIT_CREATE_PATH,
    ICAN_CREDIT_EMPLOYEE_ID,
    ICAN_CREDIT_LIST_PATH,
    ICAN_CREDIT_PASSWORD,
    ICAN_CREDIT_TRANSACTION_CREATE_PATH,
    ICAN_CREDIT_USERNAME,
    MYID_BASE_URL,
    MYID_CLIENT_ID,
    MYID_CLIENT_SECRET,
    MYID_CONNECT_TIMEOUT,
    MYID_DEBUG_LOGS,
    MYID_HTTP_RETRY_COUNT,
    MYID_MAX_RETRIES,
    MYID_METHOD,
    MYID_READ_TIMEOUT,
    MYID_REDIRECT_URL,
    MYID_SCOPE,
    MYID_WEB_BASE_URL,
)
from ..database import get_db
from ..customers_database import (
    get_customer_by_id,
    get_customer_by_phone,
    update_customer_address_by_phone,
)
from ..myid_ican_locations import resolve_ican_location
from ..orders_database import (
    create_order,
    create_monthly_payment,
    delete_order_by_id,
    get_order,
    get_monthly_payment,
    get_monthly_payments_by_order_id,
    get_monthly_payments_by_phone,
    get_orders_by_phone,
    get_order_by_prepare_id,
    get_order_by_myid_session_id,
    mark_order_cancelled,
    mark_order_completed,
    update_order,
    update_monthly_payment,
)

router = APIRouter()
click_router = APIRouter(prefix="/click")
logger = logging.getLogger(__name__)
MYID_REQUEST_TIMEOUT = (MYID_CONNECT_TIMEOUT, MYID_READ_TIMEOUT)

CLICK_ERROR_SIGNATURE = -1
CLICK_ERROR_AMOUNT = -2
CLICK_ERROR_ACTION = -3
CLICK_ERROR_ALREADY_PAID = -4
CLICK_ERROR_ORDER_NOT_FOUND = -5
CLICK_ERROR_TRANSACTION_NOT_FOUND = -6
CLICK_ERROR_REQUEST = -8
CLICK_ERROR_CANCELLED = -9
MONTHLY_PAYMENT_CLICK_OFFSET = 9_000_000_000

MYID_CLIENT_TOKEN_CACHE: dict[str, Any] = {
    "access_token": "",
    "expires_at": None,
}
ICAN_ORDER_SYNC_LOCK = Lock()
ICAN_ORDER_SYNC_ATTEMPTS: dict[int, float] = {}
ICAN_ORDER_SYNC_INTERVAL_SECONDS = 15.0
ICAN_ORDER_SYNC_TIMEOUT = (2, 4)

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

CYRILLIC_TO_LATIN = {
    "А": "A",
    "Б": "B",
    "В": "V",
    "Г": "G",
    "Д": "D",
    "Е": "E",
    "Ё": "YO",
    "Ж": "ZH",
    "З": "Z",
    "И": "I",
    "Й": "Y",
    "К": "K",
    "Л": "L",
    "М": "M",
    "Н": "N",
    "О": "O",
    "П": "P",
    "Р": "R",
    "С": "S",
    "Т": "T",
    "У": "U",
    "Ф": "F",
    "Х": "KH",
    "Ц": "TS",
    "Ч": "CH",
    "Ш": "SH",
    "Щ": "SH",
    "Ъ": "",
    "Ы": "Y",
    "Ь": "",
    "Э": "E",
    "Ю": "YU",
    "Я": "YA",
    "Ў": "O",
    "Қ": "Q",
    "Ғ": "G",
    "Ҳ": "H",
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


class MonthlyPaymentInitiatePayload(BaseModel):
    amount: float | None = None
    phone: str | None = None
    return_url: str | None = None


class SubmitCreditPayload(BaseModel):
    phones: list[str] = Field(default_factory=list)


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
        errors = payload.get("errors")
        if isinstance(errors, list):
            rendered = [str(item).strip() for item in errors if str(item).strip()]
            if rendered:
                return "; ".join(rendered)
        if isinstance(errors, dict):
            rendered: list[str] = []
            for field, field_errors in errors.items():
                if isinstance(field_errors, list):
                    rendered.extend(
                        f"{field}: {str(item).strip()}"
                        for item in field_errors
                        if str(item).strip()
                    )
                else:
                    value = str(field_errors).strip()
                    if value:
                        rendered.append(f"{field}: {value}")
            if rendered:
                return "; ".join(rendered)

        for key in ("detail", "message", "error", "error_description"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value

    text = response.text.strip()
    return text[:500] if text else "Upstream request failed."


def _extract_ican_credit_number(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("number", "credit_number", "creditNumber"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

        for key in ("data", "result", "credit"):
            nested_value = _extract_ican_credit_number(payload.get(key))
            if nested_value:
                return nested_value

    if isinstance(payload, list):
        for item in payload:
            nested_value = _extract_ican_credit_number(item)
            if nested_value:
                return nested_value

    return ""


def _extract_ican_credit_status_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    status = payload.get("status")
    status_value = ""
    status_label = ""
    if isinstance(status, dict):
        status_value = str(status.get("value") or "").strip()
        status_label = str(status.get("label") or "").strip()
    elif status is not None:
        status_value = str(status).strip()

    return {
        "id": str(payload.get("id") or "").strip(),
        "number": str(payload.get("number") or "").strip(),
        "status": status_value,
        "status_label": status_label,
        "cancel_reason": str(payload.get("cancel_reason") or "").strip(),
        "comment": str(payload.get("comment") or "").strip(),
        "updated_at": payload.get("updated_at"),
        "created_at": payload.get("created_at"),
    }


def _fetch_ican_credit_details(
    order: dict[str, Any],
    *,
    live: bool = False,
) -> dict[str, Any]:
    stored_payload = order.get("ican_credit_payload") if isinstance(order.get("ican_credit_payload"), dict) else {}
    fallback_payload = _extract_ican_credit_status_payload(stored_payload)
    if fallback_payload:
        fallback_payload["source"] = "stored"
        if not fallback_payload.get("id"):
            fallback_payload["id"] = str(order.get("ican_credit_id") or "").strip()

    if not live or not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        return fallback_payload

    credit_number = _extract_ican_credit_number(stored_payload)
    credit_id = str(order.get("ican_credit_id") or "").strip()
    params: dict[str, str] = {}
    if credit_number:
        params["filter[number]"] = credit_number
    elif credit_id:
        params["filter[id]"] = credit_id
    else:
        return fallback_payload

    try:
        response = requests.get(
            f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_LIST_PATH}",
            params=params,
            auth=(ICAN_CREDIT_USERNAME, ICAN_CREDIT_PASSWORD),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru",
            },
            timeout=ICAN_ORDER_SYNC_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        return fallback_payload

    try:
        payload = response.json()
    except ValueError:
        return fallback_payload

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return fallback_payload

    latest_payload = _extract_ican_credit_status_payload(items[0])
    if not latest_payload:
        return fallback_payload

    latest_payload["source"] = "live"
    if not latest_payload.get("number") and credit_number:
        latest_payload["number"] = credit_number
    if not latest_payload.get("id") and credit_id:
        latest_payload["id"] = credit_id
    return latest_payload


def _refresh_order_ican_credit_details(order_id: int) -> None:
    try:
        order = get_order(order_id)
        if not order:
            return

        latest_payload = _fetch_ican_credit_details(order, live=True)
        if latest_payload.get("source") != "live":
            return

        payload_to_store = {
            key: value for key, value in latest_payload.items() if key != "source"
        }
        stored_payload = _fetch_ican_credit_details(order)
        stored_payload.pop("source", None)

        next_credit_id = str(payload_to_store.get("id") or order.get("ican_credit_id") or "").strip()
        if stored_payload == payload_to_store and next_credit_id == str(order.get("ican_credit_id") or "").strip():
            return

        update_order(
            order_id,
            ican_credit_id=next_credit_id or order.get("ican_credit_id"),
            ican_credit_payload=payload_to_store,
        )
    except Exception:
        logger.exception("Background ICAN credit sync failed for order_id=%s", order_id)


def _queue_order_ican_credit_sync(order: dict[str, Any]) -> None:
    order_id = int(order.get("id") or 0)
    if order_id <= 0:
        return

    if not (order.get("ican_credit_id") or order.get("ican_credit_payload")):
        return

    now = perf_counter()
    with ICAN_ORDER_SYNC_LOCK:
        last_attempt = ICAN_ORDER_SYNC_ATTEMPTS.get(order_id, 0.0)
        if now - last_attempt < ICAN_ORDER_SYNC_INTERVAL_SECONDS:
            return
        ICAN_ORDER_SYNC_ATTEMPTS[order_id] = now

    Thread(
        target=_refresh_order_ican_credit_details,
        args=(order_id,),
        daemon=True,
    ).start()


def _build_return_url(base_url: str | None, order_id: int) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return ""

    separator = "&" if "?" in normalized else "?"
    return f"{normalized}{separator}order_id={order_id}&payment=click"


def _append_query_params(base_url: str | None, params: dict[str, Any]) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return ""

    filtered = {
        key: value
        for key, value in params.items()
        if value is not None and str(value).strip() != ""
    }
    if not filtered:
        return normalized

    separator = "&" if "?" in normalized else "?"
    return f"{normalized}{separator}{urlencode(filtered)}"


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


def _build_monthly_click_transaction_param(payment_id: int) -> int:
    return MONTHLY_PAYMENT_CLICK_OFFSET + int(payment_id)


def _parse_monthly_payment_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw.isdigit():
        return None

    numeric = int(raw)
    if numeric < MONTHLY_PAYMENT_CLICK_OFFSET:
        return None

    payment_id = numeric - MONTHLY_PAYMENT_CLICK_OFFSET
    return payment_id if payment_id > 0 else None


def _build_monthly_return_url(request: Request, explicit_url: str | None, payment_id: int) -> str:
    if explicit_url:
        return _append_query_params(explicit_url, {"monthly_payment_id": payment_id})

    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        referer = str(request.headers.get("referer") or "").strip()
        if referer.startswith("http://") or referer.startswith("https://"):
            origin = referer.split("://", 1)[0] + "://" + referer.split("://", 1)[1].split("/", 1)[0]

    return _append_query_params(f"{origin}/profile/orders", {"monthly_payment_id": payment_id}) if origin else ""


def _build_monthly_click_payment_url(payment_id: int, amount: float, return_url: str) -> str:
    params = {
        "service_id": CLICK_SERVICE_ID,
        "merchant_id": CLICK_MERCHANT_ID,
        "amount": _build_click_amount(amount),
        "transaction_param": _build_monthly_click_transaction_param(payment_id),
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


def _parse_positive_int(value: Any) -> int:
    try:
        numeric = int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return 0

    return numeric if numeric > 0 else 0


def _parse_amount(value: Any) -> float:
    try:
        numeric = float(str(value or "").strip())
    except (TypeError, ValueError):
        return 0

    return numeric if numeric > 0 else 0


def _normalize_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_myid_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    if "." in raw:
        parts = raw.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            day, month, year = (int(part) for part in parts)
            if year > 1900 and 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"

    return _normalize_birth_date(raw)


def _normalize_ican_gender(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "male", "m", "erkak"}:
        return "male"
    if normalized in {"2", "female", "f", "ayol"}:
        return "female"
    return normalized if normalized in {"male", "female"} else ""


def _normalize_ican_person_name(value: Any) -> str:
    raw = (
        str(value or "")
        .strip()
        .replace("`", "'")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("ʼ", "'")
        .replace("ʻ", "'")
    )
    transliterated = "".join(
        CYRILLIC_TO_LATIN.get(char.upper(), char.upper()) for char in raw
    )
    cleaned = re.sub(r"[^A-Z'.\s-]", " ", transliterated)
    words = [word.strip("-'.") for word in cleaned.split() if word.strip("-'.")]
    normalized_words = [word[:15] for word in words[:2] if word[:15]]
    return " ".join(normalized_words)


def _build_credit_products_note(products: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        name = str(product.get("name") or "").strip()
        quantity = _parse_positive_int(product.get("quantity")) or 1
        if name:
            parts.append(f"{name} x{quantity}")

    return ", ".join(parts)


def _resolve_credit_amount(products: list[dict[str, Any]], fallback_total: Any = 0) -> float:
    total = 0.0
    for product in products:
        if not isinstance(product, dict):
            continue
        quantity = _parse_positive_int(product.get("quantity")) or 1
        total += _parse_amount(product.get("price")) * quantity

    if total > 0:
        return total

    return _parse_amount(fallback_total)


def _resolve_initial_payment_amount(products: list[dict[str, Any]]) -> float:
    total = 0.0
    for product in products:
        if not isinstance(product, dict) or not _normalize_flag(product.get("initial_payment_enabled")):
            continue
        quantity = _parse_positive_int(product.get("quantity")) or 1
        total += _parse_amount(product.get("initial_payment_amount")) * quantity
    return total


def _sync_product_initial_payments_from_db(
    products: list[dict[str, Any]],
    db: Session,
) -> list[dict[str, Any]]:
    product_ids = [
        int(product_id)
        for product in products
        if isinstance(product, dict)
        and (product_id := _parse_positive_int(product.get("id"))) > 0
    ]
    if not product_ids:
        return products

    products_by_id = {
        int(product.id): product
        for product in db.query(models.Product)
        .filter(models.Product.id.in_(sorted(set(product_ids))))
        .all()
    }

    normalized_products: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            normalized_products.append(product)
            continue

        normalized_product = dict(product)
        current_product = products_by_id.get(_parse_positive_int(product.get("id")))
        if current_product:
            initial_payment_enabled = bool(
                current_product.credit_enabled and current_product.initial_payment_enabled
            )
            normalized_product["initial_payment_enabled"] = initial_payment_enabled
            normalized_product["initial_payment_amount"] = (
                int(current_product.initial_payment_amount or 0)
                if initial_payment_enabled
                else 0
            )

        normalized_products.append(normalized_product)

    return normalized_products


def _resolve_order_credit_plan(products: list[dict[str, Any]]) -> dict[str, int]:
    selected: list[tuple[int, int]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        credit_plan = product.get("credit_plan")
        if not isinstance(credit_plan, dict):
            continue
        tariff_id = _parse_positive_int(credit_plan.get("tariff_id") or credit_plan.get("id"))
        months = _parse_positive_int(credit_plan.get("months"))
        if tariff_id > 0 and months > 0:
            selected.append((tariff_id, months))

    if not selected:
        raise HTTPException(status_code=400, detail="Nasiyaga tarif tanlanmagan.")

    first = selected[0]
    if any(item != first for item in selected[1:]):
        raise HTTPException(
            status_code=400,
            detail="Savatchadagi mahsulotlar uchun bir xil nasiyaga muddat tanlang.",
        )

    return {
        "tariff_id": first[0],
        "months": first[1],
    }


def _resolve_order_for_myid(order_id: int | None, session_id: str) -> dict[str, Any] | None:
    if order_id:
        return get_order(order_id)
    if session_id:
        return get_order_by_myid_session_id(session_id)
    return None


def _build_existing_myid_finalize_response(
    order: dict[str, Any],
    request: Request,
    *,
    session_id: str = "",
) -> dict[str, Any]:
    payment_method = str(order.get("payment_method") or "myid").strip().lower() or "myid"
    status = str(order.get("status") or "pending").strip().lower() or "pending"
    result_code = order.get("myid_result_code")
    try:
        normalized_result_code = int(result_code)
    except (TypeError, ValueError):
        normalized_result_code = 1 if status != "cancelled" else 0

    response: dict[str, Any] = {
        "success": status != "cancelled",
        "status": status,
        "payment_method": payment_method,
        "order_id": int(order["id"]),
        "session_id": session_id or str(order.get("myid_session_id") or "").strip(),
        "job_id": str(order.get("myid_job_id") or "").strip(),
        "result_code": normalized_result_code,
        "result_note": str(
            order.get("myid_result_note")
            or order.get("click_error_note")
            or ""
        ).strip(),
        "profile": order.get("myid_profile"),
    }

    if (
        payment_method == "click"
        and status in {"pending", "prepared"}
        and float(order.get("total") or 0) > 0
        and _click_is_configured()
    ):
        return_url = _resolve_return_url(request, None, int(order["id"]))
        response["initial_payment"] = float(order.get("total") or 0)
        response["payment_url"] = _build_click_payment_url(
            int(order["id"]),
            float(order.get("total") or 0),
            return_url,
        )
        response["return_url"] = return_url

    return response


def _resolve_myid_profile_sections(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile_root = profile.get("profile") if isinstance(profile.get("profile"), dict) else {}
    common_data = profile_root.get("common_data") if isinstance(profile_root.get("common_data"), dict) else {}
    doc_data = profile_root.get("doc_data") if isinstance(profile_root.get("doc_data"), dict) else {}
    contacts = profile_root.get("contacts") if isinstance(profile_root.get("contacts"), dict) else {}
    address = profile_root.get("address") if isinstance(profile_root.get("address"), dict) else {}
    permanent_registration = (
        address.get("permanent_registration")
        if isinstance(address.get("permanent_registration"), dict)
        else {}
    )
    return common_data, doc_data, contacts, address, permanent_registration


def _resolve_myid_ican_location(profile: dict[str, Any]) -> dict[str, Any]:
    _, _, _, address, permanent_registration = _resolve_myid_profile_sections(profile)
    try:
        return resolve_ican_location(
            myid_region_id=permanent_registration.get("region_id"),
            myid_region_name=(
                permanent_registration.get("region")
                or permanent_registration.get("region_name")
                or address.get("region")
            ),
            myid_district_id=permanent_registration.get("district_id"),
            myid_district_name=(
                permanent_registration.get("district")
                or permanent_registration.get("district_name")
                or address.get("district")
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _extract_myid_residential_address(profile: dict[str, Any]) -> str:
    _, _, _, address, permanent_registration = _resolve_myid_profile_sections(profile)
    raw_address = (
        permanent_registration.get("address")
        or address.get("permanent_address")
        or address.get("address")
        or address.get("current_address")
        or ""
    )
    district = (
        permanent_registration.get("district")
        or permanent_registration.get("district_name")
        or address.get("district")
        or address.get("district_name")
        or ""
    )
    region = (
        permanent_registration.get("region")
        or permanent_registration.get("region_name")
        or address.get("region")
        or address.get("region_name")
        or ""
    )
    parts = [str(raw_address).strip(), str(district).strip(), str(region).strip()]
    unique_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized = part.strip()
        if not normalized:
            continue
        dedupe_key = normalized.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique_parts.append(normalized)
    return ", ".join(unique_parts)


def _sync_customer_address_from_myid_profile(
    order: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> None:
    if not isinstance(order, dict) or not isinstance(profile, dict):
        return

    phone = _normalize_phone(order.get("phone"))
    if len(phone) != 12:
        return

    # Always refresh customer address from the latest MyID profile payload.
    address = _extract_myid_residential_address(profile)
    if not address:
        return
    update_customer_address_by_phone(phone, address)


def _build_credit_phone_list(
    order_phone: Any,
    profile: dict[str, Any],
    extra_phones: list[str] | None = None,
) -> list[str]:
    _, _, contacts, _, _ = _resolve_myid_profile_sections(profile)
    values = [
        _normalize_phone(order_phone),
        *[_normalize_phone(phone) for phone in (extra_phones or [])],
        _normalize_phone(contacts.get("phone")),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if len(value) != 12 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result[:3]


def _submit_ican_credit_for_order(
    order: dict[str, Any],
    extra_phones: list[str] | None = None,
) -> dict[str, Any]:
    if not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ICAN credit integration credentials are not configured",
        )

    products = order.get("products") if isinstance(order.get("products"), list) else []
    profile = order.get("myid_profile") if isinstance(order.get("myid_profile"), dict) else {}
    if not profile:
        raise HTTPException(status_code=400, detail="MyID profile topilmadi.")

    selected_plan = _resolve_order_credit_plan(products)
    credit_amount = _resolve_credit_amount(products, order.get("total"))
    if credit_amount <= 0:
        raise HTTPException(status_code=400, detail="Kredit summasi aniqlanmadi.")

    initial_payment = _resolve_initial_payment_amount(products)
    common_data, doc_data, _, address, permanent_registration = _resolve_myid_profile_sections(profile)
    location = _resolve_myid_ican_location(profile)
    region_id = int(location["ican_region_id"])
    district_id = int(location["ican_district_id"])

    phones = _build_credit_phone_list(order.get("phone"), profile, extra_phones=extra_phones)
    if not phones:
        raise HTTPException(status_code=400, detail="Telefon raqami topilmadi.")

    passport = _normalize_pass_data(doc_data.get("pass_data"))
    pinfl = "".join(ch for ch in str(common_data.get("pinfl") or "") if ch.isdigit())
    if not passport or len(pinfl) != 14:
        raise HTTPException(status_code=400, detail="Passport yoki PINFL topilmadi.")

    passport_address = str(
        address.get("permanent_address")
        or permanent_registration.get("address")
        or address.get("address")
        or address.get("current_address")
        or ""
    ).strip()
    if not passport_address:
        raise HTTPException(status_code=400, detail="Passportdagi manzil topilmadi.")

    _sync_customer_address_from_myid_profile(order, profile)

    user_document_id = _resolve_existing_ican_user_document_id(pinfl=pinfl)
    if not user_document_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "ICAN foydalanuvchi hujjati topilmadi. "
                "Bu mijoz uchun avval ICAN tizimida hujjat yaratilgan bo'lishi kerak."
            ),
        )

    payload: dict[str, Any] = {
        "company_id": ICAN_CREDIT_COMPANY_ID,
        "employee_id": ICAN_CREDIT_EMPLOYEE_ID,
        "tariff_id": selected_plan["tariff_id"],
        "amount": credit_amount,
        "initial_payment": initial_payment,
        "period": selected_plan["months"],
        "payment_day": 1,
        "products_note": _build_credit_products_note(products),
        "comment": f"Website checkout order #{order['id']}",
        "person_main": {
            "user_document_id": int(user_document_id),
            "district_id": district_id,
            "passport_address": passport_address,
            "phones": phones,
        },
    }

    if region_id > 0:
        payload["person_main"]["region_id"] = region_id

    try:
        response = requests.post(
            f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_CREATE_PATH}",
            json=payload,
            auth=(ICAN_CREDIT_USERNAME, ICAN_CREDIT_PASSWORD),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ICAN credit service is unavailable: {exc}",
        ) from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        status_code = 400 if response.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="ICAN credit service returned an invalid response",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="ICAN credit service returned an invalid response",
        )

    return payload


def _extract_ican_credit_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("credit_id", "creditId", "credit___id", "id"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()

        for key in ("data", "result", "credit"):
            nested_value = _extract_ican_credit_id(payload.get(key))
            if nested_value:
                return nested_value

    if isinstance(payload, list):
        for item in payload:
            nested_value = _extract_ican_credit_id(item)
            if nested_value:
                return nested_value

    return ""


def _extract_ican_user_document_id(payload: Any, *, pinfl: str = "") -> str:
    normalized_pinfl = "".join(ch for ch in str(pinfl or "") if ch.isdigit())

    if isinstance(payload, dict):
        person_main = payload.get("person_main")
        if isinstance(person_main, dict):
            user_document_id = person_main.get("user_document_id")
            if user_document_id is not None and str(user_document_id).strip():
                if not normalized_pinfl:
                    return str(user_document_id).strip()

                user_document = person_main.get("user_document")
                if isinstance(user_document, dict):
                    data = user_document.get("data")
                    if isinstance(data, dict):
                        payload_pinfl = "".join(
                            ch for ch in str(data.get("pinfl") or "") if ch.isdigit()
                        )
                        if payload_pinfl == normalized_pinfl:
                            return str(user_document_id).strip()

        user_document = payload.get("user_document")
        if isinstance(user_document, dict):
            user_document_id = user_document.get("id")
            if user_document_id is not None and str(user_document_id).strip():
                if not normalized_pinfl:
                    return str(user_document_id).strip()

                data = user_document.get("data")
                if isinstance(data, dict):
                    payload_pinfl = "".join(
                        ch for ch in str(data.get("pinfl") or "") if ch.isdigit()
                    )
                    if payload_pinfl == normalized_pinfl:
                        return str(user_document_id).strip()

        for value in payload.values():
            nested_value = _extract_ican_user_document_id(value, pinfl=normalized_pinfl)
            if nested_value:
                return nested_value

    if isinstance(payload, list):
        for item in payload:
            nested_value = _extract_ican_user_document_id(item, pinfl=normalized_pinfl)
            if nested_value:
                return nested_value

    return ""


def _resolve_existing_ican_user_document_id(*, pinfl: str) -> str:
    normalized_pinfl = "".join(ch for ch in str(pinfl or "") if ch.isdigit())
    if len(normalized_pinfl) != 14:
        return ""

    if not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        return ""

    try:
        response = requests.get(
            f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_LIST_PATH}",
            params={"filter[pinfl]": normalized_pinfl},
            auth=(ICAN_CREDIT_USERNAME, ICAN_CREDIT_PASSWORD),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru",
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""

    try:
        payload = response.json()
    except ValueError:
        return ""

    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ""

    for item in items:
        user_document_id = _extract_ican_user_document_id(item, pinfl=normalized_pinfl)
        if user_document_id:
            return user_document_id

    return ""


def _resolve_order_monthly_payment_amount(order: dict[str, Any]) -> float:
    products = order.get("products") if isinstance(order.get("products"), list) else []
    total = 0.0
    for product in products:
        if not isinstance(product, dict):
            continue
        credit_plan = product.get("credit_plan") if isinstance(product.get("credit_plan"), dict) else {}
        quantity = _parse_positive_int(product.get("quantity")) or 1
        total += _parse_amount(
            credit_plan.get("monthly_payment") or credit_plan.get("monthlyPayment")
        ) * quantity

    return total


def _submit_ican_credit_transaction(*, credit_id: str, amount: float) -> dict[str, Any]:
    if not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ICAN credit integration credentials are not configured",
        )

    normalized_credit_id = str(credit_id or "").strip()
    if not normalized_credit_id:
        raise HTTPException(status_code=400, detail="Kredit ID topilmadi.")

    if float(amount or 0) <= 0:
        raise HTTPException(status_code=400, detail="To'lov summasi noto'g'ri.")

    try:
        response = requests.post(
            f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_TRANSACTION_CREATE_PATH}",
            json={
                "payment_type": "card_click",
                "amount": float(amount),
                "credit_id": normalized_credit_id,
            },
            auth=(ICAN_CREDIT_USERNAME, ICAN_CREDIT_PASSWORD),
            headers={
                "Accept": "application/json",
                "Accept-Language": "ru",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ICAN credit transaction service is unavailable: {exc}",
        ) from exc

    if response.status_code >= 400:
        detail = _extract_upstream_error_detail(response)
        status_code = 400 if response.status_code < 500 else 502
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="ICAN credit transaction service returned an invalid response",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="ICAN credit transaction service returned an invalid response",
        )

    return payload


def _order_requires_initial_payment(order: dict[str, Any]) -> bool:
    products = order.get("products") if isinstance(order.get("products"), list) else []
    return _resolve_initial_payment_amount(products) > 0


def _get_request_origin(request: Request) -> str:
    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    if origin:
        return origin

    referer = str(request.headers.get("referer") or "").strip()
    if referer.startswith("http://") or referer.startswith("https://"):
        return referer.split("://", 1)[0] + "://" + referer.split("://", 1)[1].split("/", 1)[0]

    return ""


def _resolve_myid_redirect_uri(request: Request, explicit_url: str | None) -> str:
    configured = MYID_REDIRECT_URL or str(explicit_url or "").strip()
    if configured:
        return _append_query_params(configured, {"myid_popup": "1"})

    origin = _get_request_origin(request)
    return _append_query_params(f"{origin}/checkout", {"myid_popup": "1"}) if origin else ""


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


def _myid_http_attempt_count() -> int:
    return max(1, min(int(MYID_HTTP_RETRY_COUNT or 1), 5))


def _mask_debug_value(value: Any) -> Any:
    if value is None:
        return None

    text = str(value)
    if len(text) <= 4:
        return "***"

    return f"{text[:2]}***{text[-2:]}"


def _sanitize_myid_debug_payload(value: Any) -> Any:
    sensitive_keys = {
        "authorization",
        "client_secret",
        "access_token",
        "refresh_token",
        "code",
        "auth_code",
        "pinfl",
        "pass_data",
        "birth_date",
    }

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in sensitive_keys:
                sanitized[key] = _mask_debug_value(item)
            elif isinstance(item, (dict, list)):
                sanitized[key] = _sanitize_myid_debug_payload(item)
            else:
                sanitized[key] = item
        return sanitized

    if isinstance(value, list):
        return [_sanitize_myid_debug_payload(item) for item in value]

    return value


def _myid_debug_context(kwargs: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("data", "json", "headers", "params"):
        if key in kwargs:
            context[key] = _sanitize_myid_debug_payload(kwargs.get(key))
    return context


def _request_myid(method: str, path: str, *, failure_message: str, **kwargs: Any) -> requests.Response:
    url = f"{MYID_BASE_URL}{path}"
    attempts = _myid_http_attempt_count()
    debug_context = _myid_debug_context(kwargs) if MYID_DEBUG_LOGS else {}

    for attempt in range(1, attempts + 1):
        started_at = perf_counter()
        if MYID_DEBUG_LOGS:
            logger.info(
                "MyID request start method=%s path=%s attempt=%s/%s timeout=%s payload=%s",
                method,
                path,
                attempt,
                attempts,
                MYID_REQUEST_TIMEOUT,
                debug_context,
            )

        try:
            response = requests.request(
                method,
                url,
                timeout=MYID_REQUEST_TIMEOUT,
                **kwargs,
            )
            if MYID_DEBUG_LOGS:
                logger.info(
                    "MyID request done method=%s path=%s attempt=%s/%s status=%s elapsed_ms=%s",
                    method,
                    path,
                    attempt,
                    attempts,
                    response.status_code,
                    int((perf_counter() - started_at) * 1000),
                )
            return response
        except requests.Timeout as exc:
            if MYID_DEBUG_LOGS:
                logger.warning(
                    "MyID request timeout method=%s path=%s attempt=%s/%s elapsed_ms=%s",
                    method,
                    path,
                    attempt,
                    attempts,
                    int((perf_counter() - started_at) * 1000),
                )
            if attempt >= attempts:
                raise HTTPException(
                    status_code=504,
                    detail="MyID serveriga ulanish vaqti tugadi. Birozdan keyin qayta urinib ko'ring.",
                ) from exc
        except requests.ConnectionError as exc:
            if MYID_DEBUG_LOGS:
                logger.warning(
                    "MyID request connection error method=%s path=%s attempt=%s/%s elapsed_ms=%s error=%s",
                    method,
                    path,
                    attempt,
                    attempts,
                    int((perf_counter() - started_at) * 1000),
                    exc,
                )
            if attempt >= attempts:
                raise HTTPException(
                    status_code=502,
                    detail="MyID serveriga ulanishda xatolik bo'ldi. Birozdan keyin qayta urinib ko'ring.",
                ) from exc
        except requests.RequestException as exc:
            if MYID_DEBUG_LOGS:
                logger.warning(
                    "MyID request failed method=%s path=%s attempt=%s/%s elapsed_ms=%s error=%s",
                    method,
                    path,
                    attempt,
                    attempts,
                    int((perf_counter() - started_at) * 1000),
                    exc,
                )
            raise HTTPException(status_code=502, detail=f"{failure_message}: {exc}") from exc

        sleep(min(0.5 * attempt, 2))

    raise HTTPException(status_code=502, detail=failure_message)


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

    response = _request_myid(
        "POST",
        "/api/v1/oauth2/access-token",
        failure_message="MyID token request failed",
        data=form_data,
        headers={"Accept": "application/json"},
    )

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

    response = _request_myid(
        "POST",
        "/api/v1/web/sessions",
        failure_message="MyID session creation failed",
        json=payload,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
    )

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
    response = _request_myid(
        "POST",
        f"/api/v1/web/sessions/{session_id}/result",
        failure_message="MyID session result request failed",
        headers=_myid_client_bearer_headers(),
    )

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
    response = _request_myid(
        "POST",
        f"/api/v1/web/sessions/{session_id}/client/close",
        failure_message="MyID session close request failed",
        json={"code": close_code},
        headers=_myid_client_bearer_headers(),
    )

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


def _build_public_order_payload(order: dict[str, Any]) -> dict[str, Any]:
    payment_method = str(order.get("payment_method") or "cash").strip().lower() or "cash"
    status_note = ""
    monthly_payments = get_monthly_payments_by_order_id(int(order["id"]))
    ican_credit = _fetch_ican_credit_details(order)

    if payment_method == "click":
        status_note = order.get("click_error_note") or ""
    elif payment_method == "myid":
        status_note = order.get("myid_result_note") or ""

    customer = get_customer_by_phone(_normalize_phone(order.get("phone")))
    customer_address = str((customer or {}).get("address") or "").strip()

    return {
        "id": order["id"],
        "status": order["status"],
        "payment_method": payment_method,
        "customer_address": customer_address,
        "total": order.get("total") or 0,
        "products": order.get("products") or [],
        "monthly_payment_amount": _resolve_order_monthly_payment_amount(order),
        "monthly_payments": [
            {
                "id": payment.get("id"),
                "amount": payment.get("amount") or 0,
                "status": payment.get("status") or "pending",
                "created_at": payment.get("created_at"),
                "updated_at": payment.get("updated_at"),
                "ican_error_note": payment.get("ican_error_note") or "",
            }
            for payment in monthly_payments
        ],
        "can_pay_monthly": bool(order.get("ican_credit_id")),
        "credit_submitted": bool(order.get("ican_credit_id") or order.get("ican_credit_payload")),
        "ican_credit": ican_credit,
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "click_error": order.get("click_error"),
        "click_error_note": order.get("click_error_note") or "",
        "myid_result_code": order.get("myid_result_code"),
        "myid_result_note": order.get("myid_result_note") or "",
        "status_note": status_note,
    }


def _is_public_order_visible(order: dict[str, Any]) -> bool:
    payment_method = str(order.get("payment_method") or "").strip().lower()
    status = str(order.get("status") or "").strip().lower()
    credit_submitted = bool(order.get("ican_credit_id") or order.get("ican_credit_payload"))

    if credit_submitted:
        return True

    if payment_method == "click":
        return status == "completed" and not order.get("myid_profile")

    if payment_method == "myid":
        return False

    return status == "completed"


def _build_public_monthly_payment_payload(payment: dict[str, Any]) -> dict[str, Any]:
    status = str(payment.get("status") or "pending").strip().lower() or "pending"
    click_paid = status == "completed"
    ican_response = payment.get("ican_response") if isinstance(payment.get("ican_response"), dict) else None
    ican_error_note = str(payment.get("ican_error_note") or "").strip()
    ican_sent = click_paid and bool(ican_response) and not ican_error_note

    return {
        "id": payment.get("id"),
        "order_id": payment.get("order_id"),
        "amount": payment.get("amount") or 0,
        "status": status,
        "click_paid": click_paid,
        "click_trans_id": payment.get("click_trans_id") or "",
        "click_paydoc_id": payment.get("click_paydoc_id") or "",
        "click_error": payment.get("click_error"),
        "click_error_note": payment.get("click_error_note") or "",
        "ican_sent": ican_sent,
        "ican_error_note": ican_error_note,
        "needs_ican_retry": click_paid and not ican_sent,
        "created_at": payment.get("created_at"),
        "updated_at": payment.get("updated_at"),
    }


@router.get("/orders")
def list_public_orders(phone: str) -> dict[str, Any]:
    normalized_phone = _normalize_phone(phone)
    if len(normalized_phone) != 12:
        raise HTTPException(status_code=400, detail="Valid phone number is required")

    orders: list[dict[str, Any]] = []
    for order in get_orders_by_phone(normalized_phone):
        if not _is_public_order_visible(order):
            continue
        _queue_order_ican_credit_sync(order)
        orders.append(_build_public_order_payload(order))

    return {
        "success": True,
        "orders": orders,
        "total": len(orders),
    }


@router.get("/monthly-payments")
def list_public_monthly_payments(
    phone: str | None = None,
    customer_id: int | None = None,
) -> dict[str, Any]:
    resolved_phone = _normalize_phone(phone or "")

    if customer_id is not None:
        customer = get_customer_by_id(customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        customer_phone = _normalize_phone(customer.get("phone"))
        if resolved_phone and resolved_phone != customer_phone:
            raise HTTPException(status_code=403, detail="Phone number does not match customer")
        resolved_phone = customer_phone

    if len(resolved_phone) != 12:
        raise HTTPException(status_code=400, detail="Valid phone number or customer_id is required")

    payments = [
        _build_public_monthly_payment_payload(payment)
        for payment in get_monthly_payments_by_phone(resolved_phone)
    ]

    return {
        "success": True,
        "phone": resolved_phone,
        "customer_id": customer_id,
        "transactions": payments,
        "total": len(payments),
    }


@router.get("/orders/{order_id}")
def get_public_order_status(order_id: int) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    _queue_order_ican_credit_sync(order)
    return _build_public_order_payload(order)


@router.delete("/orders/{order_id}")
def delete_payment_order(
    order_id: int,
    token: dict = Depends(verify_admin_token),
) -> dict[str, Any]:
    deleted_order = delete_order_by_id(order_id)
    if not deleted_order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "success": True,
        "order_id": order_id,
        "detail": "Order deleted successfully",
    }


@router.post("/orders/{order_id}/monthly-payment/initiate")
def initiate_monthly_payment(
    order_id: int,
    payload: MonthlyPaymentInitiatePayload,
    request: Request,
) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not _click_is_configured():
        raise HTTPException(status_code=500, detail="CLICK integration is not configured")

    credit_id = str(order.get("ican_credit_id") or "").strip()
    if not credit_id:
        raise HTTPException(status_code=400, detail="Bu buyurtma uchun kredit ID topilmadi.")

    request_phone = _normalize_phone(payload.phone or "")
    order_phone = _normalize_phone(order.get("phone"))
    if request_phone and request_phone != order_phone:
        raise HTTPException(status_code=403, detail="Bu buyurtma sizga tegishli emas.")

    amount = _resolve_order_monthly_payment_amount(order)
    requested_amount = float(payload.amount or 0)
    if requested_amount > 0 and not _amounts_match(amount, requested_amount):
        raise HTTPException(status_code=400, detail="Oylik to'lov summasi mos kelmadi.")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Oylik to'lov summasi aniqlanmadi.")

    payment = create_monthly_payment(
        order_id=int(order["id"]),
        credit_id=credit_id,
        phone=order_phone,
        amount=amount,
        status="pending",
    )
    merchant_prepare_id = _build_monthly_click_transaction_param(int(payment["id"]))
    payment = update_monthly_payment(
        int(payment["id"]),
        merchant_prepare_id=merchant_prepare_id,
        click_error=0,
        click_error_note="",
    ) or payment

    return_url = _build_monthly_return_url(request, payload.return_url, int(payment["id"]))
    payment_url = _build_monthly_click_payment_url(int(payment["id"]), amount, return_url)

    return {
        "success": True,
        "payment_id": int(payment["id"]),
        "order_id": int(order["id"]),
        "amount": amount,
        "payment_url": payment_url,
        "return_url": return_url,
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
def initiate_myid_payment(
    payload: MyIdCheckoutPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    name = str(payload.name or "").strip()
    phone = _normalize_phone(payload.phone)
    products = _sync_product_initial_payments_from_db(payload.products or [], db)
    pinfl = "".join(ch for ch in str(payload.pinfl or "") if ch.isdigit())
    pass_data = _normalize_pass_data(payload.pass_data)
    birth_date = _normalize_birth_date(payload.birth_date)
    redirect_uri = _resolve_myid_redirect_uri(request, payload.redirect_uri)

    if MYID_DEBUG_LOGS:
        logger.info(
            "MyID initiate received origin=%s redirect_uri=%s explicit_redirect_uri=%s products_count=%s has_pinfl=%s has_pass_data=%s",
            _get_request_origin(request),
            redirect_uri,
            str(payload.redirect_uri or "").strip(),
            len(products),
            bool(pinfl),
            bool(pass_data),
        )

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
    initial_payment_amount = _resolve_initial_payment_amount(products)
    order_total = initial_payment_amount if initial_payment_amount > 0 else float(payload.total or 0)
    order = create_order(
        name=name,
        phone=phone,
        products=products,
        total=order_total,
        locale=(payload.locale or "uz").strip() or "uz",
        status="pending",
        payment_method="myid",
        myid_session_id=session_id,
        myid_external_id=external_id,
    )
    redirect_uri_with_order = _append_query_params(
        redirect_uri,
        {"order_id": int(order["id"])},
    )
    redirect_url = _build_myid_redirect_url(
        session_id=session_id,
        pinfl=pinfl,
        pass_data=pass_data,
        birth_date=birth_date,
        redirect_uri=redirect_uri_with_order,
        is_resident=payload.is_resident,
        lang=payload.lang or payload.locale,
    )

    if MYID_DEBUG_LOGS:
        logger.info(
            "MyID initiate redirect built order_id=%s session_id=%s external_id=%s redirect_uri=%s web_base_url=%s",
            int(order["id"]),
            _mask_debug_value(session_id),
            _mask_debug_value(external_id),
            redirect_uri_with_order,
            MYID_WEB_BASE_URL,
        )

    return {
        "success": True,
        "order_id": int(order["id"]),
        "session_id": session_id,
        "external_id": external_id,
        "redirect_uri": redirect_uri_with_order,
        "redirect_url": redirect_url,
    }


@router.post("/myid/finalize")
def finalize_myid_payment(
    payload: MyIdFinalizePayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    session_id = str(payload.session_id or "").strip()
    order = _resolve_order_for_myid(payload.order_id, session_id)
    if payload.order_id and not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order:
        synced_products = _sync_product_initial_payments_from_db(order.get("products") or [], db)
        update_fields: dict[str, Any] = {}
        if synced_products != (order.get("products") or []):
            update_fields["products"] = synced_products

        initial_payment_amount = _resolve_initial_payment_amount(synced_products)
        if (
            initial_payment_amount <= 0
            and str(order.get("payment_method") or "").strip().lower() == "click"
            and str(order.get("status") or "").strip().lower() in {"pending", "prepared"}
            and str(order.get("myid_result_code") or "").strip() == "1"
        ):
            note = "MyID tasdiqlandi. Endi xaridni yakunlang."
            update_fields.update(
                total=_resolve_credit_amount(synced_products, order.get("total")),
                status="pending",
                payment_method="myid",
                click_error_note=note,
                myid_result_note=note,
            )

        if update_fields:
            order = update_order(int(order["id"]), **update_fields) or order

    if order and order.get("myid_profile") and str(order.get("myid_result_code") or "").strip() == "1":
        _sync_customer_address_from_myid_profile(
            order,
            order.get("myid_profile") if isinstance(order.get("myid_profile"), dict) else {},
        )
        return _build_existing_myid_finalize_response(order, request, session_id=session_id)

    if payload.reason_code is not None and not str(payload.auth_code or "").strip():
        reason_note = _build_myid_reason_note(int(payload.reason_code))
        if order:
            order = update_order(
                int(order["id"]),
                status="cancelled",
                payment_method="myid",
                myid_session_id=session_id or order.get("myid_session_id"),
                myid_result_code=int(payload.reason_code),
                myid_result_note=reason_note,
            ) or order
        return {
            "success": False,
            "status": "cancelled",
            "payment_method": "myid",
            "order_id": int(order["id"]) if order else payload.order_id,
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

    try:
        token_payload = _myid_request_access_token(
            grant_type="authorization_code",
            code=auth_code,
        )
    except HTTPException as exc:
        latest_order = _resolve_order_for_myid(payload.order_id, session_id)
        if latest_order and latest_order.get("myid_profile") and str(
            latest_order.get("myid_result_code") or ""
        ).strip() == "1":
            return _build_existing_myid_finalize_response(
                latest_order,
                request,
                session_id=session_id,
            )
        raise exc
    access_token = str(token_payload.get("access_token") or "").strip()
    profile = _myid_fetch_user_profile(access_token)
    _sync_customer_address_from_myid_profile(order, profile)
    job_id = str(_decode_jwt_payload(access_token).get("job_id") or "").strip()
    result_note = "MyID verification completed successfully"

    if not order:
        return {
            "success": True,
            "status": "completed",
            "payment_method": "myid",
            "session_id": session_id,
            "job_id": job_id,
            "result_code": 1,
            "result_note": result_note,
            "profile": profile,
        }

    try:
        _resolve_myid_ican_location(profile)
    except HTTPException as exc:
        update_order(
            int(order["id"]),
            status="cancelled",
            payment_method="myid",
            myid_session_id=session_id or order.get("myid_session_id"),
            myid_job_id=job_id,
            myid_result_code=0,
            myid_result_note=str(exc.detail),
            myid_profile=profile,
        )
        raise

    order = update_order(
        int(order["id"]),
        status="pending",
        payment_method="myid",
        myid_session_id=session_id or order.get("myid_session_id"),
        myid_job_id=job_id,
        myid_result_code=1,
        myid_result_note=result_note,
        myid_profile=profile,
    ) or order

    synced_products = order.get("products") or []
    initial_payment_amount = _resolve_initial_payment_amount(synced_products)
    if initial_payment_amount > 0:
        if not _click_is_configured():
            raise HTTPException(status_code=500, detail="CLICK integration is not configured")

        click_note = "MyID tasdiqlandi. Endi bosh to'lovni Click orqali to'lang."
        merchant_prepare_id = int(order["id"])
        return_url = _resolve_return_url(request, None, int(order["id"]))
        payment_url = _build_click_payment_url(
            int(order["id"]),
            float(initial_payment_amount),
            return_url,
        )
        order = update_order(
            int(order["id"]),
            total=float(initial_payment_amount),
            status="pending",
            payment_method="click",
            merchant_prepare_id=merchant_prepare_id,
            click_error=0,
            click_error_note=click_note,
            myid_result_note=click_note,
        ) or order
        return {
            "success": True,
            "status": "pending",
            "payment_method": "click",
            "order_id": int(order["id"]),
            "session_id": session_id,
            "job_id": job_id,
            "result_code": 1,
            "result_note": click_note,
            "initial_payment": float(initial_payment_amount),
            "profile": profile,
            "payment_url": payment_url,
            "return_url": return_url,
            "next_action": "initial_payment",
        }

    success_note = "MyID tasdiqlandi. Endi xaridni yakunlang."
    order = update_order(
        int(order["id"]),
        status="pending",
        payment_method="myid",
        myid_result_note=success_note,
    ) or order

    return {
        "success": True,
        "status": "pending",
        "payment_method": "myid",
        "order_id": int(order["id"]),
        "session_id": session_id,
        "job_id": job_id,
        "result_code": 1,
        "result_note": success_note,
        "profile": profile,
        "next_action": "submit_credit",
    }


@router.post("/orders/{order_id}/submit-credit")
def submit_order_credit_request(
    order_id: int,
    payload: SubmitCreditPayload | None = Body(default=None),
) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not order.get("myid_profile") or int(order.get("myid_result_code") or 0) != 1:
        raise HTTPException(status_code=400, detail="MyID tasdig'i topilmadi.")

    if _order_requires_initial_payment(order):
        payment_method = str(order.get("payment_method") or "").strip().lower()
        status = str(order.get("status") or "").strip().lower()
        if payment_method == "click" and status != "completed":
            raise HTTPException(status_code=400, detail="Avval boshlang'ich to'lovni yakunlang.")

    if (
        str(order.get("status") or "").strip().lower() == "completed"
        and (order.get("ican_credit_id") or order.get("ican_credit_payload"))
    ):
        return {
            "success": True,
            "status": "completed",
            "payment_method": str(order.get("payment_method") or "myid").strip().lower() or "myid",
            "order_id": int(order["id"]),
            "result_note": str(order.get("myid_result_note") or order.get("click_error_note") or "").strip(),
            "credit_submitted": True,
        }

    extra_phones = [
        phone
        for phone in {_normalize_phone(phone) for phone in ((payload.phones if payload else []) or [])}
        if len(phone) == 12
    ]
    if len(extra_phones) < 2:
        raise HTTPException(status_code=400, detail="2 ta qo'shimcha telefon raqami majburiy.")

    try:
        credit_payload = _submit_ican_credit_for_order(
            order,
            extra_phones=extra_phones,
        )
    except HTTPException as exc:
        update_order(
            int(order["id"]),
            myid_result_note=str(exc.detail),
        )
        raise

    success_note = "MyID tasdiqlandi va kredit arizasi yuborildi."
    ican_credit_id = _extract_ican_credit_id(credit_payload)
    updated_order = update_order(
        int(order["id"]),
        status="completed",
        payment_method="myid",
        myid_result_note=success_note,
        click_error_note=success_note,
        ican_credit_id=ican_credit_id or order.get("ican_credit_id"),
        ican_credit_payload=credit_payload,
    ) or order

    return {
        "success": True,
        "status": "completed",
        "payment_method": str(updated_order.get("payment_method") or "myid").strip().lower() or "myid",
        "order_id": int(updated_order["id"]),
        "result_note": success_note,
        "credit_submitted": True,
        "credit": credit_payload,
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


def _handle_monthly_prepare(data: dict[str, Any], payment_id: int) -> dict[str, Any]:
    payment = get_monthly_payment(payment_id)
    if not payment:
        return _prepare_error(data, CLICK_ERROR_ORDER_NOT_FOUND, "Monthly payment not found")

    if not _amounts_match(payment.get("amount") or 0, data.get("amount") or 0):
        return _prepare_error(
            data,
            CLICK_ERROR_AMOUNT,
            "Incorrect amount",
            merchant_prepare_id=payment.get("merchant_prepare_id"),
        )

    status = str(payment.get("status") or "").strip().lower()
    if status == "completed":
        return _prepare_error(
            data,
            CLICK_ERROR_ALREADY_PAID,
            "Monthly payment already paid",
            merchant_prepare_id=payment.get("merchant_prepare_id"),
        )
    if status == "cancelled":
        return _prepare_error(
            data,
            CLICK_ERROR_CANCELLED,
            "Monthly payment cancelled",
            merchant_prepare_id=payment.get("merchant_prepare_id"),
        )

    merchant_prepare_id = int(
        payment.get("merchant_prepare_id") or _build_monthly_click_transaction_param(payment_id)
    )
    updated = update_monthly_payment(
        int(payment["id"]),
        status="prepared",
        click_trans_id=str(data.get("click_trans_id") or ""),
        click_paydoc_id=str(data.get("click_paydoc_id") or ""),
        merchant_prepare_id=merchant_prepare_id,
        click_error=0,
        click_error_note="",
    ) or payment

    return _build_prepare_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(_build_monthly_click_transaction_param(int(updated["id"]))),
        merchant_prepare_id=merchant_prepare_id,
        error=0,
        error_note="Success",
    )


def _handle_monthly_complete(data: dict[str, Any], payment_id: int) -> dict[str, Any]:
    payment = get_monthly_payment(payment_id)
    if not payment:
        return _complete_error(data, CLICK_ERROR_ORDER_NOT_FOUND, "Monthly payment not found")

    merchant_prepare_id = _parse_order_id(data.get("merchant_prepare_id"))
    expected_prepare_id = int(
        payment.get("merchant_prepare_id") or _build_monthly_click_transaction_param(payment_id)
    )
    if merchant_prepare_id != expected_prepare_id:
        return _complete_error(data, CLICK_ERROR_TRANSACTION_NOT_FOUND, "Transaction not found")

    if not _amounts_match(payment.get("amount") or 0, data.get("amount") or 0):
        return _complete_error(
            data,
            CLICK_ERROR_AMOUNT,
            "Incorrect amount",
            merchant_confirm_id=payment.get("merchant_confirm_id"),
        )

    status = str(payment.get("status") or "").strip().lower()
    if status == "completed":
        return _complete_error(
            data,
            CLICK_ERROR_ALREADY_PAID,
            "Monthly payment already paid",
            merchant_confirm_id=payment.get("merchant_confirm_id"),
        )
    if status == "cancelled":
        return _complete_error(
            data,
            CLICK_ERROR_CANCELLED,
            "Monthly payment cancelled",
            merchant_confirm_id=payment.get("merchant_confirm_id"),
        )

    click_error = int(str(data.get("error") or "0").strip() or "0")
    error_note = str(data.get("error_note") or "").strip()
    if click_error <= -1:
        cancelled = update_monthly_payment(
            int(payment["id"]),
            status="cancelled",
            click_trans_id=str(data.get("click_trans_id") or ""),
            click_paydoc_id=str(data.get("click_paydoc_id") or ""),
            merchant_prepare_id=merchant_prepare_id,
            click_error=click_error,
            click_error_note=error_note or "Payment cancelled by CLICK",
        ) or payment
        return _complete_error(
            data,
            CLICK_ERROR_CANCELLED,
            error_note or "Payment cancelled by CLICK",
            merchant_confirm_id=cancelled.get("merchant_confirm_id"),
        )

    merchant_confirm_id = int(payment.get("merchant_confirm_id") or expected_prepare_id)
    completed = update_monthly_payment(
        int(payment["id"]),
        status="completed",
        click_trans_id=str(data.get("click_trans_id") or ""),
        click_paydoc_id=str(data.get("click_paydoc_id") or ""),
        merchant_prepare_id=merchant_prepare_id,
        merchant_confirm_id=merchant_confirm_id,
        click_error=0,
        click_error_note="Success",
    ) or payment

    try:
        ican_response = _submit_ican_credit_transaction(
            credit_id=str(completed.get("credit_id") or ""),
            amount=float(completed.get("amount") or 0),
        )
        completed = update_monthly_payment(
            int(completed["id"]),
            ican_response=ican_response,
            ican_error_note="",
        ) or completed
    except HTTPException as exc:
        update_monthly_payment(
            int(completed["id"]),
            ican_error_note=str(exc.detail),
        )
    except Exception:
        update_monthly_payment(
            int(completed["id"]),
            ican_error_note="ICAN monthly transaction request failed after CLICK payment.",
        )

    return _build_complete_response(
        click_trans_id=str(data.get("click_trans_id") or ""),
        merchant_trans_id=str(_build_monthly_click_transaction_param(int(completed["id"]))),
        merchant_confirm_id=merchant_confirm_id,
        error=0,
        error_note="Success",
    )


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

    monthly_payment_id = _parse_monthly_payment_id(data.get("merchant_trans_id"))
    if monthly_payment_id:
        return _handle_monthly_prepare(data, monthly_payment_id)

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

    monthly_payment_id = _parse_monthly_payment_id(data.get("merchant_trans_id"))
    if monthly_payment_id:
        return _handle_monthly_complete(data, monthly_payment_id)

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

    if completed.get("myid_profile") and not completed.get("ican_credit_id"):
        completed = update_order(
            int(completed["id"]),
            click_error_note="Boshlang'ich to'lov qabul qilindi. Endi qo'shimcha telefonlarni kiriting.",
            myid_result_note="Boshlang'ich to'lov qabul qilindi. Endi qo'shimcha telefonlarni kiriting.",
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
