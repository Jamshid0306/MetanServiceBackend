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
    "urganchmetanservice.uz",
    "www.urganchmetanservice.uz",
    "urganch-metan-servis.uz",
    "www.urganch-metan-servis.uz",
]

DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:4173",
    "http://localhost:4173",
    *[f"https://{host}" for host in DEFAULT_PROD_HOSTS],
    *[f"http://{host}" for host in DEFAULT_PROD_HOSTS],
]

configured_cors_origins = _split_csv(os.getenv("CORS_ORIGINS", ""))
CORS_ORIGINS = list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *configured_cors_origins]))
CORS_ALLOW_ORIGIN_REGEX = _env_str(
    "CORS_ALLOW_ORIGIN_REGEX",
    r"^https?://("
    r"localhost"
    r"|127\.0\.0\.1"
    r"|0\.0\.0\.0"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
    r"|[a-z0-9-]+\.ngrok-free\.app"
    r"|[a-z0-9-]+\.ngrok\.io"
    r")(:\d+)?$",
)

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
