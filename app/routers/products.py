import os
import json
import re
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

import requests  # type: ignore
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field, ValidationError
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
    ICAN_CREDIT_TARIFF_PATH,
    ICAN_CREDIT_USERNAME,
    IMAGES_DIR,
)
from ..database import get_db
from ..orders_database import save_order
from ..telegram_notifications import send_cash_order_notification

router = APIRouter()
CONFIG_SCHEMA_VERSION = 6
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
    models.Product.initial_payment_enabled,
    models.Product.initial_payment_amount,
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


def delete_asset_file(asset_path: str | None) -> None:
    if not asset_path:
        return

    full_path = BACKEND_DIR / asset_path.lstrip("/")
    if full_path.exists():
        os.remove(full_path)


def parse_numeric_price(price: Optional[Any]) -> Optional[float]:
    if price is None:
        return None
    digits = "".join(ch for ch in str(price) if ch.isdigit())
    if not digits:
        return None
    return float(digits)


def parse_decimal_number(value: Optional[Any]) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        return numeric if numeric == numeric else None

    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None

    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


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


def parse_positive_int(value: Optional[Any]) -> Optional[int]:
    parsed = parse_decimal_number(value)
    if parsed is None:
        return None

    numeric = int(parsed)
    return numeric if numeric > 0 else None


def format_percent_value(value: float) -> float | int:
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return int(rounded)
    return rounded


def extract_ican_tariff_months(item: dict[str, Any]) -> Optional[int]:
    name = str(item.get("name") or "").strip()
    name_match = re.search(r"(\d+)", name)
    if name_match:
        months = int(name_match.group(1))
        if months > 0:
            return months

    min_period = parse_positive_int(item.get("min_period"))
    max_period = parse_positive_int(item.get("max_period"))

    if min_period is not None and max_period is not None and min_period == max_period:
        return min_period

    return max_period or min_period


def normalize_ican_tariff_item(item: Any) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict) or parse_bool_flag(item.get("is_deleted")):
        return None

    tariff_id = parse_positive_int(item.get("id"))
    months = extract_ican_tariff_months(item)
    monthly_percent = parse_decimal_number(item.get("percent"))

    if tariff_id is None or months is None or monthly_percent is None or monthly_percent <= 0:
        return None

    min_amount = parse_numeric_price(item.get("min_amount"))
    max_amount = parse_numeric_price(item.get("max_amount"))
    name = str(item.get("name") or "").strip() or f"{months} oylik"

    return {
        "id": tariff_id,
        "name": name,
        "months": months,
        "percent": format_percent_value(monthly_percent * months),
        "monthly_percent": format_percent_value(monthly_percent),
        "min_amount": int(min_amount) if min_amount is not None else None,
        "max_amount": int(max_amount) if max_amount is not None else None,
        "company_id": parse_positive_int(item.get("company_id")),
        "type": str(item.get("type") or "").strip(),
        "period_type": str(item.get("period_type") or "").strip(),
        "created_at": str(item.get("created_at") or "").strip(),
        "updated_at": str(item.get("updated_at") or "").strip(),
    }


def normalize_ican_tariffs(items: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_months: set[int] = set()

    for item in items:
        tariff = normalize_ican_tariff_item(item)
        if tariff is None or tariff["months"] in seen_months:
            continue
        normalized.append(tariff)
        seen_months.add(tariff["months"])

    normalized.sort(key=lambda tariff: (tariff["months"], tariff["id"]))
    return normalized


def fetch_ican_credit_tariffs() -> dict[str, Any]:
    if not ICAN_CREDIT_USERNAME or not ICAN_CREDIT_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="ICAN credit integration credentials are not configured",
        )

    tariff_url = f"{ICAN_CREDIT_API_URL}{ICAN_CREDIT_TARIFF_PATH}"

    try:
        response = requests.get(
            tariff_url,
            auth=(ICAN_CREDIT_USERNAME, ICAN_CREDIT_PASSWORD),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=503,
            detail=f"ICAN credit tariff service is unavailable: {exc}",
        ) from exc

    if response.status_code >= 400:
        detail = extract_upstream_error_detail(response)
        raise HTTPException(
            status_code=502,
            detail=f"ICAN credit tariff service returned an error: {detail}",
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="ICAN credit tariff service returned an invalid response",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=502,
            detail="ICAN credit tariff service returned an unexpected response",
        )

    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raise HTTPException(
            status_code=502,
            detail="ICAN credit tariff service returned an unexpected response",
        )

    normalized_tariffs = normalize_ican_tariffs(raw_items)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}

    return {
        "data": normalized_tariffs,
        "meta": {
            **meta,
            "total": len(normalized_tariffs),
        },
    }


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


