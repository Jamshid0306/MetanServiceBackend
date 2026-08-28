import base64
import binascii
import json
import threading
import time
from typing import Any

import requests  # type: ignore

from ..config import (
    TEXTUP_AUTH_URL,
    TEXTUP_DEBUG_RESPONSE,
    TEXTUP_EMAIL,
    TEXTUP_PASSWORD,
    TEXTUP_SMS_URL,
    TEXTUP_TIMEOUT_SECONDS,
    TEXTUP_TOKEN_TTL_SECONDS,
)


class TextUpError(RuntimeError):
    """Base error for safe TextUp integration failures."""

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        debug: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.public_detail = str(message or "TextUp request failed").strip()
        self.upstream_status = upstream_status
        self.debug = debug if TEXTUP_DEBUG_RESPONSE else None


class TextUpConfigurationError(TextUpError):
    """Raised when required TextUp credentials are missing."""


class TextUpDeliveryError(TextUpError):
    """Raised when TextUp cannot accept an SMS request."""


_token_lock = threading.Lock()
_cached_access_token = ""
_cached_refresh_token = ""
_cached_user_id = ""
_cached_token_expires_at = 0.0
_cached_auth_debug: dict[str, Any] | None = None

_SENSITIVE_DEBUG_KEYS = {
    "access",
    "accesstoken",
    "access_token",
    "authorization",
    "email",
    "password",
    "phone",
    "refresh",
    "refreshtoken",
    "refresh_token",
    "token",
}


def _sanitize_debug_payload(value: Any, key: str = "") -> Any:
    normalized_key = "".join(
        ch for ch in str(key).lower() if ch.isalnum() or ch == "_"
    )
    if normalized_key in _SENSITIVE_DEBUG_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_debug_payload(item, str(item_key))
            for item_key, item in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_sanitize_debug_payload(item) for item in value[:20]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:300]


