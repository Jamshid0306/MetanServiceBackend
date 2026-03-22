import os
import json
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

import requests  # type: ignore
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, load_only

from .. import models
from ..auth import verify_token
from ..config import (
    BACKEND_DIR,
    ICAN_CREDIT_API_URL,
    ICAN_CREDIT_COMPANY_ID,
    ICAN_CREDIT_CREATE_PATH,
    ICAN_CREDIT_DEFAULT_PAYMENT_DAY,
    ICAN_CREDIT_EMPLOYEE_ID,
    ICAN_CREDIT_PASSWORD,
    ICAN_CREDIT_USERNAME,
    IMAGES_DIR,
)
from ..database import get_db
from ..orders_database import save_order

router = APIRouter()
CONFIG_SCHEMA_VERSION = 5
DEFAULT_FUEL_TYPE_LABEL = "Standart"
DEFAULT_TRANSMISSION_LABEL = "Standart"
SUMMARY_PRODUCT_COLUMNS = (
    models.Product.id,
    models.Product.name_uz,
    models.Product.name_ru,
    models.Product.name_en,
    models.Product.default_price,
    models.Product.price_uz,
    models.Product.price_ru,
    models.Product.price_en,
    models.Product.credit_enabled,
    models.Product.credit_months,
    models.Product.credit_percent,
    models.Product.credit_6m_percent,
    models.Product.credit_plans,
    models.Product.config_options,
    models.Product.images,
    models.Product.order,
    models.Product.is_active,
)


