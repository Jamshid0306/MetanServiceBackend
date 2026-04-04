import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import requests  # type: ignore

from .config import TELEGRAM_ORDER_BOT_TOKEN, TELEGRAM_ORDER_CHAT_ID

logger = logging.getLogger(__name__)

ORDER_OPTION_LABELS = {
    "fuel_type": "Yoqilgi turi",
    "transmission": "Dastur turi",
    "cylinder_volume": "Balon hajmi",
    "additional_service": "Qoshimcha xizmat",
}


def _telegram_api_base_url() -> str:
    if not TELEGRAM_ORDER_BOT_TOKEN:
        return ""
    return f"https://api.telegram.org/bot{TELEGRAM_ORDER_BOT_TOKEN}"


def is_cash_order_notification_configured() -> bool:
    return bool(TELEGRAM_ORDER_BOT_TOKEN and TELEGRAM_ORDER_CHAT_ID)


def _format_phone(phone: Any) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 12:
        return f"+{digits}"
    return str(phone or "").strip()


def _format_total(total: Any) -> str:
    raw_value = str(total or "").strip()
    if not raw_value:
        return "0 UZS"

    try:
        numeric = Decimal(str(total))
    except (InvalidOperation, ValueError):
        digits = "".join(ch for ch in raw_value if ch.isdigit())
        if not digits:
            return f"{raw_value} UZS"
        numeric = Decimal(digits)

    normalized = numeric.quantize(Decimal("0.01")).normalize()
    if normalized == normalized.to_integral_value():
        amount = f"{int(normalized):,}".replace(",", " ")
    else:
        amount = format(normalized, ",f").rstrip("0").rstrip(".").replace(",", " ")

    return f"{amount} UZS"


def _format_option_line(option: dict[str, Any]) -> str:
    group_key = str(option.get("group_key") or "").strip()
    label = str(option.get("label") or "").strip()
    if not label:
        return ""

    prefix = ORDER_OPTION_LABELS.get(group_key, group_key.replace("_", " ").strip() or "Variant")

    count = str(option.get("count") or "").strip()
    if group_key == "cylinder_volume" and count and count not in {"0", "1"}:
        label = f"{label} x{count}"

    return f"   - {prefix}: {label}"


def _format_product_line(index: int, product: dict[str, Any]) -> list[str]:
    name = str(product.get("name") or product.get("id") or "Mahsulot").strip()
    quantity = int(product.get("quantity") or 1)
    lines = [f"{index}. {name} x{quantity}"]

    selected_options = product.get("selected_options") or []
    if isinstance(selected_options, list):
        for option in selected_options:
            if not isinstance(option, dict):
                continue
            option_line = _format_option_line(option)
            if option_line:
                lines.append(option_line)

    return lines


def build_cash_order_message(order: dict[str, Any]) -> str:
    products = order.get("products") or []
    product_lines: list[str] = []

    if isinstance(products, list):
        for index, product in enumerate(products, start=1):
            if not isinstance(product, dict):
                continue
            product_lines.extend(_format_product_line(index, product))

    if not product_lines:
        product_lines.append("1. Mahsulotlar ro'yxati mavjud emas")

    return "\n".join(
        [
            "Yangi naqd buyurtma",
            f"Order ID: #{order.get('id')}",
            f"Mijoz: {str(order.get('name') or '').strip()}",
            f"Telefon: {_format_phone(order.get('phone'))}",
            "",
            "Mahsulotlar:",
            *product_lines,
            "",
            f"Jami: {_format_total(order.get('total'))}",
        ]
    )


def send_cash_order_notification(order: dict[str, Any]) -> bool:
    if not is_cash_order_notification_configured():
        logger.warning("Cash order notification skipped because Telegram order bot is not configured")
        return False

    api_base_url = _telegram_api_base_url()
    if not api_base_url:
        return False

    payload = {
        "chat_id": TELEGRAM_ORDER_CHAT_ID,
        "text": build_cash_order_message(order),
    }

    try:
        response = requests.post(
            f"{api_base_url}/sendMessage",
            json=payload,
            timeout=15,
        )
    except requests.RequestException as error:
        logger.exception("Telegram cash order notification failed: %s", error)
        return False

    try:
        response_payload = response.json()
    except ValueError:
        logger.error(
            "Telegram cash order notification returned non-JSON: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return False

    if response.status_code >= 400 or not response_payload.get("ok"):
        logger.error(
            "Telegram cash order notification failed: status=%s payload=%s response=%s",
            response.status_code,
            payload,
            response_payload,
        )
        return False

    return True