def _response_debug(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        payload = str(response.text or "").strip()[:500]
    return _sanitize_debug_payload(payload)


def is_textup_configured() -> bool:
    return bool(TEXTUP_EMAIL and TEXTUP_PASSWORD and TEXTUP_AUTH_URL and TEXTUP_SMS_URL)


def _clear_token_cache() -> None:
    global _cached_access_token, _cached_refresh_token, _cached_user_id
    global _cached_token_expires_at, _cached_auth_debug
    with _token_lock:
        _cached_access_token = ""
        _cached_refresh_token = ""
        _cached_user_id = ""
        _cached_token_expires_at = 0.0
        _cached_auth_debug = None


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise TextUpDeliveryError("TextUp returned an invalid response") from error

    if not isinstance(payload, dict):
        raise TextUpDeliveryError("TextUp returned an invalid response")
    return payload


def _provider_error_detail(response: requests.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = str(response.text or "").strip()
        return f"TextUp: {text[:500]}" if text else fallback

    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"TextUp: {value.strip()[:500]}"

        errors = payload.get("errors")
        if isinstance(errors, list):
            values = [str(item).strip() for item in errors if str(item).strip()]
            if values:
                return f"TextUp: {'; '.join(values)[:500]}"
        if isinstance(errors, dict):
            values = [
                f"{field}: {value}"
                for field, value in errors.items()
                if str(value or "").strip()
            ]
            if values:
                return f"TextUp: {'; '.join(values)[:500]}"

    return fallback


def _response_field_paths(payload: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.append(path)
        if isinstance(value, dict) and len(paths) < 30:
            paths.extend(_response_field_paths(value, path))
        if len(paths) >= 30:
            break
    return paths[:30]


def _find_value(payload: dict[str, Any], *keys: str) -> Any:
    key_set = set(keys)
    queue: list[dict[str, Any]] = [payload]
    while queue:
        current = queue.pop(0)
        for key, value in current.items():
            if key in key_set and value not in (None, ""):
                return value
            if isinstance(value, dict):
                queue.append(value)
    return None


def _jwt_expiry_monotonic(access_token: str, now: float) -> float | None:
    try:
        encoded_payload = access_token.split(".")[1]
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
        expires_at = float(payload["exp"])
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        return None

    remaining = expires_at - time.time() - 30
    return now + remaining if remaining > 0 else None


def get_textup_credentials(*, force_refresh: bool = False) -> tuple[str, str]:
    global _cached_access_token, _cached_refresh_token, _cached_user_id
    global _cached_token_expires_at, _cached_auth_debug

    if not is_textup_configured():
        raise TextUpConfigurationError("TextUp SMS is not configured")

    with _token_lock:
        now = time.monotonic()
        if (
            not force_refresh
            and _cached_access_token
            and _cached_user_id
            and now < _cached_token_expires_at
        ):
            return _cached_access_token, _cached_user_id

        try:
            response = requests.post(
                TEXTUP_AUTH_URL,
                json={"email": TEXTUP_EMAIL, "password": TEXTUP_PASSWORD},
                headers={"Content-Type": "application/json"},
                timeout=TEXTUP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise TextUpDeliveryError("TextUp authentication is unavailable") from error

        if response.status_code >= 400:
            raise TextUpDeliveryError(
                _provider_error_detail(response, "TextUp authentication failed"),
                upstream_status=response.status_code,
            )

        payload = _response_json(response)
        _cached_auth_debug = _sanitize_debug_payload(payload)
        access_token = str(
            _find_value(payload, "access", "accessToken", "access_token", "token") or ""
        ).strip()
        refresh_token = str(
            _find_value(payload, "refresh", "refreshToken", "refresh_token") or ""
        ).strip()
        user_id_value = _find_value(payload, "userId", "user_id", "userid")
        if not user_id_value:
            user = _find_value(payload, "user")
            if isinstance(user, dict):
                user_id_value = user.get("id") or user.get("_id")
            elif user not in (None, ""):
                user_id_value = user
        if not user_id_value:
            for container in (payload, payload.get("data"), payload.get("result")):
                if isinstance(container, dict) and container.get("id") not in (None, ""):
                    user_id_value = container["id"]
                    break
        user_id = str(user_id_value or "").strip()
        if not access_token or not user_id:
            fields = ", ".join(_response_field_paths(payload)) or "empty response"
            raise TextUpDeliveryError(
                f"TextUp authentication response is missing access token or userId "
                f"(response fields: {fields})",
                upstream_status=response.status_code,
                debug={"auth_response": _cached_auth_debug},
            )

        fallback_ttl = max(60, TEXTUP_TOKEN_TTL_SECONDS)
        _cached_access_token = access_token
        _cached_refresh_token = refresh_token
        _cached_user_id = user_id
        _cached_token_expires_at = (
            _jwt_expiry_monotonic(access_token, now) or now + fallback_ttl
        )
        return _cached_access_token, _cached_user_id


def send_sms(phone: str, message: str) -> dict[str, Any]:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    normalized_message = str(message or "").strip()
    if not digits or not normalized_message:
        raise TextUpDeliveryError("SMS recipient and message are required")

    recipient = f"+{digits}"
    for attempt in range(2):
        access_token, user_id = get_textup_credentials(force_refresh=attempt > 0)
        try:
            response = requests.post(
                TEXTUP_SMS_URL,
                json={
                    "message": normalized_message,
                    "userId": user_id,
                    "recipients": [recipient],
                },
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=TEXTUP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise TextUpDeliveryError("TextUp SMS delivery is unavailable") from error

        if response.status_code in {401, 403} and attempt == 0:
            _clear_token_cache()
            continue
        if response.status_code >= 400:
            raise TextUpDeliveryError(
                _provider_error_detail(response, "TextUp rejected the SMS request"),
                upstream_status=response.status_code,
                debug={
                    "auth_response": _cached_auth_debug,
                    "sms_request": {
                        "message": normalized_message,
                        "userId": user_id,
                        "recipients": ["[REDACTED]"],
                    },
                    "sms_response": _response_debug(response),
                },
            )

        return _response_json(response)

    raise TextUpDeliveryError("TextUp authentication failed")
