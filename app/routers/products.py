import os
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import jwt  # type: ignore
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Security, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .. import models
from ..config import BACKEND_DIR, IMAGES_DIR, SECRET_KEY
from ..database import get_db

router = APIRouter()
security = HTTPBearer()


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


def parse_numeric_price(price: Optional[str]) -> Optional[float]:
    if not price:
        return None
    digits = "".join(ch for ch in str(price) if ch.isdigit())
    if not digits:
        return None
    return float(digits)


@router.get("/products")
def get_products(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1),
    offset: int = Query(0, ge=0),
):
    products = db.query(models.Product).offset(offset).limit(limit).all()

    result = []
    for p in products:
        result.append(
            {
                "id": p.id,
                "name_uz": p.name_uz,
                "name_ru": p.name_ru,
                "name_en": p.name_en,
                "description_uz": p.description_uz,
                "description_ru": p.description_ru,
                "description_en": p.description_en,
                "characteristic_uz": p.characteristic_uz,
                "characteristic_ru": p.characteristic_ru,
                "characteristic_en": p.characteristic_en,
                "price_uz": p.price_uz,
                "price_ru": p.price_ru,
                "price_en": p.price_en,
                "images": p.images.split(",") if p.images else [],
            }
        )

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
    products = db.query(models.Product).all()

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
            {
                "id": p.id,
                "name_uz": p.name_uz,
                "name_ru": p.name_ru,
                "name_en": p.name_en,
                "price_uz": p.price_uz,
                "price_ru": p.price_ru,
                "price_en": p.price_en,
                "images": p.images.split(",") if p.images else [],
            }
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
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    image_urls = [save_file(file) for file in files] if files else []

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
    oldImages: List[str] = Form([]),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

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

    return {
        "id": product.id,
        "name_uz": product.name_uz,
        "name_ru": product.name_ru,
        "name_en": product.name_en,
        "description_uz": product.description_uz,
        "description_ru": product.description_ru,
        "description_en": product.description_en,
        "characteristic_uz": product.characteristic_uz,
        "characteristic_ru": product.characteristic_ru,
        "characteristic_en": product.characteristic_en,
        "price_uz": product.price_uz,
        "price_ru": product.price_ru,
        "price_en": product.price_en,
        "images": product.images.split(",") if product.images else [],
    }


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
