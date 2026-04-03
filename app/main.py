from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .config import CORS_ORIGINS, STATIC_DIR
from .database import init_db
from .routers import admin, customers, hero_slides, payments, products

app = FastAPI(title="Shop API")

# 🔹 Jadval yaratishni shu yerda chaqiramiz
init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=500)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(products.router, prefix="/products", tags=["Products"])
app.include_router(customers.router, prefix="/customers", tags=["Customers"])
app.include_router(admin.router, prefix="/admin", tags=["Admin"])
app.include_router(hero_slides.router, prefix="/hero-slides", tags=["Hero Slides"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])
app.include_router(payments.click_router, tags=["Click"])