def normalize_product_id_list(raw_ids: Any) -> list[int]:
    source = raw_ids

    if isinstance(source, str):
        if not source.strip():
            source = []
        else:
            try:
                source = json.loads(source)
            except json.JSONDecodeError:
                source = []

    if not isinstance(source, list):
        return []

    normalized: list[int] = []
    seen: set[int] = set()

    for item in source:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue

        if value <= 0 or value in seen:
            continue

        normalized.append(value)
        seen.add(value)

    return normalized


def serialize_service(
    service: models.ExtraService,
    attached_product_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    return {
        "id": service.id,
        "name_uz": service.name_uz,
        "name_ru": service.name_ru,
        "name_en": service.name_en,
        "characteristic_uz": service.characteristic_uz,
        "characteristic_ru": service.characteristic_ru,
        "characteristic_en": service.characteristic_en,
        "price_uz": service.price_uz,
        "price_ru": service.price_ru,
        "price_en": service.price_en,
        "image_path": service.image_path,
        "product_ids": attached_product_ids or [],
    }


def load_service_product_ids_map(
    db: Session,
    *,
    service_ids: Optional[list[int]] = None,
    product_ids: Optional[list[int]] = None,
) -> dict[int, list[int]]:
    query = db.query(models.ProductExtraService)

    if service_ids:
        query = query.filter(models.ProductExtraService.service_id.in_(service_ids))

    if product_ids:
        query = query.filter(models.ProductExtraService.product_id.in_(product_ids))

    rows = query.all()
    result: dict[int, list[int]] = {}

    for row in rows:
        key = int(row.service_id)
        result.setdefault(key, []).append(int(row.product_id))

    for value in result.values():
        value.sort()

    return result


def load_product_service_ids_map(
    db: Session,
    *,
    product_ids: list[int],
) -> dict[int, list[int]]:
    rows = (
        db.query(models.ProductExtraService)
        .filter(models.ProductExtraService.product_id.in_(product_ids))
        .all()
    )
    result: dict[int, list[int]] = {}

    for row in rows:
        key = int(row.product_id)
        result.setdefault(key, []).append(int(row.service_id))

    for value in result.values():
        value.sort()

    return result


def load_services_by_ids(
    db: Session,
    service_ids: list[int],
) -> dict[int, models.ExtraService]:
    if not service_ids:
        return {}

    services = (
        db.query(models.ExtraService)
        .filter(models.ExtraService.id.in_(service_ids))
        .all()
    )
    return {service.id: service for service in services}


def attach_extra_services_to_products(
    db: Session,
    products_payload: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    product_ids = [int(product["id"]) for product in products_payload if product.get("id")]
    if not product_ids:
        return products_payload

    product_service_map = load_product_service_ids_map(db, product_ids=product_ids)
    service_ids = sorted(
        {
            service_id
            for service_list in product_service_map.values()
            for service_id in service_list
        }
    )
    services_by_id = load_services_by_ids(db, service_ids)

    for product in products_payload:
        linked_service_ids = product_service_map.get(int(product["id"]), [])
        product["extra_services"] = [
            serialize_service(services_by_id[service_id], attached_product_ids=[int(product["id"])])
            for service_id in linked_service_ids
            if service_id in services_by_id
        ]

    return products_payload


def sync_service_product_links(
    db: Session,
    *,
    service_id: int,
    product_ids: list[int],
) -> None:
    db.query(models.ProductExtraService).filter(
        models.ProductExtraService.service_id == service_id
    ).delete(synchronize_session=False)

    if not product_ids:
        return

    existing_product_ids = {
        product.id
        for product in db.query(models.Product.id)
        .filter(models.Product.id.in_(product_ids))
        .all()
    }

    for product_id in sorted(existing_product_ids):
        db.add(
            models.ProductExtraService(
                product_id=product_id,
                service_id=service_id,
            )
        )


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


def _infer_gearbox_from_transmissions(
    transmissions: list[dict[str, Any]],
) -> tuple[bool, int, int]:
    auto: Optional[dict[str, Any]] = None
    manual: Optional[dict[str, Any]] = None
    for tr in transmissions:
        lab = str(tr.get("label", "")).lower()
        if any(
            x in lab
            for x in ("avtomat", "автомат", "automatic", "auto")
        ):
            auto = tr
        if any(
            x in lab
            for x in ("mexanika", "mehanika", "manual", "механика", "mechanic")
        ):
            manual = tr
    if auto and manual:
        return (
            True,
            int(auto.get("price_delta", 0) or 0),
            int(manual.get("price_delta", 0) or 0),
        )
    return False, 0, 0


def _build_transmissions_from_fuel_shape(
    option_id: str,
    volumes: list[dict[str, Any]],
    gearbox_enabled: bool,
    auto_delta: int,
    manual_delta: int,
) -> list[dict[str, Any]]:
    def dup_volumes() -> list[dict[str, Any]]:
        return [dict(v) for v in volumes]

    if gearbox_enabled:
        out = [
            _normalize_transmission_item(
                {
                    "id": f"{option_id}-gearbox-auto",
                    "label": "Avtomat",
                    "hidden": False,
                    "price_delta": auto_delta,
                    "cylinder_volumes": dup_volumes(),
                },
                0,
                f"{option_id}-trans",
            ),
            _normalize_transmission_item(
                {
                    "id": f"{option_id}-gearbox-manual",
                    "label": "Mexanika",
                    "hidden": False,
                    "price_delta": manual_delta,
                    "cylinder_volumes": dup_volumes(),
                },
                1,
                f"{option_id}-trans",
            ),
        ]
    else:
        out = [
            _normalize_transmission_item(
                {
                    "id": f"{option_id}-gearbox-off",
                    "label": DEFAULT_TRANSMISSION_LABEL,
                    "hidden": False,
                    "price_delta": 0,
                    "cylinder_volumes": dup_volumes(),
                },
                0,
                f"{option_id}-trans",
            ),
        ]
    return [t for t in out if t]


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

    fuel_level_volumes = item.get("cylinder_volumes")
    has_fuel_volumes = isinstance(fuel_level_volumes, list) and len(fuel_level_volumes) > 0

    volumes_normalized: list[dict[str, Any]] = []
    gearbox_enabled = False
    automatic_price_delta = 0
    manual_price_delta = 0

    if has_fuel_volumes:
        volumes_normalized = _normalize_cylinder_volume_list(
            fuel_level_volumes,
            f"{option_id}-volume",
        )
        gearbox_enabled = bool(item.get("gearbox_program_enabled"))
        automatic_price_delta = int(parse_numeric_price(item.get("automatic_price_delta")) or 0)
        manual_price_delta = int(parse_numeric_price(item.get("manual_price_delta")) or 0)
    elif isinstance(item.get("transmissions"), list) and item["transmissions"]:
        raw_trs = item["transmissions"]
        merged_raw: list[dict[str, Any]] = []
        for tr in raw_trs:
            if isinstance(tr, dict):
                merged_raw.extend(
                    _collect_cylinder_volumes_from_transmission_raw(tr, [])
                )
        volumes_normalized = _normalize_cylinder_volume_list(
            merged_raw,
            f"{option_id}-volume",
        )
        normalized_trs = _normalize_transmission_list(raw_trs, f"{option_id}-legacy-trans")
        gb, automatic_price_delta, manual_price_delta = _infer_gearbox_from_transmissions(
            normalized_trs
        )
        gearbox_enabled = gb
    elif fallback_transmissions:
        return _normalize_fuel_type_item(
            {**item, "transmissions": fallback_transmissions},
            index,
            prefix,
            None,
        )
    else:
        volumes_normalized = []

    transmissions = _build_transmissions_from_fuel_shape(
        option_id,
        volumes_normalized,
        gearbox_enabled,
        automatic_price_delta,
        manual_price_delta,
    )

    return {
        "id": option_id,
        "label": label,
        "hidden": bool(item.get("hidden")),
        "gearbox_program_enabled": gearbox_enabled,
        "automatic_price_delta": automatic_price_delta,
        "manual_price_delta": manual_price_delta,
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
        "hidden": False,
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
    fuel_types_out: list[dict[str, Any]] = []
    for ft in normalized["fuel_types"]:
        tr0 = (ft.get("transmissions") or [{}])[0]
        cyl = tr0.get("cylinder_volumes") or []
        fuel_types_out.append(
            {
                "id": ft["id"],
                "label": ft["label"],
                "hidden": False,
                "cylinder_volumes": [
                    {
                        "id": v["id"],
                        "label": v["label"],
                        "count": v.get("count", 1),
                        "price_delta": v.get("price_delta", 0),
                    }
                    for v in cyl
                ],
                "gearbox_program_enabled": bool(ft.get("gearbox_program_enabled")),
                "automatic_price_delta": int(ft.get("automatic_price_delta", 0)),
                "manual_price_delta": int(ft.get("manual_price_delta", 0)),
            }
        )
    payload = {"schema_version": CONFIG_SCHEMA_VERSION, "fuel_types": fuel_types_out}
    return json.dumps(payload, ensure_ascii=False)


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
    payment_method: str = "cash"
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
        "initial_payment_enabled": bool(getattr(product, "initial_payment_enabled", 0)),
        "initial_payment_amount": getattr(product, "initial_payment_amount", None),
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
                "extra_services": [],
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

    if include_full_details:
        attach_extra_services_to_products(db, result)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "products": result,
    }


@router.get("/credit/tariffs")
def get_credit_tariffs():
    return fetch_ican_credit_tariffs()


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


class ExtraServicePayload(BaseModel):
    name_uz: str
    name_ru: str
    name_en: str
    characteristic_uz: str = ""
    characteristic_ru: str = ""
    characteristic_en: str = ""
    price_uz: str
    price_ru: str
    price_en: str
    product_ids: list[int] = Field(default_factory=list)


def parse_service_product_ids(raw_value: Any) -> list[int]:
    if isinstance(raw_value, list):
        return normalize_product_id_list(raw_value)

    if raw_value is None:
        return []

    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if not normalized:
            return []

        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in normalized.split(",")]

        if isinstance(parsed, list):
            return normalize_product_id_list(parsed)

        return []

    return normalize_product_id_list([raw_value])


async def parse_extra_service_request(
    request: Request,
) -> tuple[ExtraServicePayload, UploadFile | None]:
    content_type = (request.headers.get("content-type") or "").lower()
    payload_data: dict[str, Any]
    file: UploadFile | None = None

    if "application/json" in content_type:
        raw_payload = await request.json()
        if not isinstance(raw_payload, dict):
            raise HTTPException(status_code=400, detail="Invalid service payload")
        payload_data = dict(raw_payload)
    else:
        form = await request.form()
        payload_data = {
            "name_uz": form.get("name_uz", ""),
            "name_ru": form.get("name_ru", ""),
            "name_en": form.get("name_en", ""),
            "characteristic_uz": form.get("characteristic_uz", ""),
            "characteristic_ru": form.get("characteristic_ru", ""),
            "characteristic_en": form.get("characteristic_en", ""),
            "price_uz": form.get("price_uz", ""),
            "price_ru": form.get("price_ru", ""),
            "price_en": form.get("price_en", ""),
            "product_ids": form.get("product_ids", "[]"),
        }
        file_candidate = form.get("file")
        if getattr(file_candidate, "filename", ""):
            file = file_candidate

    payload_data["product_ids"] = parse_service_product_ids(payload_data.get("product_ids"))

    try:
        payload = ExtraServicePayload(**payload_data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    return payload, file


@router.get("/services")
def get_extra_services(
    db: Session = Depends(get_db),
    token: dict | None = Depends(verify_token),
):
    services = db.query(models.ExtraService).order_by(models.ExtraService.id.desc()).all()
    service_ids = [service.id for service in services]
    service_product_map = load_service_product_ids_map(db, service_ids=service_ids)

    return {
        "services": [
            serialize_service(service, service_product_map.get(service.id, []))
            for service in services
        ]
    }


@router.post("/services")
async def create_extra_service(
    request: Request,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    payload, file = await parse_extra_service_request(request)
    service = models.ExtraService(
        name_uz=payload.name_uz.strip(),
        name_ru=payload.name_ru.strip(),
        name_en=payload.name_en.strip(),
        characteristic_uz=payload.characteristic_uz.strip(),
        characteristic_ru=payload.characteristic_ru.strip(),
        characteristic_en=payload.characteristic_en.strip(),
        price_uz=payload.price_uz.strip(),
        price_ru=payload.price_ru.strip(),
        price_en=payload.price_en.strip(),
        image_path=save_file(file) if file is not None else None,
    )

    db.add(service)
    db.flush()
    sync_service_product_links(
        db,
        service_id=service.id,
        product_ids=normalize_product_id_list(payload.product_ids),
    )
    db.commit()
    db.refresh(service)

    return {
        "success": True,
        "service": serialize_service(
            service,
            normalize_product_id_list(payload.product_ids),
        ),
    }


@router.put("/services/{service_id}")
async def update_extra_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    payload, file = await parse_extra_service_request(request)
    service = db.query(models.ExtraService).filter(models.ExtraService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    service.name_uz = payload.name_uz.strip()
    service.name_ru = payload.name_ru.strip()
    service.name_en = payload.name_en.strip()
    service.characteristic_uz = payload.characteristic_uz.strip()
    service.characteristic_ru = payload.characteristic_ru.strip()
    service.characteristic_en = payload.characteristic_en.strip()
    service.price_uz = payload.price_uz.strip()
    service.price_ru = payload.price_ru.strip()
    service.price_en = payload.price_en.strip()

    if file is not None:
        new_image_path = save_file(file)
        delete_asset_file(service.image_path)
        service.image_path = new_image_path

    normalized_product_ids = normalize_product_id_list(payload.product_ids)
    sync_service_product_links(
        db,
        service_id=service.id,
        product_ids=normalized_product_ids,
    )
    db.commit()
    db.refresh(service)

    return {
        "success": True,
        "service": serialize_service(service, normalized_product_ids),
    }


@router.delete("/services/{service_id}")
def delete_extra_service(
    service_id: int,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    service = db.query(models.ExtraService).filter(models.ExtraService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    db.query(models.ProductExtraService).filter(
        models.ProductExtraService.service_id == service_id
    ).delete(synchronize_session=False)
    delete_asset_file(service.image_path)
    db.delete(service)
    db.commit()
    return {"success": True, "service_id": service_id}


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
    initial_payment_enabled: str = Form("false"),
    initial_payment_amount: str = Form(""),
    config_options: str = Form(""),
    order: str = Form("999999"),
    is_active: str = Form("true"),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    image_urls = [save_file(file) for file in files] if files else []
    is_credit_enabled = parse_bool_flag(credit_enabled)
    is_initial_payment_enabled = is_credit_enabled and parse_bool_flag(initial_payment_enabled)
    parsed_initial_payment_amount = int(parse_numeric_price(initial_payment_amount) or 0)
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

    if is_initial_payment_enabled and parsed_initial_payment_amount <= 0:
        raise HTTPException(status_code=400, detail="Initial payment amount is required")

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
        initial_payment_enabled=int(is_initial_payment_enabled),
        initial_payment_amount=(
            parsed_initial_payment_amount if is_initial_payment_enabled else None
        ),
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
    initial_payment_enabled: str = Form("false"),
    initial_payment_amount: str = Form(""),
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
    is_initial_payment_enabled = is_credit_enabled and parse_bool_flag(initial_payment_enabled)
    parsed_initial_payment_amount = int(parse_numeric_price(initial_payment_amount) or 0)
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

    if is_initial_payment_enabled and parsed_initial_payment_amount <= 0:
        raise HTTPException(status_code=400, detail="Initial payment amount is required")

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
    product.initial_payment_enabled = int(is_initial_payment_enabled)
    product.initial_payment_amount = (
        parsed_initial_payment_amount if is_initial_payment_enabled else None
    )
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

    payload = serialize_product(product)
    attach_extra_services_to_products(db, [payload])
    return payload


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
    payment_method = (payload.payment_method or "cash").strip().lower()

    if not name or not phone or len(phone_digits) < 9 or not products:
        raise HTTPException(status_code=400, detail="Name, phone and products are required")

    if order_type not in {"standard", "credit"}:
        raise HTTPException(status_code=400, detail="Unsupported order type")

    if payment_method not in {"cash", "credit"}:
        raise HTTPException(status_code=400, detail="Unsupported payment method")

    resolved_payment_method = "credit" if order_type == "credit" else payment_method

    response_payload: dict[str, Any] = {
        "success": True,
        "order_type": order_type,
        "payment_method": resolved_payment_method,
    }

    if order_type == "credit":
        if payload.credit is None:
            raise HTTPException(status_code=400, detail="Credit order data is required")

        response_payload["credit"] = submit_ican_credit_request(payload.credit, products)

    order_record = save_order(
        name=name,
        phone=phone,
        products=products,
        total=float(payload.total or 0),
        locale=(payload.locale or "uz").strip() or "uz",
        payment_method=resolved_payment_method,
    )

    if resolved_payment_method == "cash":
        send_cash_order_notification(order_record)

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

    db.query(models.ProductExtraService).filter(
        models.ProductExtraService.product_id == product_id
    ).delete(synchronize_session=False)

    if product.images:
        for img_path in product.images.split(","):
            full_path = BACKEND_DIR / img_path.lstrip("/")
            if full_path.exists():
                os.remove(full_path)

    db.delete(product)
    db.commit()
    return {"detail": "Product deleted successfully"}
