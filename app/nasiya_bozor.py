from __future__ import annotations

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


def _extract_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        errors = payload.get("errors")
        if isinstance(errors, dict):
            parts = []
            for field, value in errors.items():
                if isinstance(value, list):
                    text = ", ".join(str(item) for item in value)
                else:
                    text = str(value)
                if text.strip():
                    parts.append(f"{field}: {text.strip()}")
            if parts:
                return "; ".join(parts)

    return response.text.strip()[:500] or "Nasiya Bozor request failed"


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
            detail="Nasiya Bozor API kaliti sozlanmagan.",
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
        raise HTTPException(
            status_code=503,
            detail=f"Nasiya Bozor xizmati bilan aloqa qilib bo'lmadi: {exc}",
        ) from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Nasiya Bozor xatosi: {_extract_error(response)}",
        )

    if response.status_code == 204 or not response.content:
        return {}

    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="Nasiya Bozor noto'g'ri JSON javob qaytardi.",
        ) from exc

    if not isinstance(result, dict):
        raise HTTPException(
            status_code=502,
            detail="Nasiya Bozor kutilmagan formatda javob qaytardi.",
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
