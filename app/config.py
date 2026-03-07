import os
from pathlib import Path

from dotenv import load_dotenv


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = BACKEND_DIR / "static"
IMAGES_DIR = STATIC_DIR / "images"

load_dotenv(BACKEND_DIR / ".env")


def _split_csv(value: str) -> list[str]:
    return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]


DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    "https://urganchmetanservice.uz",
    "https://www.urganchmetanservice.uz",
    "http://urganchmetanservice.uz",
    "http://www.urganchmetanservice.uz",
]

CORS_ORIGINS = _split_csv(os.getenv("CORS_ORIGINS", "")) or DEFAULT_CORS_ORIGINS

SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_here")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
