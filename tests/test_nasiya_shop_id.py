import unittest
from unittest.mock import patch

from app import config
from app.routers import nasiya, payments


class NasiyaShopIdRemovalTests(unittest.TestCase):
    def test_direct_contract_ignores_legacy_shop_id_and_does_not_forward_it(self):
        payload = nasiya.ContractCreatePayload.model_validate(
            {
                "shopId": "legacy-shop-id",
                "installmentPlanId": "plan-12",
                "clientFullName": "Test User",
                "clientDateOfBirth": "1990-01-01",
                "clientGender": "M",
                "clientPhone": "+998901111111",
                "clientPhone2": "+998902222222",
                "clientPhone3": "+998903333333",
                "clientAddress": "Test address",
                "clientPassportSeries": "AA",
                "clientPassportNumber": "1234567",
                "clientJshshir": "12345678901234",
                "items": [
                    {
                        "productName": "Test product",
                        "quantity": 1,
                        "realUnitPriceMinor": 1_000_000,
                    }
                ],
            }
        )

        self.assertNotIn("shopId", payload.model_dump())

        with (
            patch.object(
                nasiya,
                "create_contract",
                return_value={"data": {"id": "contract-1", "status": "pending"}},
            ) as create_contract_mock,
            patch.object(nasiya, "create_order", return_value={"id": 7}),
        ):
            response = nasiya.submit_contract(payload)

        self.assertEqual(response["contract_id"], "contract-1")
        forwarded_payload = create_contract_mock.call_args.args[0]
        self.assertNotIn("shopId", forwarded_payload)

    def test_checkout_contract_payload_does_not_require_or_send_shop_id(self):
        order = {
            "id": 42,
            "name": "Test User",
            "phone": "+998901111111",
            "products": [
                {
                    "name": "Test product",
                    "quantity": 1,
                    "price": 1_000_000,
                }
            ],
            "myid_profile": {"verified": True},
        }
        common_data = {
            "last_name": "User",
            "first_name": "Test",
            "birth_date": "1990-01-01",
            "gender": "M",
            "pinfl": "12345678901234",
            "inn": "123456789",
        }
        document_data = {"pass_data": "AA1234567"}

        with (
            patch.object(
                payments,
                "_resolve_nasiya_order_plan",
                return_value={"tariff_id": "plan-12"},
            ),
            patch.object(
                payments,
                "_resolve_myid_profile_sections",
                return_value=(common_data, document_data, {}, {}, {}),
            ),
            patch.object(
                payments,
                "_extract_myid_residential_address",
                return_value="Test address",
            ),
            patch.object(
                payments,
                "_parse_myid_date",
                return_value="1990-01-01",
            ),
            patch.object(
                payments,
                "_resolve_initial_payment_amount",
                return_value=100_000,
            ),
        ):
            contract_payload, plan_id, down_payment, total = (
                payments._build_nasiya_contract_for_order(
                    order,
                    extra_phones=["+998902222222", "+998903333333"],
                )
            )

        self.assertNotIn("shopId", contract_payload)
        self.assertEqual(plan_id, "plan-12")
        self.assertEqual(down_payment, 100_000)
        self.assertEqual(total, 1_000_000)

    def test_meta_and_config_no_longer_expose_shop_id(self):
        self.assertFalse(hasattr(config, "NASIYA_BOZOR_SHOP_ID"))
        with patch.object(nasiya, "is_configured", return_value=True):
            self.assertEqual(nasiya.get_meta(), {"enabled": True})


if __name__ == "__main__":
    unittest.main()
