import json
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from app import nasiya_bozor
from app.routers import nasiya, payments


def make_response(
    status_code,
    *,
    json_payload=None,
    text="",
    invalid_json=False,
):
    response = Mock(spec=requests.Response)
    response.status_code = status_code
    response.text = text
    response.content = text.encode("utf-8") or b"response"
    if invalid_json:
        response.json.side_effect = ValueError("invalid json")
    else:
        response.json.return_value = json_payload
    return response


class NasiyaDiagnosticContractTests(unittest.TestCase):
    def call_request(self, response):
        with (
            patch.object(
                nasiya_bozor,
                "NASIYA_BOZOR_API_URL",
                "https://nasiya.example/api/v1",
            ),
            patch.object(
                nasiya_bozor,
                "NASIYA_BOZOR_API_KEY",
                "super-secret-api-key",
            ),
            patch.object(
                nasiya_bozor.requests,
                "request",
                return_value=response,
            ) as request_mock,
        ):
            with self.assertRaises(HTTPException) as raised:
                nasiya_bozor.request(
                    "post",
                    "/online-shop/contracts",
                    payload={
                        "outgoingOnly": "OUTGOING_ONLY_MARKER",
                        "clientPhone": "+998901234567",
                    },
                )
        return raised.exception, request_mock

    def test_upstream_json_error_keeps_safe_validation_diagnostics(self):
        response = make_response(
            422,
            json_payload={
                "message": (
                    "Request validation failed for +998901234567, "
                    "12345678901234, AA1234567, person@example.com "
                    "and 8600 1234 5678 9012"
                ),
                "errors": [
                    {
                        "property": "clientPhone",
                        "value": "+998901234567",
                        "target": {
                            "clientPhone": "+998901234567",
                            "clientJshshir": "12345678901234",
                        },
                        "constraints": {
                            "isPhone": "clientPhone +998901234567 is invalid",
                        },
                    }
                ],
                "credentials": {
                    "apiKey": "super-secret-api-key",
                    "access_token": "provider-token",
                },
                "clientPassportNumber": "1234567",
                "inn": "123456789",
                "name": "Private Person",
                "reference": "private-reference",
                "email": "person@example.com",
                "cardNumber": "8600123456789012",
            },
        )

        error, request_mock = self.call_request(response)

        self.assertEqual(error.status_code, 502)
        self.assertEqual(
            {
                key: error.detail[key]
                for key in (
                    "code",
                    "provider",
                    "upstream_status",
                    "method",
                    "path",
                )
            },
            {
                "code": "NASIYA_UPSTREAM_ERROR",
                "provider": "nasiya_bozor",
                "upstream_status": 422,
                "method": "POST",
                "path": "/online-shop/contracts",
            },
        )
        self.assertIn("Request validation failed", error.detail["reason"])

        diagnostic = error.detail["nasiya_response"]
        validation_error = diagnostic["errors"][0]
        self.assertEqual(validation_error["property"], "clientPhone")
        self.assertEqual(
            validation_error["value"],
            nasiya_bozor.DIAGNOSTIC_REDACTED,
        )
        self.assertEqual(
            validation_error["target"],
            nasiya_bozor.DIAGNOSTIC_REDACTED,
        )
        self.assertIn(
            "[REDACTED_PHONE]",
            validation_error["constraints"]["isPhone"],
        )

        serialized = json.dumps(error.detail, ensure_ascii=False)
        for secret in (
            "super-secret-api-key",
            "provider-token",
            "+998901234567",
            "12345678901234",
            "AA1234567",
            "Private Person",
            "private-reference",
            "person@example.com",
            "8600123456789012",
            "8600 1234 5678 9012",
            "OUTGOING_ONLY_MARKER",
        ):
            self.assertNotIn(secret, serialized)

        sent_kwargs = request_mock.call_args.kwargs
        self.assertIn("X-Api-Key", sent_kwargs["headers"])
        self.assertNotIn("headers", error.detail)
        self.assertNotIn("payload", error.detail)

    def test_non_json_upstream_error_is_masked_and_capped(self):
        response = make_response(
            500,
            text=(
                "failed for +998901234567; api_key=super-secret-api-key; "
                + ("x" * 30_000)
            ),
            invalid_json=True,
        )

        error, _ = self.call_request(response)

        diagnostic = error.detail["nasiya_response"]
        self.assertIsInstance(diagnostic, str)
        self.assertIn("[REDACTED_PHONE]", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)
        self.assertNotIn("super-secret-api-key", diagnostic)
        self.assertLessEqual(
            len(diagnostic),
            nasiya_bozor.DIAGNOSTIC_MAX_RESPONSE_CHARS,
        )

    def test_connection_error_has_structure_without_request_data(self):
        with (
            patch.object(
                nasiya_bozor,
                "NASIYA_BOZOR_API_URL",
                "https://nasiya.example/api/v1",
            ),
            patch.object(
                nasiya_bozor,
                "NASIYA_BOZOR_API_KEY",
                "super-secret-api-key",
            ),
            patch.object(
                nasiya_bozor.requests,
                "request",
                side_effect=requests.ConnectTimeout("timed out"),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                nasiya_bozor.request(
                    "get",
                    "/online-shop/plans",
                    payload={"outgoingOnly": "OUTGOING_ONLY_MARKER"},
                )

        error = raised.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.detail["code"], "NASIYA_CONNECTION_ERROR")
        self.assertEqual(error.detail["provider"], "nasiya_bozor")
        self.assertEqual(error.detail["method"], "GET")
        self.assertEqual(error.detail["path"], "/online-shop/plans")
        self.assertIsNone(error.detail["upstream_status"])
        self.assertIsNone(error.detail["nasiya_response"])
        serialized = json.dumps(error.detail)
        self.assertNotIn("super-secret-api-key", serialized)
        self.assertNotIn("OUTGOING_ONLY_MARKER", serialized)

    def test_not_configured_error_uses_diagnostic_contract(self):
        with (
            patch.object(nasiya_bozor, "NASIYA_BOZOR_API_KEY", ""),
            patch.object(
                nasiya_bozor,
                "NASIYA_BOZOR_API_URL",
                "https://nasiya.example/api/v1",
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                nasiya_bozor.request("get", "/online-shop/plans")

        error = raised.exception
        self.assertEqual(error.status_code, 500)
        self.assertEqual(
            error.detail,
            {
                "code": "NASIYA_NOT_CONFIGURED",
                "provider": "nasiya_bozor",
                "reason": "Nasiya Bozor API kaliti sozlanmagan.",
                "upstream_status": None,
                "method": "GET",
                "path": "/online-shop/plans",
                "nasiya_response": None,
            },
        )

    def test_invalid_success_json_includes_only_sanitized_response_text(self):
        response = make_response(
            200,
            text=(
                "<html>+998901234567 "
                "authorization=BearerToken "
                "super-secret-api-key</html>"
            ),
            invalid_json=True,
        )

        error, _ = self.call_request(response)

        self.assertEqual(error.detail["code"], "NASIYA_INVALID_RESPONSE")
        self.assertEqual(error.detail["upstream_status"], 200)
        serialized = json.dumps(error.detail)
        self.assertNotIn("+998901234567", serialized)
        self.assertNotIn("BearerToken", serialized)
        self.assertNotIn("super-secret-api-key", serialized)

    def test_unexpected_success_shape_is_sanitized(self):
        response = make_response(
            200,
            json_payload=[
                {
                    "property": "clientPassportNumber",
                    "value": "1234567",
                    "clientPassportNumber": "1234567",
                }
            ],
            text="[]",
        )

        error, _ = self.call_request(response)

        self.assertEqual(error.detail["code"], "NASIYA_INVALID_RESPONSE")
        diagnostic = error.detail["nasiya_response"][0]
        self.assertEqual(diagnostic["property"], "clientPassportNumber")
        self.assertEqual(
            diagnostic["value"],
            nasiya_bozor.DIAGNOSTIC_REDACTED,
        )
        self.assertEqual(
            diagnostic["clientPassportNumber"],
            nasiya_bozor.DIAGNOSTIC_REDACTED,
        )

    def test_large_json_diagnostic_is_capped(self):
        response = make_response(
            422,
            json_payload={
                "message": "Request validation failed",
                "errors": [
                    {
                        "property": f"field{index}",
                        "constraints": {"invalid": "x" * 2_000},
                    }
                    for index in range(100)
                ],
            },
        )

        error, _ = self.call_request(response)

        serialized = json.dumps(error.detail["nasiya_response"])
        self.assertLessEqual(len(serialized), 16 * 1024)


class NasiyaPersistenceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.diagnostic_detail = {
            "code": "NASIYA_UPSTREAM_ERROR",
            "provider": "nasiya_bozor",
            "reason": "Request validation failed",
            "upstream_status": 422,
            "method": "POST",
            "path": "/online-shop/contracts",
            "nasiya_response": {
                "message": "Request validation failed",
                "secret": "must-not-be-persisted",
            },
        }
        self.upstream_error = HTTPException(
            status_code=502,
            detail=self.diagnostic_detail,
        )

    def test_submit_credit_preserves_myid_note_and_reraises_diagnostic(self):
        order = {
            "id": 42,
            "myid_profile": {"verified": True},
            "myid_result_code": 1,
            "products": [],
        }
        payload = payments.SubmitCreditPayload(
            phones=["+998901111111", "+998902222222"],
        )

        with (
            patch.object(payments, "get_order", return_value=order),
            patch.object(
                payments,
                "_build_nasiya_contract_for_order",
                return_value=({}, "plan-id", 0, 1_000.0),
            ),
            patch.object(
                payments,
                "create_nasiya_contract",
                side_effect=self.upstream_error,
            ),
            patch.object(payments, "update_order") as update_order_mock,
        ):
            with self.assertRaises(HTTPException) as raised:
                payments.submit_order_credit_request(42, payload)

        self.assertIs(raised.exception, self.upstream_error)
        update_kwargs = update_order_mock.call_args.kwargs
        self.assertEqual(
            update_kwargs["nasiya_error_note"],
            nasiya_bozor.NASIYA_PUBLIC_ERROR_NOTE,
        )
        self.assertNotIn("myid_result_note", update_kwargs)
        self.assertNotIn(
            "must-not-be-persisted",
            json.dumps(update_kwargs),
        )

    def test_contract_sync_persists_only_public_safe_note(self):
        order = {
            "id": 7,
            "phone": "998901234567",
            "total": 1_000,
            "nasiya_contract_id": "contract-id",
            "nasiya_contract_payload": {},
        }

        with (
            patch.object(
                nasiya,
                "fetch_contract",
                side_effect=self.upstream_error,
            ),
            patch.object(nasiya, "update_order") as update_order_mock,
            patch.object(
                nasiya,
                "get_monthly_payments_by_order_id",
                return_value=[],
            ),
            patch.object(nasiya, "get_customer_by_phone", return_value={}),
        ):
            nasiya._public_order(order, sync=True)

        update_kwargs = update_order_mock.call_args.kwargs
        self.assertEqual(
            update_kwargs["nasiya_error_note"],
            nasiya_bozor.NASIYA_PUBLIC_ERROR_NOTE,
        )
        self.assertNotIn(
            "must-not-be-persisted",
            json.dumps(update_kwargs),
        )

    def test_payment_failure_persists_only_public_safe_note(self):
        order = {
            "id": 9,
            "phone": "998901234567",
            "nasiya_contract_id": "contract-id",
        }
        payment = {
            "id": 11,
            "amount": 500,
            "credit_id": "contract-id",
        }
        payload = nasiya.ContractPaymentPayload(
            amountMinor=500,
            method="CLICK",
        )

        with (
            patch.object(nasiya, "get_order", return_value=order),
            patch.object(
                nasiya,
                "get_monthly_payment_by_idempotency_key",
                return_value=None,
            ),
            patch.object(
                nasiya,
                "fetch_contract",
                return_value={"data": {"remainingAmountMinor": 1_000}},
            ),
            patch.object(
                nasiya,
                "create_monthly_payment",
                return_value=payment,
            ),
            patch.object(
                nasiya,
                "pay_contract",
                side_effect=self.upstream_error,
            ),
            patch.object(
                nasiya,
                "update_monthly_payment",
            ) as update_payment_mock,
        ):
            with self.assertRaises(HTTPException) as raised:
                nasiya.register_contract_payment(
                    9,
                    payload,
                    idempotency_key="payment-key",
                )

        self.assertIs(raised.exception, self.upstream_error)
        update_kwargs = update_payment_mock.call_args.kwargs
        self.assertEqual(update_kwargs["status"], "failed")
        self.assertEqual(
            update_kwargs["nasiya_error_note"],
            nasiya_bozor.NASIYA_PUBLIC_ERROR_NOTE,
        )
        self.assertNotIn(
            "must-not-be-persisted",
            json.dumps(update_kwargs),
        )


if __name__ == "__main__":
    unittest.main()
