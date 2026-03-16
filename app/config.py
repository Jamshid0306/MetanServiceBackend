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


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError:
        return default


DEFAULT_PROD_HOSTS = [
    "urganchmetanservice.uz",
    "www.urganchmetanservice.uz",
    "urganch-metan-servis.uz",
    "www.urganch-metan-servis.uz",
]

DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    *[f"https://{host}" for host in DEFAULT_PROD_HOSTS],
    *[f"http://{host}" for host in DEFAULT_PROD_HOSTS],
]

configured_cors_origins = _split_csv(os.getenv("CORS_ORIGINS", ""))
CORS_ORIGINS = list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *configured_cors_origins]))

SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_here")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES", 1440)
ADMIN_REFRESH_TOKEN_EXPIRE_DAYS = _env_int("ADMIN_REFRESH_TOKEN_EXPIRE_DAYS", 30)
ICAN_CREDIT_API_URL = os.getenv("ICAN_CREDIT_API_URL", "https://api.credit.icangroup.uz").rstrip("/")
ICAN_CREDIT_CREATE_PATH = os.getenv("ICAN_CREDIT_CREATE_PATH", "/external/ican/credit/create")
ICAN_CREDIT_USERNAME = os.getenv("ICAN_CREDIT_USERNAME", "")
ICAN_CREDIT_PASSWORD = os.getenv("ICAN_CREDIT_PASSWORD", "")
ICAN_CREDIT_COMPANY_ID = _env_int("ICAN_CREDIT_COMPANY_ID", 1)
ICAN_CREDIT_EMPLOYEE_ID = _env_int("ICAN_CREDIT_EMPLOYEE_ID", 4)
ICAN_CREDIT_DEFAULT_PAYMENT_DAY = _env_int("ICAN_CREDIT_DEFAULT_PAYMENT_DAY", 5)
