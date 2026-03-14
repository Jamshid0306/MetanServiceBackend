import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import models
from ..auth import verify_token
from ..config import BACKEND_DIR, IMAGES_DIR
from ..database import get_db

router = APIRouter()


def save_slide_image(file: UploadFile) -> str:
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


def normalize_duration_days(value: Any) -> int:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Duration days must be a number") from exc

    if days <= 0:
        raise HTTPException(status_code=400, detail="Duration days must be greater than zero")

    return days


def cleanup_expired_hero_slides(db: Session) -> None:
    now = datetime.utcnow()
    expired_slides = (
        db.query(models.HeroSlide)
        .filter(models.HeroSlide.expires_at <= now)
        .all()
    )

    if not expired_slides:
        return

    for slide in expired_slides:
        delete_asset_file(slide.image_path)
        db.delete(slide)

    db.commit()


def serialize_slide(slide: models.HeroSlide) -> dict[str, Any]:
    now = datetime.utcnow()
    remaining_seconds = max((slide.expires_at - now).total_seconds(), 0)
    remaining_days = math.ceil(remaining_seconds / 86400) if remaining_seconds else 0
    created_at = slide.created_at.replace(microsecond=0).isoformat() + "Z"
    expires_at = slide.expires_at.replace(microsecond=0).isoformat() + "Z"

    return {
        "id": slide.id,
        "image_path": slide.image_path,
        "duration_days": slide.duration_days,
        "created_at": created_at,
        "expires_at": expires_at,
        "remaining_days": remaining_days,
    }


@router.get("")
def get_active_hero_slides(db: Session = Depends(get_db)):
    cleanup_expired_hero_slides(db)
    slides = (
        db.query(models.HeroSlide)
        .order_by(models.HeroSlide.created_at.desc())
        .all()
    )
    return {"slides": [serialize_slide(slide) for slide in slides]}


@router.get("/admin")
def get_admin_hero_slides(
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    cleanup_expired_hero_slides(db)
    slides = (
        db.query(models.HeroSlide)
        .order_by(models.HeroSlide.created_at.desc())
        .all()
    )
    return {"slides": [serialize_slide(slide) for slide in slides]}


@router.post("")
async def create_hero_slide(
    duration_days: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    cleanup_expired_hero_slides(db)
    normalized_duration_days = normalize_duration_days(duration_days)
    now = datetime.utcnow()
    image_path = save_slide_image(file)
    slide = models.HeroSlide(
        image_path=image_path,
        duration_days=normalized_duration_days,
        created_at=now,
        expires_at=now + timedelta(days=normalized_duration_days),
    )

    db.add(slide)
    db.commit()
    db.refresh(slide)

    return {"success": True, "slide": serialize_slide(slide)}


@router.put("/{slide_id}")
async def update_hero_slide(
    slide_id: int,
    duration_days: str = Form(...),
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    cleanup_expired_hero_slides(db)
    slide = db.query(models.HeroSlide).filter(models.HeroSlide.id == slide_id).first()
    if not slide:
        raise HTTPException(status_code=404, detail="Hero slide not found")

    normalized_duration_days = normalize_duration_days(duration_days)
    now = datetime.utcnow()

    if file is not None and file.filename:
        new_image_path = save_slide_image(file)
        delete_asset_file(slide.image_path)
        slide.image_path = new_image_path

    slide.duration_days = normalized_duration_days
    slide.created_at = now
    slide.expires_at = now + timedelta(days=normalized_duration_days)

    db.commit()
    db.refresh(slide)

    return {"success": True, "slide": serialize_slide(slide)}


@router.delete("/{slide_id}")
async def delete_hero_slide(
    slide_id: int,
    db: Session = Depends(get_db),
    token: dict = Depends(verify_token),
):
    slide = db.query(models.HeroSlide).filter(models.HeroSlide.id == slide_id).first()
    if not slide:
        raise HTTPException(status_code=404, detail="Hero slide not found")

    delete_asset_file(slide.image_path)
    db.delete(slide)
    db.commit()
    return {"success": True}
