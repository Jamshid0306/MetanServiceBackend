from __future__ import annotations

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
