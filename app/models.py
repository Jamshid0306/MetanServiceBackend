from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from .database import Base

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name_uz = Column(String, nullable=False)
    name_ru = Column(String, nullable=False)
    name_en = Column(String, nullable=False)
    short_name_uz = Column(String, nullable=True)
    short_name_ru = Column(String, nullable=True)
    short_name_en = Column(String, nullable=True)
    description_uz = Column(Text)
    description_ru = Column(Text)
    description_en = Column(Text)
    characteristic_uz = Column(Text)
    characteristic_ru = Column(Text)
    characteristic_en = Column(Text)
    default_price = Column(String, nullable=True)
    price_uz = Column(String, nullable=False)
    price_ru = Column(String, nullable=False)
    price_en = Column(String, nullable=False)
    credit_enabled = Column(Integer, nullable=False, default=0)
    credit_months = Column(Integer, nullable=True)
    credit_percent = Column(Integer, nullable=True)
    credit_6m_percent = Column(Integer, nullable=True)
    credit_plans = Column(Text, nullable=True)
    initial_payment_enabled = Column(Integer, nullable=False, default=0)
    initial_payment_amount = Column(Integer, nullable=True)
    config_options = Column(Text, nullable=True)
    images = Column(String, nullable=True)
    # Display order for frontend listing (smaller = first; same order → by id).
    order = Column("order", Integer, nullable=False, default=999999)
    # 1 = shown on public site, 0 = hidden from catalog (admin still sees all).
    is_active = Column(Integer, nullable=False, default=1)


class HeroSlide(Base):
    __tablename__ = "hero_slides"

    id = Column(Integer, primary_key=True)
    image_path = Column(String, nullable=False)
    product_link = Column(String, nullable=True)
    duration_days = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class ExtraService(Base):
    __tablename__ = "extra_services"

    id = Column(Integer, primary_key=True)
    name_uz = Column(String, nullable=False)
    name_ru = Column(String, nullable=False)
    name_en = Column(String, nullable=False)
    characteristic_uz = Column(Text)
    characteristic_ru = Column(Text)
    characteristic_en = Column(Text)
    price_uz = Column(String, nullable=False)
    price_ru = Column(String, nullable=False)
    price_en = Column(String, nullable=False)
    image_path = Column(String, nullable=True)


class ProductExtraService(Base):
    __tablename__ = "product_extra_services"

    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    service_id = Column(Integer, ForeignKey("extra_services.id", ondelete="CASCADE"), primary_key=True)
