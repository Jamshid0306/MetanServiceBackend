from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests
from fastapi import HTTPException

from .config import (
    NASIYA_BOZOR_API_KEY,
    NASIYA_BOZOR_API_URL,
    NASIYA_BOZOR_CONNECT_TIMEOUT,
    NASIYA_BOZOR_READ_TIMEOUT,
)


REQUEST_TIMEOUT = (NASIYA_BOZOR_CONNECT_TIMEOUT, NASIYA_BOZOR_READ_TIMEOUT)
PERCENT_QUANTUM = Decimal("0.0001")
MONTHS_PER_YEAR = Decimal("12")
NASIYA_PUBLIC_ERROR_NOTE = (
    "Nasiya Bozor xizmatida vaqtinchalik xatolik. Qayta urinib ko'ring."
)

DIAGNOSTIC_MAX_DEPTH = 8
DIAGNOSTIC_MAX_ITEMS_PER_CONTAINER = 50
DIAGNOSTIC_MAX_NODES = 500
DIAGNOSTIC_MAX_STRING_CHARS = 2_000
DIAGNOSTIC_MAX_RESPONSE_CHARS = 8 * 1024
DIAGNOSTIC_REDACTED = "[REDACTED]"
DIAGNOSTIC_TRUNCATED = "[TRUNCATED]"

_SECRET_KEY_PARTS = (
    "authorization",
    "cookie",
    "apikey",
    "token",
    "password",
    "secret",
    "signature",
)
_PII_KEY_PARTS = (
    "phone",
    "passport",
    "jshshir",
    "pinfl",
    "address",
    "fullname",
    "birth",
    "image",
    "salary",
    "workplace",
    "email",
    "account",
    "card",
)
_PII_EXACT_KEYS = {
    "clientinn",
    "inn",
    "name",
    "reference",
    "rejectedvalue",
    "target",
    "value",
}
_PHONE_PATTERN = re.compile(r"(?<!\d)\+?998(?:[\s()\-]*\d){9}(?!\d)")
_PINFL_PATTERN = re.compile(r"(?<!\d)\d{14}(?!\d)")
_PASSPORT_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\s?\d{7}(?!\d)")
_EMAIL_PATTERN = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])"
)
_CARD_OR_ACCOUNT_PATTERN = re.compile(r"(?<!\d)(?:\d[\s-]?){15,19}\d(?!\d)")
_TEXT_SECRET_PATTERN = re.compile(
    r"(?i)\b("
    r"x[\s_-]*api[\s_-]*key|api[\s_-]*key|authorization|"
    r"access[\s_-]*token|refresh[\s_-]*token|password|"
    r"client[\s_-]*secret|secret|signature"
    r")(\s*[:=]\s*)(?:bearer\s+)?[^\s,;}\]]+"
)


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None

    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None

    return parsed if parsed.is_finite() else None


def _parse_positive_months(value: Any) -> int | None:
    parsed = _parse_decimal(value)
    if parsed is None or parsed <= 0 or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _parse_minor_amount(value: Any) -> int:
    parsed = _parse_decimal(value)
    if parsed is None or parsed < 0:
        return 0
    return int(parsed)


def _rounded_percent(value: Decimal) -> float:
    return float(value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP))


