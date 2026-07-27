from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from ..customers_database import get_customer_by_phone
from ..nasiya_bozor import (
    NASIYA_PUBLIC_ERROR_NOTE,
    create_contract,
    fetch_contract,
    fetch_plans,
    is_configured,
    normalize_nasiya_plan,
    pay_contract,
    unwrap_data,
)
from ..orders_database import (
    create_monthly_payment,
    create_order,
    get_monthly_payment_by_idempotency_key,
    get_monthly_payments_by_order_id,
    get_order,
    get_orders_by_phone,
    update_monthly_payment,
    update_order,
)


router = APIRouter()


class ContractItem(BaseModel):
    productName: str
    quantity: int = Field(default=1, ge=1)
    realUnitPriceMinor: int = Field(ge=1)


class ContractCreatePayload(BaseModel):
    installmentPlanId: str
    submitForApproval: bool = False
    clientFullName: str
    clientDateOfBirth: str
    clientGender: Literal["M", "F"]
    clientPhone: str
    clientPhone2: str = ""
    clientPhone3: str = ""
    clientAddress: str
    clientPassportSeries: str
    clientPassportNumber: str
    clientJshshir: str
    clientInn: str = ""
    clientGuarantorFullName: str = ""
    clientGuarantorPhone: str = ""
    clientWorkplace: str = ""
    clientSalaryMinor: int = Field(default=0, ge=0)
    clientNotes: str = ""
    clientImagePath: str = ""
    productImagePath: str = ""
    clientPassportImagePath: str = ""
    items: list[ContractItem] = Field(min_length=1)
    downPaymentMinor: int = Field(default=0, ge=0)
    notes: str = ""
    locale: str = "uz"
    products: list[dict[str, Any]] = Field(default_factory=list)


class ContractPaymentPayload(BaseModel):
    amountMinor: int = Field(gt=0)
    method: Literal["CASH", "CLICK", "PAYME", "UZCARD", "HUMO", "BANK_TRANSFER"]
    reference: str = ""
    notes: str = ""
    paymentKind: Literal["down_payment", "monthly"] = "monthly"


def _normalize_phone(value: Any, *, with_plus: bool = False) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 9:
        digits = f"998{digits}"
    if len(digits) != 12 or not digits.startswith("998"):
        return ""
    return f"+{digits}" if with_plus else digits


def _normalize_contract_status(payload: dict[str, Any]) -> str:
    contract = unwrap_data(payload)
    return str(contract.get("status") or "pending").strip().lower() or "pending"


def _extract_contract_id(payload: dict[str, Any]) -> str:
    contract = unwrap_data(payload)
    for key in ("id", "contractId", "contract_id"):
        value = str(contract.get(key) or "").strip()
        if value:
            return value
    return ""


def _payment_payload(payment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payment.get("id"),
        "amount": payment.get("amount") or 0,
        "status": payment.get("status") or "pending",
        "payment_kind": payment.get("payment_kind") or "monthly",
        "method": payment.get("provider_method") or "",
        "reference": payment.get("click_trans_id") or "",
        "error_note": payment.get("nasiya_error_note") or payment.get("click_error_note") or "",
        "created_at": payment.get("created_at"),
        "updated_at": payment.get("updated_at"),
    }


