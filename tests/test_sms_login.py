import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import customers_database
from app.routers import customers
from app.services import textup


class SmsOtpStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            customers_database,
            "DB_PATH",
            Path(self.temp_dir.name) / "sms-login.db",
        )
        self.db_patch.start()
        customers_database.init_customer_sms_login_otps_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def reserve(self, code_hash="code-hash"):
        return customers_database.reserve_customer_sms_login_otp(
            "998901234567",
            code_hash,
            ttl_seconds=120,
            resend_seconds=60,
            rate_window_seconds=600,
            max_sends=5,
            max_attempts=5,
        )

    def test_code_is_one_time_and_incorrect_attempts_are_counted(self):
        self.assertEqual(self.reserve()["status"], "reserved")

        invalid = customers_database.verify_customer_sms_login_otp(
            "998901234567",
            "wrong-hash",
        )
        self.assertEqual(invalid, {"status": "invalid", "attempts_remaining": 4})

        verified = customers_database.verify_customer_sms_login_otp(
            "998901234567",
            "code-hash",
        )
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(
            customers_database.verify_customer_sms_login_otp(
                "998901234567",
                "code-hash",
            )["status"],
            "missing",
        )

    def test_resend_cooldown_is_enforced(self):
        self.assertEqual(self.reserve()["status"], "reserved")
        blocked = self.reserve("new-code-hash")
        self.assertEqual(blocked["status"], "cooldown")
        self.assertGreater(blocked["retry_after"], 0)


class TextUpServiceTests(unittest.TestCase):
    def tearDown(self):
        textup._clear_token_cache()

    def test_sms_uses_login_token_user_id_and_expected_textup_json(self):
        login_response = Mock(status_code=200)
        login_response.json.return_value = {
            "accessToken": "textup-token",
            "refreshToken": "textup-refresh-token",
            "userId": "textup-user-id",
        }
        sms_response = Mock(status_code=200)
        sms_response.json.return_value = {"status": "success"}

        with (
            patch.object(textup, "TEXTUP_EMAIL", "sms@example.com"),
            patch.object(textup, "TEXTUP_PASSWORD", "secret"),
            patch.object(textup.requests, "post", side_effect=[login_response, sms_response]) as post,
        ):
            result = textup.send_sms("+998 90 123 45 67", "Metan kodi: 123456")

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {"email": "sms@example.com", "password": "secret"},
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["json"],
            {
                "message": "Metan kodi: 123456",
                "userId": "textup-user-id",
                "recipients": ["+998901234567"],
            },
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["headers"],
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer textup-token",
            },
        )

    def test_auth_accepts_nested_user_id(self):
        login_response = Mock(status_code=200)
        login_response.json.return_value = {
            "data": {
                "access_token": "textup-token",
                "refresh_token": "textup-refresh-token",
                "user": {"id": "nested-user-id"},
            }
        }

        with (
            patch.object(textup, "TEXTUP_EMAIL", "sms@example.com"),
            patch.object(textup, "TEXTUP_PASSWORD", "secret"),
            patch.object(textup.requests, "post", return_value=login_response),
        ):
            access_token, user_id = textup.get_textup_credentials()

        self.assertEqual(access_token, "textup-token")
        self.assertEqual(user_id, "nested-user-id")

    def test_provider_error_is_exposed_without_tokens(self):
        login_response = Mock(status_code=401, text="")
        login_response.json.return_value = {
            "message": "Email yoki parol noto'g'ri",
            "accessToken": "must-not-leak",
        }

        with (
            patch.object(textup, "TEXTUP_EMAIL", "sms@example.com"),
            patch.object(textup, "TEXTUP_PASSWORD", "secret"),
            patch.object(textup.requests, "post", return_value=login_response),
        ):
            with self.assertRaises(textup.TextUpDeliveryError) as raised:
                textup.get_textup_credentials()

        self.assertIn("Email yoki parol noto'g'ri", raised.exception.public_detail)
        self.assertNotIn("must-not-leak", raised.exception.public_detail)

    def test_debug_payload_redacts_provider_secrets_and_personal_data(self):
        sanitized = textup._sanitize_debug_payload(
            {
                "accessToken": "access-secret",
                "refreshToken": "refresh-secret",
                "user": {
                    "id": "user-id",
                    "email": "person@example.com",
                    "phone": "+998901234567",
                    "role": "client",
                },
            }
        )

        self.assertEqual(sanitized["accessToken"], "[REDACTED]")
        self.assertEqual(sanitized["refreshToken"], "[REDACTED]")
        self.assertEqual(sanitized["user"]["email"], "[REDACTED]")
        self.assertEqual(sanitized["user"]["phone"], "[REDACTED]")
        self.assertEqual(sanitized["user"]["id"], "user-id")


