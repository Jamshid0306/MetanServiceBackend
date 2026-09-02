import unittest
from unittest.mock import patch

from app.routers import nasiya, payments


class NasiyaContractRequirementsTests(unittest.TestCase):
    def test_direct_contract_forwards_configured_shop_id_and_image_paths(self):
        payload = nasiya.ContractCreatePayload.model_validate(
            {
                "shopId": "untrusted-client-shop-id",
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
                "clientImagePath": "customer/image.jpg",
                "productImagePath": "product/image.jpg",
                "items": [
                    {
                        "productName": "Test product",
                        "quantity": 1,
                        "realUnitPriceMinor": 1_000_000,
                    }
                ],
            }
        )

        with (
            patch.object(nasiya, "NASIYA_BOZOR_SHOP_ID", "configured-shop-id"),
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
        self.assertEqual(forwarded_payload["shopId"], "configured-shop-id")
        self.assertEqual(forwarded_payload["clientImagePath"], "customer/image.jpg")
        self.assertEqual(forwarded_payload["productImagePath"], "product/image.jpg")

    def test_checkout_contract_sends_provider_required_references(self):
        order = {
            "id": 42,
            "name": "Test User",
            "phone": "+998901111111",
            "myid_job_id": "myid-job-42",
            "products": [
                {
                    "name": "Test product",
                    "quantity": 1,
                    "price": 1_000_000,
                    "images": ["/static/images/product.jpg"],
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
            patch.object(payments, "NASIYA_BOZOR_SHOP_ID", "configured-shop-id"),
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

        self.assertEqual(contract_payload["shopId"], "configured-shop-id")
        self.assertEqual(contract_payload["clientImagePath"], "myid-job:myid-job-42")
        self.assertEqual(contract_payload["productImagePath"], "/static/images/product.jpg")
        self.assertEqual(plan_id, "plan-12")
        self.assertEqual(down_payment, 100_000)
        self.assertEqual(total, 1_000_000)

    def test_meta_reports_shop_configuration(self):
        with (
            patch.object(nasiya, "NASIYA_BOZOR_SHOP_ID", "configured-shop-id"),
            patch.object(nasiya, "is_configured", return_value=True),
        ):
            self.assertEqual(
                nasiya.get_meta(),
                {"enabled": True, "shop_id_configured": True},
            )


if __name__ == "__main__":
    unittest.main()