def _public_order(order: dict[str, Any], *, sync: bool = False) -> dict[str, Any]:
    contract_payload = order.get("nasiya_contract_payload") or {}
    contract_id = str(order.get("nasiya_contract_id") or "").strip()
    if sync and contract_id:
        try:
            contract_payload = fetch_contract(contract_id)
            contract = unwrap_data(contract_payload)
            update_order(
                int(order["id"]),
                status=_normalize_contract_status(contract_payload),
                nasiya_contract_status=_normalize_contract_status(contract_payload),
                nasiya_contract_payload=contract_payload,
                nasiya_error_note="",
                nasiya_last_synced_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            )
            order = get_order(int(order["id"])) or order
        except HTTPException:
            update_order(
                int(order["id"]),
                nasiya_error_note=NASIYA_PUBLIC_ERROR_NOTE,
            )

    contract = unwrap_data(contract_payload) if isinstance(contract_payload, dict) else {}
    payments = get_monthly_payments_by_order_id(int(order["id"]))
    if sync and contract_id:
        for payment in payments:
            retry_key = str(payment.get("idempotency_key") or "").strip()
            if (
                str(payment.get("status") or "").lower() != "completed"
                or not payment.get("nasiya_error_note")
                or not retry_key
            ):
                continue
            try:
                retry_response = pay_contract(
                    contract_id,
                    {
                        "amountMinor": int(round(float(payment.get("amount") or 0))),
                        "method": str(payment.get("provider_method") or "CLICK").upper(),
                        "reference": str(payment.get("click_trans_id") or ""),
                        "notes": (
                            "Boshlang'ich to'lov"
                            if payment.get("payment_kind") == "down_payment"
                            else "Oylik to'lov"
                        ),
                    },
                    idempotency_key=retry_key,
                )
                update_monthly_payment(
                    int(payment["id"]),
                    nasiya_response=retry_response,
                    nasiya_error_note="",
                )
            except HTTPException:
                continue
        payments = get_monthly_payments_by_order_id(int(order["id"]))
    customer = get_customer_by_phone(_normalize_phone(order.get("phone"))) or {}
    remaining = contract.get("remainingAmountMinor")
    paid = contract.get("paidAmountMinor")
    total = (
        contract.get("totalAmountMinor")
        or contract.get("totalMinor")
        or contract.get("contractAmountMinor")
    )
    monthly_amount = (
        contract.get("monthlyPaymentMinor")
        or contract.get("monthlyAmountMinor")
        or contract.get("installmentAmountMinor")
        or 0
    )
    return {
        "id": order["id"],
        "contract_id": contract_id,
        "installment_plan_id": order.get("nasiya_plan_id") or "",
        "status": str(contract.get("status") or order.get("nasiya_contract_status") or order.get("status") or "pending"),
        "payment_method": "nasiya",
        "customer_address": str(customer.get("address") or ""),
        "total": order.get("total") or 0,
        "products": order.get("products") or [],
        "contract": contract,
        "remaining_amount": remaining,
        "paid_amount": paid,
        "contract_total": total,
        "monthly_payment_amount": monthly_amount,
        "can_pay_monthly": bool(contract_id) and (remaining is None or int(remaining or 0) > 0),
        "credit_submitted": bool(contract_id),
        "monthly_payments": [_payment_payload(payment) for payment in payments],
        "status_note": order.get("nasiya_error_note") or "",
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
    }


@router.get("/meta")
def get_meta() -> dict[str, Any]:
    return {"enabled": is_configured()}


@router.get("/plans")
def get_plans() -> dict[str, Any]:
    payload = fetch_plans()
    raw_plans = payload.get("data")
    if not isinstance(raw_plans, list):
        raise HTTPException(status_code=502, detail="Tariflar noto'g'ri formatda qaytdi.")
    plans = [
        plan
        for item in raw_plans
        if (plan := normalize_nasiya_plan(item)) is not None
    ]
    return {"data": plans}


@router.post("/contracts")
def submit_contract(payload: ContractCreatePayload) -> dict[str, Any]:
    phone = _normalize_phone(payload.clientPhone)
    phone2 = _normalize_phone(payload.clientPhone2, with_plus=True)
    phone3 = _normalize_phone(payload.clientPhone3, with_plus=True)
    if not phone or not phone2 or not phone3:
        raise HTTPException(status_code=400, detail="3 ta to'g'ri telefon raqami kerak.")

    external_payload = payload.model_dump(
        exclude={"locale", "products"},
    )
    external_payload.update(
        {
            "clientPhone": f"+{phone}",
            "clientPhone2": phone2,
            "clientPhone3": phone3,
            "clientPassportSeries": payload.clientPassportSeries.strip().upper(),
            "clientPassportNumber": "".join(ch for ch in payload.clientPassportNumber if ch.isdigit()),
            "clientJshshir": "".join(ch for ch in payload.clientJshshir if ch.isdigit()),
        }
    )
    response = create_contract(external_payload)
    contract_id = _extract_contract_id(response)
    if not contract_id:
        raise HTTPException(status_code=502, detail="Javobda shartnoma ID topilmadi.")

    local_products = payload.products or [
        {
            "name": item.productName,
            "quantity": item.quantity,
            "price": item.realUnitPriceMinor,
            "credit_plan": {"tariff_id": payload.installmentPlanId},
        }
        for item in payload.items
    ]
    total = sum(item.realUnitPriceMinor * item.quantity for item in payload.items)
    status = _normalize_contract_status(response)
    order = create_order(
        name=payload.clientFullName.strip(),
        phone=phone,
        products=local_products,
        total=total,
        locale=payload.locale,
        status=status,
        payment_method="nasiya",
        nasiya_contract_id=contract_id,
        nasiya_plan_id=payload.installmentPlanId,
        nasiya_contract_status=status,
        nasiya_contract_payload=response,
    )
    return {
        "success": True,
        "order_id": order["id"],
        "contract_id": contract_id,
        "status": status,
        "contract": unwrap_data(response),
        "down_payment_minor": payload.downPaymentMinor,
    }


