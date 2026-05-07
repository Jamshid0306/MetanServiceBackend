from pydantic import BaseModel, Field
from typing import List, Optional, Union

class CreditPlan(BaseModel):
    months: int
    percent: int

class ProductBase(BaseModel):
    price_uz: Union[str, float]
    price_ru: Union[str, float]
    price_en: Union[str, float]
    name_uz: str
    name_ru: str
    name_en: str
    short_name_uz: Optional[str] = None
    short_name_ru: Optional[str] = None
    short_name_en: Optional[str] = None
    description_uz: str
    description_ru: str
    description_en: str
    characteristic_uz: str
    characteristic_ru: str
    characteristic_en: str
    credit_enabled: bool = False
    credit_months: Optional[int] = None
    credit_percent: Optional[int] = None
    credit_plans: List[CreditPlan] = Field(default_factory=list)
    initial_payment_enabled: bool = False
    initial_payment_amount: Optional[int] = None
    images: List[str]

class ProductCreate(ProductBase):
    pass

class Product(ProductBase):
    id: int

    class Config:
        orm_mode = True
