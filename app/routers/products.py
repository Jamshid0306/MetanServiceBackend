import os
import json
from pathlib import Path
from typing import Any, List, Optional
from uuid import uuid4

import jwt  # type: ignore
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Security, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session, load_only

from .. import models
from ..config import BACKEND_DIR, IMAGES_DIR, SECRET_KEY
from ..database import get_db
from ..orders_database import save_order

router = APIRouter()
security = HTTPBearer()
CONFIG_SCHEMA_VERSION = 2
DEFAULT_TRANSMISSION_LABEL = "Standart"
DEFAULT_GENERATION_LABEL = "Standart"
SUMMARY_PRODUCT_COLUMNS = (
    models.Product.id,
    models.Product.name_uz,
    models.Product.name_ru,
    models.Product.name_en,
    models.Product.price_uz,
    models.Product.price_ru,
    models.Product.price_en,
    models.Product.credit_enabled,
    models.Product.credit_months,
    models.Product.credit_percent,
    models.Product.credit_6m_percent,
    models.Product.config_options,
    models.Product.images,
)


def save_file(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix
    filename = f"{uuid4()}{suffix.lower()}"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = IMAGES_DIR / filename
    with filepath.open("wb") as buffer:
        buffer.write(file.file.read())
    return f"/static/images/{filename}"


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


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


def resolve_credit_fields(product: models.Product) -> tuple[Optional[int], Optional[int]]:
    credit_percent = product.credit_percent
    legacy_credit_percent = product.credit_6m_percent

    if credit_percent is None and legacy_credit_percent is not None:
        credit_percent = legacy_credit_percent

    credit_months = product.credit_months
    if credit_months is None and credit_percent is not None:
        credit_months = 6

    return credit_months, credit_percent


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

    return {
        "id": option_id,
        "label": label,
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


def _normalize_generation_item(
    item: Any,
    index: int,
    prefix: str,
    fallback_volumes: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    label = str(item.get("label", "")).strip()
    if not label:
        return None

    option_id = str(item.get("id") or f"{prefix}-{index + 1}").strip()
    raw_volumes = item.get("cylinder_volumes")
    cylinder_volumes = (
        _normalize_cylinder_volume_list(raw_volumes, f"{option_id}-volume")
        if isinstance(raw_volumes, list)
        else [dict(volume) for volume in (fallback_volumes or [])]
    )
    price_delta = parse_numeric_price(item.get("price_delta"))

    return {
        "id": option_id,
        "label": label,
        "hidden": bool(item.get("hidden")),
        "price_delta": int(price_delta or 0),
        "cylinder_volumes": cylinder_volumes,
    }


def _normalize_generation_list(
    values: Any,
    prefix: str,
    fallback_volumes: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        option = _normalize_generation_item(item, index, prefix, fallback_volumes)
        if option:
            normalized.append(option)

    return normalized


def _normalize_transmission_item(
    item: Any,
    index: int,
    prefix: str,
    fallback_generations: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    label = str(item.get("label", "")).strip()
    if not label:
        return None

    option_id = str(item.get("id") or f"{prefix}-{index + 1}").strip()
    raw_generations = item.get("generations")
    generations = (
        _normalize_generation_list(raw_generations, f"{option_id}-generation")
        if isinstance(raw_generations, list)
        else [dict(generation) for generation in (fallback_generations or [])]
    )
    price_delta = parse_numeric_price(item.get("price_delta"))

    return {
        "id": option_id,
        "label": label,
        "hidden": bool(item.get("hidden")),
        "price_delta": int(price_delta or 0),
        "generations": generations,
    }


def _normalize_transmission_list(
    values: Any,
    prefix: str,
    fallback_generations: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        option = _normalize_transmission_item(item, index, prefix, fallback_generations)
        if option:
            normalized.append(option)

    return normalized


def _create_synthetic_generation(volumes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "generation-default",
        "label": DEFAULT_GENERATION_LABEL,
        "hidden": True,
        "price_delta": 0,
        "cylinder_volumes": [dict(volume) for volume in volumes],
    }


def _create_synthetic_transmission(generations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "transmission-default",
        "label": DEFAULT_TRANSMISSION_LABEL,
        "hidden": True,
        "price_delta": 0,
        "generations": [dict(generation) for generation in generations],
    }


def _normalize_legacy_config(source: dict[str, Any]) -> list[dict[str, Any]]:
    legacy_volumes = _normalize_cylinder_volume_list(source.get("cylinder_volume"), "legacy-volume")
    legacy_generations = _normalize_generation_list(
        source.get("cylinder_position"),
        "legacy-generation",
        legacy_volumes,
    )
    fallback_generations = (
        legacy_generations
        if legacy_generations
        else [_create_synthetic_generation(legacy_volumes)] if legacy_volumes else []
    )
    legacy_transmissions = _normalize_transmission_list(
        source.get("transmission"),
        "legacy-transmission",
        fallback_generations,
    )

    if legacy_transmissions:
        return legacy_transmissions

    if fallback_generations:
        return [_create_synthetic_transmission(fallback_generations)]

    return []


def normalize_config_options(raw_options: Optional[Any]) -> dict[str, Any]:
    options_data = _parse_config_source(raw_options)
    transmissions = (
        _normalize_transmission_list(options_data.get("transmissions"), "transmission")
        if isinstance(options_data.get("transmissions"), list)
        else _normalize_legacy_config(options_data)
    )

    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "transmissions": transmissions,
    }


def dump_config_options(raw_options: Optional[Any]) -> Optional[str]:
    normalized = normalize_config_options(raw_options)
    if not normalized["transmissions"]:
        return None
    return json.dumps(normalized, ensure_ascii=False)


class OrderPayload(BaseModel):
    name: str
    phone: str
    products: List[dict[str, Any]]
    total: float = 0
    locale: str = "uz"


def serialize_product(product: models.Product, include_full_details: bool = True) -> dict[str, Any]:
    credit_months, credit_percent = resolve_credit_fields(product)
    payload: dict[str, Any] = {
        "id": product.id,
        "name_uz": product.name_uz,
        "name_ru": product.name_ru,
        "name_en": product.name_en,
        "price_uz": product.price_uz,
        "price_ru": product.price_ru,
        "price_en": product.price_en,
        "credit_enabled": bool(product.credit_enabled),
        "credit_months": credit_months,
        "credit_percent": credit_percent,
        "credit_6m_percent": product.credit_6m_percent,
        "config_options": normalize_config_options(product.config_options),
        "images": product.images.split(",") if product.images else [],
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
):
    query = db.query(models.Product).order_by(models.Product.id.desc())
    if not include_full_details:
        query = query.options(load_only(*SUMMARY_PRODUCT_COLUMNS))

    products = query.offset(offset).limit(limit).all()

    result = []
    for p in products:
        result.append(serialize_product(p, include_full_details=include_full_details))

    total = db.query(models.Product).count()

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
        .options(load_only(*SUMMARY_PRODUCT_COLUMNS))
        .order_by(models.Product.id.desc())
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
    price_uz: str = Form(...),
    price_ru: str = Form(...),
    price_en: str = Form(...),
    credit_enabled: str = Form("false"),
    credit_months: str = Form(""),
    credit_percent: str = Form(""),
    credit_6m_percent: str = Form(""),
    config_options: str = Form(""),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    image_urls = [save_file(file) for file in files] if files else []
    is_credit_enabled = parse_bool_flag(credit_enabled)
    parsed_credit_percent = parse_credit_percent(credit_percent or credit_6m_percent)
    parsed_credit_months = parse_credit_months(credit_months)

    if is_credit_enabled and (parsed_credit_months is None or parsed_credit_percent is None):
        raise HTTPException(status_code=400, detail="Credit months and percent are required")

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
        price_uz=price_uz,
        price_ru=price_ru,
        price_en=price_en,
        credit_enabled=int(is_credit_enabled),
        credit_months=parsed_credit_months if is_credit_enabled else None,
        credit_percent=parsed_credit_percent if is_credit_enabled else None,
        credit_6m_percent=(
            parsed_credit_percent
            if is_credit_enabled and parsed_credit_months == 6
            else None
        ),
        config_options=dump_config_options(config_options),
        images=",".join(image_urls) if image_urls else None,
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
    price_uz: str = Form(...),
    price_ru: str = Form(...),
    price_en: str = Form(...),
    credit_enabled: str = Form("false"),
    credit_months: str = Form(""),
    credit_percent: str = Form(""),
    credit_6m_percent: str = Form(""),
    config_options: str = Form(""),
    oldImages: List[str] = Form([]),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    is_credit_enabled = parse_bool_flag(credit_enabled)
    parsed_credit_percent = parse_credit_percent(credit_percent or credit_6m_percent)
    parsed_credit_months = parse_credit_months(credit_months)

    if is_credit_enabled and (parsed_credit_months is None or parsed_credit_percent is None):
        raise HTTPException(status_code=400, detail="Credit months and percent are required")

    product.name_uz = name_uz
    product.name_ru = name_ru
    product.name_en = name_en
    product.description_uz = description_uz
    product.description_ru = description_ru
    product.description_en = description_en
    product.characteristic_uz = characteristic_uz
    product.characteristic_ru = characteristic_ru
    product.characteristic_en = characteristic_en
    product.price_uz = price_uz
    product.price_ru = price_ru
    product.price_en = price_en
    product.credit_enabled = int(is_credit_enabled)
    product.credit_months = parsed_credit_months if product.credit_enabled else None
    product.credit_percent = parsed_credit_percent if product.credit_enabled else None
    product.credit_6m_percent = (
        parsed_credit_percent
        if product.credit_enabled and parsed_credit_months == 6
        else None
    )
    product.config_options = dump_config_options(config_options)

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

    return serialize_product(product)


@router.post("/order")
def create_order(payload: OrderPayload):
    name = payload.name.strip()
    phone = payload.phone.strip()
    products = payload.products or []
    phone_digits = "".join(ch for ch in phone if ch.isdigit())

    if not name or not phone or len(phone_digits) < 9 or not products:
        raise HTTPException(status_code=400, detail="Name, phone and products are required")

    save_order(
        name=name,
        phone=phone,
        products=products,
        total=float(payload.total or 0),
        locale=(payload.locale or "uz").strip() or "uz",
    )

    return {"success": True}


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
