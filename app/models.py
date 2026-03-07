from sqlalchemy import Column, Integer, String, Text

from .database import Base

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name_uz = Column(String, nullable=False)
    name_ru = Column(String, nullable=False)
    name_en = Column(String, nullable=False)
    description_uz = Column(Text)
    description_ru = Column(Text)
    description_en = Column(Text)
    characteristic_uz = Column(Text)
    characteristic_ru = Column(Text)
    characteristic_en = Column(Text)
    price_uz = Column(String, nullable=False)
    price_ru = Column(String, nullable=False)
    price_en = Column(String, nullable=False)
    images = Column(String, nullable=True)
