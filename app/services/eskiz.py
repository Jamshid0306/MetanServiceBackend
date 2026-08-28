import threading
import time
from typing import Any

import requests  # type: ignore

from ..config import (
    ESKIZ_API_BASE_URL,
    ESKIZ_EMAIL,
    ESKIZ_FROM,
    ESKIZ_PASSWORD,
    ESKIZ_TIMEOUT_SECONDS,
    ESKIZ_TOKEN_TTL_SECONDS,
)


class EskizError(RuntimeError):
    """Base error for safe Eskiz integration failures."""


class EskizConfigurationError(EskizError):
    """Raised when required Eskiz credentials are missing."""


class EskizDeliveryError(EskizError):
    """Raised when Eskiz cannot accept an SMS request."""


_token_lock = threading.Lock()
_cached_token = ""
_cached_token_expires_at = 0.0


def is_eskiz_configured() -> bool:
    return bool(ESKIZ_EMAIL and ESKIZ_PASSWORD and ESKIZ_FROM)


def _clear_token_cache() -> None:
    global _cached_token, _cached_token_expires_at
    with _token_lock:
        _cached_token = ""
        _cached_token_expires_at = 0.0


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise EskizDeliveryError("Eskiz returned an invalid response") from error

    if not isinstance(payload, dict):
        raise EskizDeliveryError("Eskiz returned an invalid response")
    return payload


def get_eskiz_token(*, force_refresh: bool = False) -> str:
    global _cached_token, _cached_token_expires_at

    if not is_eskiz_configured():
        raise EskizConfigurationError("Eskiz SMS is not configured")

    with _token_lock:
        now = time.monotonic()
        if not force_refresh and _cached_token and now < _cached_token_expires_at:
            return _cached_token

        try:
            response = requests.post(
                f"{ESKIZ_API_BASE_URL}/auth/login",
                data={"email": ESKIZ_EMAIL, "password": ESKIZ_PASSWORD},
                timeout=ESKIZ_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise EskizDeliveryError("Eskiz authentication is unavailable") from error

        if response.status_code >= 400:
            raise EskizDeliveryError("Eskiz authentication failed")

        payload = _response_json(response)
        token = str((payload.get("data") or {}).get("token") or "").strip()
        if not token:
            raise EskizDeliveryError("Eskiz authentication token is missing")

        _cached_token = token
        _cached_token_expires_at = now + max(60, ESKIZ_TOKEN_TTL_SECONDS)
        return token


def send_sms(phone: str, message: str) -> dict[str, Any]:
    normalized_phone = "".join(ch for ch in str(phone or "") if ch.isdigit())
    normalized_message = str(message or "").strip()
    if not normalized_phone or not normalized_message:
        raise EskizDeliveryError("SMS recipient and message are required")

    for attempt in range(2):
        token = get_eskiz_token(force_refresh=attempt > 0)
        try:
            response = requests.post(
                f"{ESKIZ_API_BASE_URL}/message/sms/send",
                data={
                    "mobile_phone": normalized_phone,
                    "message": normalized_message,
                    "from": ESKIZ_FROM,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=ESKIZ_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise EskizDeliveryError("Eskiz SMS delivery is unavailable") from error

        if response.status_code in {401, 403} and attempt == 0:
            _clear_token_cache()
            continue

        if response.status_code >= 400:
            raise EskizDeliveryError("Eskiz rejected the SMS request")

        return _response_json(response)

    raise EskizDeliveryError("Eskiz authentication failed")
