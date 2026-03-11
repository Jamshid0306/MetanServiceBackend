from pydantic import BaseModel
from typing import List, Optional, Union

class ProductBase(BaseModel):
    price_uz: Union[str, float]
    price_ru: Union[str, float]
    price_en: Union[str, float]
    name_uz: str
    name_ru: str
    name_en: str
    description_uz: str
    description_ru: str
    description_en: str
    characteristic_uz: str
    characteristic_ru: str
    characteristic_en: str
    credit_enabled: bool = False
    credit_months: Optional[int] = None
    credit_percent: Optional[int] = None
    images: List[str]

class ProductCreate(ProductBase):
    pass

class Product(ProductBase):
    id: int

    class Config:
        orm_mode = True
