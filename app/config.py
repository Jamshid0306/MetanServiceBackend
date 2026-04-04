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


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _resolve_telegram_login_client_id() -> str:
    explicit_value = os.getenv("TELEGRAM_LOGIN_CLIENT_ID", "").strip()
    if explicit_value:
        return explicit_value

    bot_token = os.getenv("TELEGRAM_LOGIN_BOT_TOKEN", "").strip()
    bot_id = bot_token.split(":", 1)[0].strip()
    return bot_id if bot_id.isdigit() else ""


DEFAULT_PROD_HOSTS = [
    "urganch-metan-servis.uz",
]

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    *[f"https://{host}" for host in DEFAULT_PROD_HOSTS],
]

configured_cors_origins = _split_csv(os.getenv("CORS_ORIGINS", ""))
CORS_ORIGINS = list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *configured_cors_origins]))
CORS_ALLOW_ORIGIN_REGEX = _env_str("CORS_ALLOW_ORIGIN_REGEX", "")

SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_here")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES = _env_int("ADMIN_ACCESS_TOKEN_EXPIRE_MINUTES", 1440)
ADMIN_REFRESH_TOKEN_EXPIRE_DAYS = _env_int("ADMIN_REFRESH_TOKEN_EXPIRE_DAYS", 30)
TELEGRAM_LOGIN_BOT_USERNAME = os.getenv("TELEGRAM_LOGIN_BOT_USERNAME", "").strip().lstrip("@")
TELEGRAM_LOGIN_BOT_TOKEN = os.getenv("TELEGRAM_LOGIN_BOT_TOKEN", "").strip()
TELEGRAM_LOGIN_CLIENT_ID = _resolve_telegram_login_client_id()
TELEGRAM_LOGIN_CLIENT_SECRET = os.getenv("TELEGRAM_LOGIN_CLIENT_SECRET", "").strip()
TELEGRAM_ORDER_BOT_TOKEN = os.getenv("TELEGRAM_ORDER_BOT_TOKEN", TELEGRAM_LOGIN_BOT_TOKEN).strip()
TELEGRAM_ORDER_CHAT_ID = os.getenv("TELEGRAM_ORDER_CHAT_ID", "").strip()
ICAN_CREDIT_API_URL = os.getenv("ICAN_CREDIT_API_URL", "https://api.credit.icangroup.uz").rstrip("/")
ICAN_CREDIT_CREATE_PATH = os.getenv("ICAN_CREDIT_CREATE_PATH", "/external/ican/credit/create")
ICAN_CREDIT_USERNAME = os.getenv("ICAN_CREDIT_USERNAME", "")
ICAN_CREDIT_PASSWORD = os.getenv("ICAN_CREDIT_PASSWORD", "")
ICAN_CREDIT_COMPANY_ID = _env_int("ICAN_CREDIT_COMPANY_ID", 1)
ICAN_CREDIT_EMPLOYEE_ID = _env_int("ICAN_CREDIT_EMPLOYEE_ID", 4)
ICAN_CREDIT_DEFAULT_PAYMENT_DAY = _env_int("ICAN_CREDIT_DEFAULT_PAYMENT_DAY", 5)
CLICK_SERVICE_ID = _env_int("CLICK_SERVICE_ID", 0)
CLICK_MERCHANT_ID = _env_int("CLICK_MERCHANT_ID", 0)
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "").strip()
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "").strip()
CLICK_PAYMENT_BASE_URL = os.getenv("CLICK_PAYMENT_BASE_URL", "https://my.click.uz/services/pay").strip()
CLICK_RETURN_URL = os.getenv("CLICK_RETURN_URL", "").strip().rstrip("/")
MYID_BASE_URL = os.getenv("MYID_BASE_URL", "https://myid.uz").strip().rstrip("/")
MYID_WEB_BASE_URL = os.getenv("MYID_WEB_BASE_URL", "https://web.myid.uz").strip().rstrip("/")
MYID_CLIENT_ID = os.getenv("MYID_CLIENT_ID", "").strip()
MYID_CLIENT_SECRET = os.getenv("MYID_CLIENT_SECRET", "").strip()
MYID_REDIRECT_URL = os.getenv("MYID_REDIRECT_URL", "").strip()
MYID_SCOPE = os.getenv("MYID_SCOPE", "common_data").strip()
MYID_METHOD = os.getenv("MYID_METHOD", "strong").strip().lower() or "strong"
MYID_MAX_RETRIES = _env_int("MYID_MAX_RETRIES", 3)