def normalize_nasiya_plan(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    plan_id = str(item.get("id") or "").strip()
    months = _parse_positive_months(item.get("durationMonths"))
    annual_percent = _parse_decimal(item.get("interestRatePct"))
    penalty_percent = _parse_decimal(item.get("penaltyRatePct")) or Decimal("0")

    if (
        not plan_id
        or months is None
        or annual_percent is None
        or annual_percent < 0
        or penalty_percent < 0
    ):
        return None

    term_percent = annual_percent * Decimal(months) / MONTHS_PER_YEAR
    monthly_percent = annual_percent / MONTHS_PER_YEAR

    return {
        "id": plan_id,
        "name": str(item.get("name") or "").strip(),
        "months": months,
        "annual_percent": _rounded_percent(annual_percent),
        "percent": _rounded_percent(term_percent),
        "monthly_percent": _rounded_percent(monthly_percent),
        "penalty_percent": _rounded_percent(penalty_percent),
        "min_amount": _parse_minor_amount(item.get("minPriceMinor")),
        "max_amount": _parse_minor_amount(item.get("maxPriceMinor")),
    }


def is_configured() -> bool:
    return bool(NASIYA_BOZOR_API_URL and NASIYA_BOZOR_API_KEY)


def _normalize_diagnostic_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _is_sensitive_diagnostic_key(value: Any) -> bool:
    normalized = _normalize_diagnostic_key(value)
    if not normalized:
        return False
    if normalized in _PII_EXACT_KEYS:
        return True
    return any(part in normalized for part in (*_SECRET_KEY_PARTS, *_PII_KEY_PARTS))


def _mask_sensitive_text(value: Any) -> str:
    text = str(value or "")
    if NASIYA_BOZOR_API_KEY:
        text = text.replace(NASIYA_BOZOR_API_KEY, DIAGNOSTIC_REDACTED)
    text = _TEXT_SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{DIAGNOSTIC_REDACTED}",
        text,
    )
    text = _PHONE_PATTERN.sub("[REDACTED_PHONE]", text)
    text = _PINFL_PATTERN.sub("[REDACTED_PINFL]", text)
    text = _PASSPORT_PATTERN.sub("[REDACTED_PASSPORT]", text)
    text = _EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
    text = _CARD_OR_ACCOUNT_PATTERN.sub("[REDACTED_ACCOUNT]", text)
    if len(text) > DIAGNOSTIC_MAX_STRING_CHARS:
        return f"{text[:DIAGNOSTIC_MAX_STRING_CHARS]}{DIAGNOSTIC_TRUNCATED}"
    return text


def _sanitize_diagnostic(
    value: Any,
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
    parent_key: str = "",
) -> Any:
    if budget is None:
        budget = {"nodes": DIAGNOSTIC_MAX_NODES}
    if budget["nodes"] <= 0 or depth > DIAGNOSTIC_MAX_DEPTH:
        return DIAGNOSTIC_TRUNCATED
    budget["nodes"] -= 1

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _mask_sensitive_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        preserves_validation_details = _normalize_diagnostic_key(parent_key) in {
            "constraint",
            "constraints",
            "errors",
        }
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= DIAGNOSTIC_MAX_ITEMS_PER_CONTAINER:
                sanitized["__truncated__"] = DIAGNOSTIC_TRUNCATED
                break
            key = _mask_sensitive_text(raw_key)
            if (
                not preserves_validation_details
                and _is_sensitive_diagnostic_key(raw_key)
            ):
                sanitized[key] = DIAGNOSTIC_REDACTED
            else:
                sanitized[key] = _sanitize_diagnostic(
                    item,
                    depth=depth + 1,
                    budget=budget,
                    parent_key=str(raw_key),
                )
        return sanitized
    if isinstance(value, (list, tuple)):
        sanitized_items = [
            _sanitize_diagnostic(
                item,
                depth=depth + 1,
                budget=budget,
                parent_key="" if isinstance(item, dict) else parent_key,
            )
            for item in value[:DIAGNOSTIC_MAX_ITEMS_PER_CONTAINER]
        ]
        if len(value) > DIAGNOSTIC_MAX_ITEMS_PER_CONTAINER:
            sanitized_items.append(DIAGNOSTIC_TRUNCATED)
        return sanitized_items
    return _mask_sensitive_text(value)


