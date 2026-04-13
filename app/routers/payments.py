import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
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
    ICAN_CREDIT_PASSWORD,
    ICAN_CREDIT_USERNAME,
    MYID_BASE_URL,
    MYID_CLIENT_ID,
    MYID_CLIENT_SECRET,
    MYID_MAX_RETRIES,
    MYID_METHOD,
    MYID_REDIRECT_URL,
    MYID_SCOPE,
    MYID_WEB_BASE_URL,
)
from ..database import get_db
from ..myid_ican_locations import resolve_ican_location
from ..orders_database import (
    create_order,
    get_order,
    get_order_by_prepare_id,
    get_order_by_myid_session_id,
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

    text = response.text.strip()
    return text[:500] if text else "Upstream request failed."


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


def _build_credit_phone_list(order_phone: Any, profile: dict[str, Any]) -> list[str]:
    _, _, contacts, _, _ = _resolve_myid_profile_sections(profile)
    values = [
        _normalize_phone(order_phone),
        _normalize_phone(contacts.get("phone")),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if len(value) != 12 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _submit_ican_credit_for_order(order: dict[str, Any]) -> dict[str, Any]:
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

    phones = _build_credit_phone_list(order.get("phone"), profile)
    if not phones:
        raise HTTPException(status_code=400, detail="Telefon raqami topilmadi.")

    gender = _normalize_ican_gender(common_data.get("gender"))
    if gender not in {"male", "female"}:
        raise HTTPException(status_code=400, detail="Jins ma'lumoti noto'g'ri.")

    birth_date = _parse_myid_date(common_data.get("birth_date"))
    passport_issue_date = _parse_myid_date(doc_data.get("issued_date"))
    passport_expiry_date = _parse_myid_date(doc_data.get("expiry_date"))
    if not birth_date or not passport_issue_date or not passport_expiry_date:
        raise HTTPException(status_code=400, detail="MyID sana ma'lumotlari to'liq emas.")

    passport = _normalize_pass_data(doc_data.get("pass_data"))
    pinfl = "".join(ch for ch in str(common_data.get("pinfl") or "") if ch.isdigit())
    if not passport or len(pinfl) != 14:
        raise HTTPException(status_code=400, detail="Passport yoki PINFL topilmadi.")

    last_name = _normalize_ican_person_name(common_data.get("last_name"))
    first_name = _normalize_ican_person_name(common_data.get("first_name"))
    middle_name = _normalize_ican_person_name(common_data.get("middle_name"))
    if not last_name or not first_name:
        raise HTTPException(status_code=400, detail="MyID ism-familiya ma'lumotlari to'liq emas.")

    passport_address = str(
        address.get("permanent_address")
        or permanent_registration.get("address")
        or ""
    ).strip()
    birth_address = str(common_data.get("birth_place") or common_data.get("birth_country") or "").strip()

    form_data: dict[str, Any] = {
        "credit___company_id": ICAN_CREDIT_COMPANY_ID,
        "credit___employee_id": ICAN_CREDIT_EMPLOYEE_ID,
        "credit___tariff_id": selected_plan["tariff_id"],
        "credit___amount": credit_amount,
        "credit___initial_payment": initial_payment,
        "credit___period": selected_plan["months"],
        "credit___payment_day": 1,
        "credit___products_note": _build_credit_products_note(products),
        "credit___comment": f"Website checkout order #{order['id']}",
        "user_document___passport": passport,
        "user_document___pinfl": pinfl,
        "user_document___last_name": last_name,
        "user_document___first_name": first_name,
        "user_document___middle_name": middle_name,
        "user_document___gender": gender,
        "user_document___birth_date": birth_date,
        "user_document___birth_address": birth_address,
        "user_document___passport_issue_date": passport_issue_date,
        "user_document___passport_expiry_date": passport_expiry_date,
        "person_main___district_id": district_id,
        "person_main___passport_address": passport_address,
    }

    if region_id > 0:
        form_data["person_main___region_id"] = region_id

    for index, phone in enumerate(phones):
        form_data[f"person_main___phones[{index}]"] = phone

    try:
        response = requests.post(
            f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_CREATE_PATH}",
            data=form_data,
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
    configured = str(explicit_url or "").strip() or MYID_REDIRECT_URL
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
def submit_order_credit_request(order_id: int) -> dict[str, Any]:
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

    if str(order.get("status") or "").strip().lower() == "completed":
        return {
            "success": True,
            "status": "completed",
            "payment_method": str(order.get("payment_method") or "myid").strip().lower() or "myid",
            "order_id": int(order["id"]),
            "result_note": str(order.get("myid_result_note") or order.get("click_error_note") or "").strip(),
        }

    try:
        credit_payload = _submit_ican_credit_for_order(order)
    except HTTPException as exc:
        update_order(
            int(order["id"]),
            myid_result_note=str(exc.detail),
        )
        raise

    success_note = "MyID tasdiqlandi va kredit arizasi yuborildi."
    updated_order = update_order(
        int(order["id"]),
        status="completed",
        payment_method="myid",
        myid_result_note=success_note,
        click_error_note=success_note,
    ) or order

    return {
        "success": True,
        "status": "completed",
        "payment_method": str(updated_order.get("payment_method") or "myid").strip().lower() or "myid",
        "order_id": int(updated_order["id"]),
        "result_note": success_note,
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

    if completed.get("myid_profile"):
        try:
            _submit_ican_credit_for_order(completed)
            completed = update_order(
                int(completed["id"]),
                click_error_note="Boshlang'ich to'lov qabul qilindi. Kredit arizasi yuborildi.",
                myid_result_note="Boshlang'ich to'lov qabul qilindi. Kredit arizasi yuborildi.",
            ) or completed
        except HTTPException as exc:
            completed = update_order(
                int(completed["id"]),
                click_error_note=(
                    "Boshlang'ich to'lov qabul qilindi, lekin kredit arizasini yuborib bo'lmadi. "
                    f"{exc.detail}"
                ),
                myid_result_note=str(exc.detail),
            ) or completed
        except Exception:
            completed = update_order(
                int(completed["id"]),
                click_error_note=(
                    "Boshlang'ich to'lov qabul qilindi, lekin kredit arizasini yuborib bo'lmadi."
                ),
                myid_result_note="ICAN credit request failed after CLICK payment.",
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