@router.get("/orders")
def list_contract_orders(
    phone: str = Query(...),
    sync: bool = Query(True),
) -> dict[str, Any]:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri.")
    orders = [
        _public_order(order, sync=sync)
        for order in get_orders_by_phone(normalized_phone)
        if str(order.get("nasiya_contract_id") or "").strip()
    ]
    return {"success": True, "orders": orders, "total": len(orders)}


@router.get("/contracts/{order_id}")
def get_contract_status(order_id: int, phone: str | None = Query(None)) -> dict[str, Any]:
    order = get_order(order_id)
    if not order or not order.get("nasiya_contract_id"):
        raise HTTPException(status_code=404, detail="Shartnoma topilmadi.")
    if phone and _normalize_phone(phone) != _normalize_phone(order.get("phone")):
        raise HTTPException(status_code=403, detail="Shartnoma boshqa mijozga tegishli.")
    return _public_order(order, sync=True)


@router.post("/contracts/{order_id}/pay")
def register_contract_payment(
    order_id: int,
    payload: ContractPaymentPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    order = get_order(order_id)
    if not order or not order.get("nasiya_contract_id"):
        raise HTTPException(status_code=404, detail="Shartnoma topilmadi.")

    request_key = str(idempotency_key or uuid4()).strip()
    existing = get_monthly_payment_by_idempotency_key(request_key)
    contract_id = str(order["nasiya_contract_id"])
    if existing and existing.get("nasiya_response") and not existing.get("nasiya_error_note"):
        return {"success": True, "payment": _payment_payload(existing)}
    if existing and (
        int(round(float(existing.get("amount") or 0))) != payload.amountMinor
        or str(existing.get("credit_id") or "") != contract_id
    ):
        raise HTTPException(status_code=409, detail="Idempotency-Key boshqa to'lovda ishlatilgan.")

    current_contract_payload = fetch_contract(contract_id)
    current_contract = unwrap_data(current_contract_payload)
    remaining_amount = current_contract.get("remainingAmountMinor")
    if remaining_amount is not None and payload.amountMinor > int(remaining_amount or 0):
        raise HTTPException(status_code=400, detail="To'lov qolgan summadan katta.")

    payment = existing or create_monthly_payment(
        order_id=order_id,
        credit_id=contract_id,
        phone=_normalize_phone(order.get("phone")),
        amount=payload.amountMinor,
        status="pending",
        payment_kind=payload.paymentKind,
        provider_method=payload.method,
        idempotency_key=request_key,
    )
    external_payload = payload.model_dump(exclude={"paymentKind"})
    try:
        response = pay_contract(
            contract_id,
            external_payload,
            idempotency_key=request_key,
        )
    except HTTPException:
        update_monthly_payment(
            int(payment["id"]),
            status="failed",
            nasiya_error_note=NASIYA_PUBLIC_ERROR_NOTE,
        )
        raise

    payment = update_monthly_payment(
        int(payment["id"]),
        status="completed",
        nasiya_response=response,
        nasiya_error_note="",
    ) or payment
    try:
        refreshed_contract = fetch_contract(contract_id)
        update_order(
            order_id,
            status=_normalize_contract_status(refreshed_contract),
            nasiya_contract_status=_normalize_contract_status(refreshed_contract),
            nasiya_contract_payload=refreshed_contract,
            nasiya_error_note="",
            nasiya_last_synced_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except HTTPException:
        pass
    return {"success": True, "payment": _payment_payload(payment), "response": response}
