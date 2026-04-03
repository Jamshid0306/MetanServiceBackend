import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import (
    CLICK_MERCHANT_ID,
    CLICK_MERCHANT_USER_ID,
    CLICK_PAYMENT_BASE_URL,
    CLICK_RETURN_URL,
    CLICK_SECRET_KEY,
    CLICK_SERVICE_ID,
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


class ClickCheckoutPayload(BaseModel):
    name: str
    phone: str
    products: list[dict[str, Any]] = Field(default_factory=list)
    total: float = 0
    locale: str = "uz"
    return_url: str | None = None


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


@router.get("/orders/{order_id}")
def get_public_order_status(order_id: int) -> dict[str, Any]:
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "id": order["id"],
        "status": order["status"],
        "payment_method": order.get("payment_method") or "cash",
        "total": order.get("total") or 0,
        "click_error": order.get("click_error"),
        "click_error_note": order.get("click_error_note") or "",
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