def _cap_diagnostic_response(value: Any) -> Any:
    sanitized = _sanitize_diagnostic(value)
    try:
        serialized = json.dumps(sanitized, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = _mask_sensitive_text(sanitized)
    if len(serialized) <= DIAGNOSTIC_MAX_RESPONSE_CHARS:
        return sanitized
    return {
        "__truncated__": True,
        "preview": serialized[:DIAGNOSTIC_MAX_RESPONSE_CHARS],
    }


def _response_diagnostic(response: requests.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        text = _mask_sensitive_text(response.text.strip())
        return text[:DIAGNOSTIC_MAX_RESPONSE_CHARS] or None
    return _cap_diagnostic_response(payload)


def _extract_error_from_diagnostic(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _mask_sensitive_text(value.strip())

        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts = []
            for field, value in errors.items():
                if isinstance(value, list):
                    text = ", ".join(str(item) for item in value)
                else:
                    text = str(value)
                if text.strip():
                    parts.append(_mask_sensitive_text(f"{field}: {text.strip()}"))
            if parts:
                return "; ".join(parts)

    if isinstance(payload, str) and payload.strip():
        return _mask_sensitive_text(payload.strip())[:500]
    return "Nasiya Bozor request failed"


def _diagnostic_detail(
    *,
    code: str,
    reason: str,
    upstream_status: int | None,
    method: str,
    path: str,
    nasiya_response: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "provider": "nasiya_bozor",
        "reason": _mask_sensitive_text(reason),
        "upstream_status": upstream_status,
        "method": str(method or "").upper(),
        "path": f"/{str(path or '').lstrip('/')}",
        "nasiya_response": nasiya_response,
    }


def request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not is_configured():
        raise HTTPException(
            status_code=500,
            detail=_diagnostic_detail(
                code="NASIYA_NOT_CONFIGURED",
                reason="Nasiya Bozor API kaliti sozlanmagan.",
                upstream_status=None,
                method=method,
                path=path,
                nasiya_response=None,
            ),
        )

    headers = {
        "Accept": "application/json",
        "X-Api-Key": NASIYA_BOZOR_API_KEY,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    url = f"{NASIYA_BOZOR_API_URL}/{path.lstrip('/')}"
    try:
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        if isinstance(exc, requests.Timeout):
            reason = "Nasiya Bozor request timed out"
        elif isinstance(exc, requests.ConnectionError):
            reason = "Nasiya Bozor connection failed"
        else:
            reason = f"Nasiya Bozor request failed ({type(exc).__name__})"
        raise HTTPException(
            status_code=503,
            detail=_diagnostic_detail(
                code="NASIYA_CONNECTION_ERROR",
                reason=reason,
                upstream_status=None,
                method=method,
                path=path,
                nasiya_response=None,
            ),
        ) from exc

    if response.status_code >= 400:
        nasiya_response = _response_diagnostic(response)
        raise HTTPException(
            status_code=502,
            detail=_diagnostic_detail(
                code="NASIYA_UPSTREAM_ERROR",
                reason=_extract_error_from_diagnostic(nasiya_response),
                upstream_status=response.status_code,
                method=method,
                path=path,
                nasiya_response=nasiya_response,
            ),
        )

    if response.status_code == 204 or not response.content:
        return {}

    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail=_diagnostic_detail(
                code="NASIYA_INVALID_RESPONSE",
                reason="Nasiya Bozor noto'g'ri JSON javob qaytardi.",
                upstream_status=response.status_code,
                method=method,
                path=path,
                nasiya_response=_cap_diagnostic_response(response.text.strip()),
            ),
        ) from exc

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail=_diagnostic_detail(
                code="NASIYA_INVALID_RESPONSE",
                reason="Nasiya Bozor kutilmagan formatda javob qaytardi.",
                upstream_status=response.status_code,
                method=method,
                path=path,
                nasiya_response=_cap_diagnostic_response(result),
            ),
        )
    return result


def unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def fetch_plans() -> dict[str, Any]:
    return request("GET", "/online-shop/plans")


def create_contract(payload: dict[str, Any]) -> dict[str, Any]:
    return request("POST", "/online-shop/contracts", payload=payload)


def fetch_contract(contract_id: str) -> dict[str, Any]:
    return request("GET", f"/online-shop/contracts/{contract_id}")


def pay_contract(
    contract_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    return request(
        "POST",
        f"/online-shop/contracts/{contract_id}/pay",
        payload=payload,
        idempotency_key=idempotency_key,
    )