class SmsLoginEndpointTests(unittest.TestCase):
    def test_send_endpoint_never_returns_the_otp(self):
        customer = {"id": 7, "name": "Test", "phone": "998901234567"}
        with (
            patch.object(customers, "is_textup_configured", return_value=True),
            patch.object(customers, "get_customer_by_phone", return_value=customer),
            patch.object(customers.secrets, "randbelow", return_value=2345),
            patch.object(
                customers,
                "reserve_customer_sms_login_otp",
                return_value={"status": "reserved", "expires_in": 120, "retry_after": 60},
            ),
            patch.object(customers, "send_sms") as send_sms,
        ):
            result = customers.send_customer_sms_login_code(
                customers.CustomerSmsLoginSendPayload(phone="+998 90 123 45 67")
            )

        self.assertTrue(result["success"])
        self.assertNotIn("code", result)
        self.assertEqual(send_sms.call_args.args[0], "998901234567")
        self.assertIn("3345", send_sms.call_args.args[1])

    def test_verify_endpoint_returns_customer_access_token(self):
        customer = {"id": 7, "name": "Test", "phone": "998901234567"}
        with (
            patch.object(
                customers,
                "verify_customer_sms_login_otp",
                return_value={"status": "verified", "attempts_remaining": 5},
            ),
            patch.object(customers, "get_customer_by_phone", return_value=customer),
        ):
            result = customers.verify_customer_sms_login_code(
                customers.CustomerSmsLoginVerifyPayload(
                    phone="+998901234567",
                    code="1234",
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["customer"], customer)
        self.assertTrue(result["access_token"])

    def test_send_endpoint_returns_retry_after_when_rate_limited(self):
        with (
            patch.object(customers, "is_textup_configured", return_value=True),
            patch.object(
                customers,
                "get_customer_by_phone",
                return_value={"id": 7, "phone": "998901234567"},
            ),
            patch.object(
                customers,
                "reserve_customer_sms_login_otp",
                return_value={"status": "cooldown", "retry_after": 42},
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                customers.send_customer_sms_login_code(
                    customers.CustomerSmsLoginSendPayload(phone="998901234567")
                )

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers, {"Retry-After": "42"})

    def test_send_endpoint_returns_textup_error_detail(self):
        with (
            patch.object(customers, "is_textup_configured", return_value=True),
            patch.object(
                customers,
                "get_customer_by_phone",
                return_value={"id": 7, "phone": "998901234567"},
            ),
            patch.object(
                customers,
                "reserve_customer_sms_login_otp",
                return_value={"status": "reserved", "expires_in": 120, "retry_after": 60},
            ),
            patch.object(customers, "release_customer_sms_login_otp"),
            patch.object(
                customers,
                "send_sms",
                side_effect=textup.TextUpDeliveryError("TextUp: SMS balance is insufficient"),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                customers.send_customer_sms_login_code(
                    customers.CustomerSmsLoginSendPayload(phone="998901234567")
                )

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(
            raised.exception.detail,
            {"message": "TextUp: SMS balance is insufficient"},
        )


if __name__ == "__main__":
    unittest.main()