def save_file(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix
    filename = f"{uuid4()}{suffix.lower()}"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = IMAGES_DIR / filename
    with filepath.open("wb") as buffer:
        buffer.write(file.file.read())
    return f"/static/images/{filename}"

def parse_numeric_price(price: Optional[Any]) -> Optional[float]:
    if price is None:
        return None
    digits = "".join(ch for ch in str(price) if ch.isdigit())
    if not digits:
        return None
    return float(digits)


def parse_bool_flag(value: Optional[Any]) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_credit_percent(value: Optional[Any]) -> Optional[int]:
    parsed = parse_numeric_price(value)
    if parsed is None:
        return None
    return int(parsed)


def parse_credit_months(value: Optional[Any]) -> Optional[int]:
    parsed = parse_numeric_price(value)
    if parsed is None:
        return None

    months = int(parsed)
    return months if months > 0 else None


def parse_option_count(
    value: Optional[Any],
    default: Optional[int] = None,
) -> Optional[int]:
    parsed = parse_numeric_price(value)
    if parsed is None:
        return default

    count = int(parsed)
    return count if count > 0 else default


def normalize_phone_number(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return ""

    if digits.startswith("998") and len(digits) == 12:
        return digits

    if len(digits) == 9:
        return f"998{digits}"

    if len(digits) == 12:
        return digits

    return digits


def normalize_phone_list(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for value in values:
        phone = normalize_phone_number(value)
        if not phone or phone in seen:
            continue
        normalized.append(phone)
        seen.add(phone)

    return normalized


def normalize_ican_gender(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"m", "male", "erkak", "man", "м"}:
        return "male"
    if normalized in {"f", "female", "ayol", "woman", "ж", "j"}:
        return "female"
    return normalized


def build_products_note(products: list[dict[str, Any]]) -> str:
    notes: list[str] = []
    for product in products:
        name = str(product.get("name") or product.get("id") or "Product").strip()
        quantity = int(product.get("quantity") or 1)
        selected_options = product.get("selected_options") or []
        option_labels = [
            (
                f"{str(option.get('label')).strip()} x{count} ta"
                if (
                    isinstance(option, dict)
                    and str(option.get("group_key") or "").strip() == "cylinder_volume"
                    and (count := parse_option_count(option.get("count"))) is not None
                    and count > 1
                )
                else str(option.get("label")).strip()
            )
            for option in selected_options
            if isinstance(option, dict) and str(option.get("label") or "").strip()
        ]

        note = f"{name} x{quantity}"
        if option_labels:
            note += f" ({', '.join(option_labels)})"
        notes.append(note)

    return "; ".join(notes)


def extract_upstream_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts: list[str] = []
            for field, value in errors.items():
                if isinstance(value, list):
                    text = ", ".join(str(item).strip() for item in value if str(item).strip())
                else:
                    text = str(value).strip()
                if text:
                    parts.append(f"{field}: {text}")
            if parts:
                return "; ".join(parts)

    text = response.text.strip()
    if text:
        return text[:500]

    return "Credit service request failed."


def submit_ican_credit_request(
    credit_payload: "CreditOrderPayload",
    products: list[dict[str, Any]],
) -> dict[str, Any]:
    if not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ICAN credit integration credentials are not configured",
        )

    if credit_payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be greater than zero")

    if credit_payload.period <= 0:
        raise HTTPException(status_code=400, detail="Credit period must be greater than zero")

    phones = normalize_phone_list(credit_payload.phones)
    if not phones:
        raise HTTPException(status_code=400, detail="At least one valid phone number is required")

    payment_day = int(credit_payload.start_date.split("-")[-1]) if credit_payload.start_date else 0
    if payment_day <= 0:
        payment_day = ICAN_CREDIT_DEFAULT_PAYMENT_DAY

    form_data: dict[str, Any] = {
        "credit___company_id": ICAN_CREDIT_COMPANY_ID,
        "credit___employee_id": ICAN_CREDIT_EMPLOYEE_ID,
        "credit___tariff_id": credit_payload.tariff_id,
        "credit___amount": credit_payload.amount,
        "credit___initial_payment": credit_payload.initial_payment,
        "credit___period": credit_payload.period,
        "credit___payment_day": payment_day,
        "credit___start_date": credit_payload.start_date,
        "credit___products_note": build_products_note(products),
        "credit___comment": f"Website order for product IDs: {', '.join(str(product.get('id')) for product in products if product.get('id'))}",
        "user_document___passport": credit_payload.passport,
        "user_document___pinfl": credit_payload.pinfl,
        "user_document___last_name": credit_payload.last_name,
        "user_document___first_name": credit_payload.first_name,
        "user_document___middle_name": credit_payload.middle_name,
        "user_document___gender": normalize_ican_gender(credit_payload.gender),
        "user_document___birth_date": credit_payload.birth_date,
        "person_main___district_id": credit_payload.district_id,
    }

    if credit_payload.region_id is not None:
        form_data["person_main___region_id"] = credit_payload.region_id

    for index, phone in enumerate(phones):
        form_data[f"person_main___phones[{index}]"] = phone

    credit_url = f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_CREATE_PATH}"

    try:
        response = requests.post(
            credit_url,
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
        detail = extract_upstream_error_detail(response)
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
            detail="ICAN credit service returned an unexpected response",
        )

    return payload


def normalize_credit_plan_item(item: Any) -> Optional[dict[str, int]]:
    if not isinstance(item, dict):
        return None

    months = parse_credit_months(item.get("months"))
    percent = parse_credit_percent(item.get("percent"))

    if months is None or percent is None:
        return None

    return {
        "months": months,
        "percent": percent,
    }


def normalize_credit_plans(raw_plans: Optional[Any]) -> list[dict[str, int]]:
    source: Any = raw_plans

    if isinstance(raw_plans, str):
        if not raw_plans.strip():
            source = []
        else:
            try:
                source = json.loads(raw_plans)
            except json.JSONDecodeError:
                source = []

    if not isinstance(source, list):
        return []

    normalized: list[dict[str, int]] = []
    seen_months: set[int] = set()

    for item in source:
        plan = normalize_credit_plan_item(item)
        if not plan or plan["months"] in seen_months:
            continue

        normalized.append(plan)
        seen_months.add(plan["months"])

    normalized.sort(key=lambda plan: plan["months"])
    return normalized


def dump_credit_plans(raw_plans: Optional[Any]) -> Optional[str]:
    normalized = normalize_credit_plans(raw_plans)
    if not normalized:
        return None

    return json.dumps(normalized, ensure_ascii=False)


def resolve_legacy_credit_plans(product: models.Product) -> list[dict[str, int]]:
    plans: list[dict[str, int]] = []
    seen_months: set[int] = set()

    primary_months = parse_credit_months(product.credit_months)
    primary_percent = parse_credit_percent(product.credit_percent)
    if primary_months is not None and primary_percent is not None:
        plans.append(
            {
                "months": primary_months,
                "percent": primary_percent,
            }
        )
        seen_months.add(primary_months)

    credit_percent = product.credit_percent
    legacy_credit_percent = product.credit_6m_percent

    if credit_percent is None and legacy_credit_percent is not None:
        credit_percent = legacy_credit_percent

    credit_months = product.credit_months
    if credit_months is None and credit_percent is not None:
        credit_months = 6

    if (
        credit_months is not None
        and credit_percent is not None
        and credit_months not in seen_months
    ):
        plans.append(
            {
                "months": int(credit_months),
                "percent": int(credit_percent),
            }
        )
        seen_months.add(int(credit_months))

    six_month_percent = parse_credit_percent(product.credit_6m_percent)
    if six_month_percent is not None and 6 not in seen_months:
        plans.append(
            {
                "months": 6,
                "percent": six_month_percent,
            }
        )

    plans.sort(key=lambda plan: plan["months"])
    return plans


def load_credit_plans(product: models.Product) -> list[dict[str, int]]:
    plans = normalize_credit_plans(product.credit_plans)
    if plans:
        return plans

    return resolve_legacy_credit_plans(product)


def resolve_credit_fields(product: models.Product) -> tuple[Optional[int], Optional[int]]:
    plans = load_credit_plans(product)
    if not plans:
        return None, None

    primary_plan = plans[0]
    return primary_plan["months"], primary_plan["percent"]


def _parse_config_source(raw_options: Optional[Any]) -> dict[str, Any]:
    options_data: Any = raw_options
    if isinstance(raw_options, str):
        if not raw_options.strip():
            options_data = {}
        else:
            try:
                options_data = json.loads(raw_options)
            except json.JSONDecodeError:
                options_data = {}

    if not isinstance(options_data, dict):
        return {}

    return options_data


def _normalize_cylinder_volume_item(
    item: Any,
    index: int,
    prefix: str,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    label = str(item.get("label", "")).strip()
    if not label:
        return None

    option_id = str(item.get("id") or f"{prefix}-{index + 1}").strip()
    price_delta = parse_numeric_price(item.get("price_delta"))
    count = parse_option_count(item.get("count"), 1)

    return {
        "id": option_id,
        "label": label,
        "count": count,
        "price_delta": int(price_delta or 0),
    }


def _normalize_cylinder_volume_list(
    values: Any,
    prefix: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        option = _normalize_cylinder_volume_item(item, index, prefix)
        if option:
            normalized.append(option)

    return normalized


def _dedupe_cylinder_volumes_by_id(volumes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for v in volumes:
        oid = str(v.get("id") or "").strip()
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append(v)
    return out


def _collect_cylinder_volumes_from_transmission_raw(
    item: dict[str, Any],
    option_id: str,
    fallback_volumes: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Yangi: balonlar transmission ostida. Eski JSON: avlodlar ichidagi balonlar — birlashtiriladi."""
    direct = item.get("cylinder_volumes")
    if isinstance(direct, list) and direct:
        normalized = _normalize_cylinder_volume_list(direct, f"{option_id}-volume")
        if normalized:
            return normalized
    gens = item.get("generations")
    if isinstance(gens, list) and gens:
        merged: list[dict[str, Any]] = []
        for idx, gen in enumerate(gens):
            if isinstance(gen, dict):
                vols = gen.get("cylinder_volumes")
                if isinstance(vols, list):
                    merged.extend(
                        _normalize_cylinder_volume_list(vols, f"{option_id}-g{idx}-volume")
                    )
        if merged:
            return _dedupe_cylinder_volumes_by_id(merged)
    if fallback_volumes:
        return [dict(v) for v in fallback_volumes]
    return []


def _normalize_transmission_item(
    item: Any,
    index: int,
    prefix: str,
    fallback_cylinder_volumes: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    label = str(item.get("label", "")).strip()
    if not label:
        return None

    option_id = str(item.get("id") or f"{prefix}-{index + 1}").strip()
    cylinder_volumes = _collect_cylinder_volumes_from_transmission_raw(
        item, option_id, fallback_cylinder_volumes
    )
    price_delta = parse_numeric_price(item.get("price_delta"))

    return {
        "id": option_id,
        "label": label,
        "hidden": bool(item.get("hidden")),
        "price_delta": int(price_delta or 0),
        "cylinder_volumes": cylinder_volumes,
    }


def _normalize_transmission_list(
    values: Any,
    prefix: str,
    fallback_cylinder_volumes: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        option = _normalize_transmission_item(item, index, prefix, fallback_cylinder_volumes)
        if option:
            normalized.append(option)

    return normalized


def _normalize_fuel_type_item(
    item: Any,
    index: int,
    prefix: str,
    fallback_transmissions: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    label = str(item.get("label", "")).strip()
    if not label:
        return None

    option_id = str(item.get("id") or f"{prefix}-{index + 1}").strip()
    raw_transmissions = item.get("transmissions")
    transmissions = (
        _normalize_transmission_list(raw_transmissions, f"{option_id}-transmission")
        if isinstance(raw_transmissions, list)
        else [dict(transmission) for transmission in (fallback_transmissions or [])]
    )

    return {
        "id": option_id,
        "label": label,
        "hidden": bool(item.get("hidden")),
        "transmissions": transmissions,
    }


def _normalize_fuel_type_list(
    values: Any,
    prefix: str,
    fallback_transmissions: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        option = _normalize_fuel_type_item(item, index, prefix, fallback_transmissions)
        if option:
            normalized.append(option)

    return normalized


def _create_synthetic_transmission(cylinder_volumes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "transmission-default",
        "label": DEFAULT_TRANSMISSION_LABEL,
        "hidden": True,
        "price_delta": 0,
        "cylinder_volumes": [dict(volume) for volume in cylinder_volumes],
    }


def _create_synthetic_fuel_type(transmissions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "fuel-type-default",
        "label": DEFAULT_FUEL_TYPE_LABEL,
        "hidden": True,
        "transmissions": [dict(transmission) for transmission in transmissions],
    }


def _normalize_legacy_config(source: dict[str, Any]) -> list[dict[str, Any]]:
    legacy_volumes = _normalize_cylinder_volume_list(source.get("cylinder_volume"), "legacy-volume")
    legacy_transmissions = _normalize_transmission_list(
        source.get("transmission"),
        "legacy-transmission",
        legacy_volumes,
    )

    if legacy_transmissions:
        return [_create_synthetic_fuel_type(legacy_transmissions)]

    if legacy_volumes:
        return [_create_synthetic_fuel_type([_create_synthetic_transmission(legacy_volumes)])]

    direct_transmissions = _normalize_transmission_list(source.get("transmissions"), "transmission")
    if direct_transmissions:
        return [_create_synthetic_fuel_type(direct_transmissions)]

    return []


def normalize_config_options(raw_options: Optional[Any]) -> dict[str, Any]:
    options_data = _parse_config_source(raw_options)
    fuel_types = (
        _normalize_fuel_type_list(options_data.get("fuel_types"), "fuel-type")
        if isinstance(options_data.get("fuel_types"), list)
        else _normalize_legacy_config(options_data)
    )

    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "fuel_types": fuel_types,
    }


def dump_config_options(raw_options: Optional[Any]) -> Optional[str]:
    normalized = normalize_config_options(raw_options)
    if not normalized["fuel_types"]:
        return None
    return json.dumps(normalized, ensure_ascii=False)


class CreditOrderPayload(BaseModel):
    tariff_id: int
    amount: float
    initial_payment: float = 0
    period: int
    start_date: str
    passport: str
    pinfl: str
    last_name: str
    first_name: str
    middle_name: str
    gender: str
    birth_date: str
    region_id: Optional[int] = None
    district_id: int
    phones: List[str] = Field(default_factory=list)


class OrderPayload(BaseModel):
    name: str
    phone: str
    products: List[dict[str, Any]] = Field(default_factory=list)
    total: float = 0
    locale: str = "uz"
    order_type: str = "standard"
    credit: Optional[CreditOrderPayload] = None


def serialize_product(product: models.Product, include_full_details: bool = True) -> dict[str, Any]:
    credit_plans = load_credit_plans(product)
    credit_months, credit_percent = resolve_credit_fields(product)
    payload: dict[str, Any] = {
        "id": product.id,
        "order": getattr(product, "order", 999999),
        "name_uz": product.name_uz,
        "name_ru": product.name_ru,
        "name_en": product.name_en,
        "default_price": product.default_price,
        "price_uz": product.price_uz,
        "price_ru": product.price_ru,
        "price_en": product.price_en,
        "credit_enabled": bool(product.credit_enabled),
        "credit_months": credit_months,
        "credit_percent": credit_percent,
        "credit_6m_percent": product.credit_6m_percent,
        "credit_plans": credit_plans,
        "config_options": normalize_config_options(product.config_options),
        "images": product.images.split(",") if product.images else [],
        "is_active": bool(getattr(product, "is_active", 1)),
    }

    if include_full_details:
        payload.update(
            {
                "description_uz": product.description_uz,
                "description_ru": product.description_ru,
                "description_en": product.description_en,
                "characteristic_uz": product.characteristic_uz,
                "characteristic_ru": product.characteristic_ru,
                "characteristic_en": product.characteristic_en,
            }
        )

    return payload


@router.get("/products")
def get_products(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1),
    offset: int = Query(0, ge=0),
    include_full_details: bool = Query(False),
    include_inactive: bool = Query(
        False,
        description="If true, return inactive products too (admin). Public catalog uses false.",
    ),
):
    base = db.query(models.Product)
    if not include_inactive:
        base = base.filter(models.Product.is_active == 1)
    total = base.count()
    query = base.order_by(models.Product.order.asc(), models.Product.id.asc())
    if not include_full_details:
        query = query.options(load_only(*SUMMARY_PRODUCT_COLUMNS))

    products = query.offset(offset).limit(limit).all()

    result = []
    for p in products:
        result.append(serialize_product(p, include_full_details=include_full_details))

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "products": result,
    }


@router.get("/filter")
def filter_products(
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    db: Session = Depends(get_db),
):
    products = (
        db.query(models.Product)
        .filter(models.Product.is_active == 1)
        .options(load_only(*SUMMARY_PRODUCT_COLUMNS))
        .order_by(models.Product.order.asc(), models.Product.id.asc())
        .all()
    )

    result = []
    for p in products:
        numeric_price = parse_numeric_price(p.price_uz)
        if min_price is not None or max_price is not None:
            if numeric_price is None:
                continue
            if min_price is not None and numeric_price < min_price:
                continue
            if max_price is not None and numeric_price > max_price:
                continue
        result.append(
            serialize_product(p, include_full_details=False)
        )

    return {"products": result}


@router.post("/create")
async def create_product(
    name_uz: str = Form(...),
    name_ru: str = Form(...),
    name_en: str = Form(...),
    description_uz: str = Form(None),
    description_ru: str = Form(None),
    description_en: str = Form(None),
    characteristic_uz: str = Form(None),
    characteristic_ru: str = Form(None),
    characteristic_en: str = Form(None),
    default_price: str = Form(""),
    price_uz: str = Form(...),
    price_ru: str = Form(...),
    price_en: str = Form(...),
    credit_enabled: str = Form("false"),
    credit_plans: str = Form(""),
    credit_months: str = Form(""),
    credit_percent: str = Form(""),
    credit_6m_percent: str = Form(""),
    config_options: str = Form(""),
    order: str = Form("999999"),
    is_active: str = Form("true"),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    image_urls = [save_file(file) for file in files] if files else []
    is_credit_enabled = parse_bool_flag(credit_enabled)
    order_value = int(order) if str(order).strip().isdigit() else 999999
    is_active_value = 1 if parse_bool_flag(is_active) else 0
    parsed_credit_plans = normalize_credit_plans(credit_plans)

    if not parsed_credit_plans:
        parsed_credit_percent = parse_credit_percent(credit_percent or credit_6m_percent)
        parsed_credit_months = parse_credit_months(credit_months)
        if parsed_credit_months is not None and parsed_credit_percent is not None:
            parsed_credit_plans = [
                {
                    "months": parsed_credit_months,
                    "percent": parsed_credit_percent,
                }
            ]

    if is_credit_enabled and not parsed_credit_plans:
        raise HTTPException(status_code=400, detail="At least one credit plan is required")

    primary_credit_plan = parsed_credit_plans[0] if parsed_credit_plans else None
    six_month_plan = next(
        (plan for plan in parsed_credit_plans if plan["months"] == 6),
        None,
    )

    new_product = models.Product(
        name_uz=name_uz,
        name_ru=name_ru,
        name_en=name_en,
        description_uz=description_uz,
        description_ru=description_ru,
        description_en=description_en,
        characteristic_uz=characteristic_uz,
        characteristic_ru=characteristic_ru,
        characteristic_en=characteristic_en,
        default_price=default_price.strip() if isinstance(default_price, str) else default_price,
        price_uz=price_uz,
        price_ru=price_ru,
        price_en=price_en,
        credit_enabled=int(is_credit_enabled),
        credit_months=primary_credit_plan["months"] if is_credit_enabled and primary_credit_plan else None,
        credit_percent=primary_credit_plan["percent"] if is_credit_enabled and primary_credit_plan else None,
        credit_6m_percent=(
            six_month_plan["percent"]
            if is_credit_enabled and six_month_plan
            else None
        ),
        credit_plans=dump_credit_plans(parsed_credit_plans) if is_credit_enabled else None,
        config_options=dump_config_options(config_options),
        images=",".join(image_urls) if image_urls else None,
        order=order_value,
        is_active=is_active_value,
    )

    db.add(new_product)
    db.commit()
    db.refresh(new_product)

    return {"success": True, "product_id": new_product.id, "images": image_urls}


@router.put("/update/{product_id}")
async def update_product(
    product_id: int,
    name_uz: str = Form(...),
    name_ru: str = Form(...),
    name_en: str = Form(...),
    description_uz: str = Form(...),
    description_ru: str = Form(...),
    description_en: str = Form(...),
    characteristic_uz: str = Form(...),
    characteristic_ru: str = Form(...),
    characteristic_en: str = Form(...),
    default_price: str = Form(""),
    price_uz: str = Form(...),
    price_ru: str = Form(...),
    price_en: str = Form(...),
    credit_enabled: str = Form("false"),
    credit_plans: str = Form(""),
    credit_months: str = Form(""),
    credit_percent: str = Form(""),
    credit_6m_percent: str = Form(""),
    config_options: str = Form(""),
    order: str = Form("999999"),
    is_active: str = Form("true"),
    oldImages: List[str] = Form([]),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    is_credit_enabled = parse_bool_flag(credit_enabled)
    parsed_credit_plans = normalize_credit_plans(credit_plans)

    if not parsed_credit_plans:
        parsed_credit_percent = parse_credit_percent(credit_percent or credit_6m_percent)
        parsed_credit_months = parse_credit_months(credit_months)
        if parsed_credit_months is not None and parsed_credit_percent is not None:
            parsed_credit_plans = [
                {
                    "months": parsed_credit_months,
                    "percent": parsed_credit_percent,
                }
            ]

    if is_credit_enabled and not parsed_credit_plans:
        raise HTTPException(status_code=400, detail="At least one credit plan is required")

    primary_credit_plan = parsed_credit_plans[0] if parsed_credit_plans else None
    six_month_plan = next(
        (plan for plan in parsed_credit_plans if plan["months"] == 6),
        None,
    )

    product.name_uz = name_uz
    product.name_ru = name_ru
    product.name_en = name_en
    product.description_uz = description_uz
    product.description_ru = description_ru
    product.description_en = description_en
    product.characteristic_uz = characteristic_uz
    product.characteristic_ru = characteristic_ru
    product.characteristic_en = characteristic_en
    product.default_price = default_price.strip() if isinstance(default_price, str) else default_price
    product.price_uz = price_uz
    product.price_ru = price_ru
    product.price_en = price_en
    product.credit_enabled = int(is_credit_enabled)
    product.credit_months = (
        primary_credit_plan["months"] if product.credit_enabled and primary_credit_plan else None
    )
    product.credit_percent = (
        primary_credit_plan["percent"] if product.credit_enabled and primary_credit_plan else None
    )
    product.credit_6m_percent = (
        six_month_plan["percent"]
        if product.credit_enabled and six_month_plan
        else None
    )
    product.credit_plans = dump_credit_plans(parsed_credit_plans) if product.credit_enabled else None
    product.config_options = dump_config_options(config_options)
    order_value = int(order) if str(order).strip().isdigit() else 999999
    product.order = order_value
    product.is_active = 1 if parse_bool_flag(is_active) else 0

    keep_images = oldImages if oldImages else []
    new_files = [save_file(file) for file in files] if files else []
    product.images = ",".join(keep_images + new_files) if (keep_images or new_files) else None

    db.commit()
    db.refresh(product)

    return {
        "success": True,
        "product_id": product.id,
        "images": product.images.split(",") if product.images else [],
    }


@router.get("/product/detail/{product_id}")
def get_product_detail(
    product_id: int,
    db: Session = Depends(get_db),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not getattr(product, "is_active", 1):
        raise HTTPException(status_code=404, detail="Product not found")

    return serialize_product(product)


class ProductActivePayload(BaseModel):
    is_active: bool


@router.patch("/active/{product_id}")
def patch_product_active(
    product_id: int,
    payload: ProductActivePayload,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    product.is_active = 1 if payload.is_active else 0
    db.commit()
    db.refresh(product)

    return {"success": True, "product_id": product.id, "is_active": bool(product.is_active)}


@router.post("/order")
def create_order(payload: OrderPayload):
    name = payload.name.strip()
    phone = payload.phone.strip()
    products = payload.products or []
    phone_digits = "".join(ch for ch in phone if ch.isdigit())
    order_type = (payload.order_type or "standard").strip().lower()

    if not name or not phone or len(phone_digits) < 9 or not products:
        raise HTTPException(status_code=400, detail="Name, phone and products are required")

    if order_type not in {"standard", "credit"}:
        raise HTTPException(status_code=400, detail="Unsupported order type")

    response_payload: dict[str, Any] = {
        "success": True,
        "order_type": order_type,
    }

    if order_type == "credit":
        if payload.credit is None:
            raise HTTPException(status_code=400, detail="Credit order data is required")

        response_payload["credit"] = submit_ican_credit_request(payload.credit, products)

    save_order(
        name=name,
        phone=phone,
        products=products,
        total=float(payload.total or 0),
        locale=(payload.locale or "uz").strip() or "uz",
    )

    return response_payload


@router.delete("/{product_id}")
async def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if product.images:
        for img_path in product.images.split(","):
            full_path = BACKEND_DIR / img_path.lstrip("/")
            if full_path.exists():
                os.remove(full_path)

    db.delete(product)
    db.commit()
    return {"detail": "Product deleted successfully"}
