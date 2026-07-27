import unittest
from unittest.mock import patch

from app.nasiya_bozor import normalize_nasiya_plan
from app.routers.nasiya import get_plans
from app.routers.products import get_credit_tariffs


REAL_PLANS = [
    {
        "id": "three-month",
        "name": "mini tarif",
        "durationMonths": 3,
        "interestRatePct": 120,
        "penaltyRatePct": 0,
        "minPriceMinor": 1_500_000,
        "maxPriceMinor": 20_000_000,
    },
    {
        "id": "six-month",
        "name": "ekonom tarif",
        "durationMonths": 6,
        "interestRatePct": 90,
        "penaltyRatePct": 0,
        "minPriceMinor": 1_500_000,
        "maxPriceMinor": 20_000_000,
    },
    {
        "id": "twelve-month",
        "name": "premium tarif",
        "durationMonths": 12,
        "interestRatePct": 74,
        "penaltyRatePct": 0,
        "minPriceMinor": 1_500_000,
        "maxPriceMinor": 30_000_000,
    },
]


class NormalizeNasiyaPlanTests(unittest.TestCase):
    def test_normalizes_real_annual_rates(self):
        normalized = [normalize_nasiya_plan(plan) for plan in REAL_PLANS]

        self.assertEqual(
            [
                {
                    "months": plan["months"],
                    "annual_percent": plan["annual_percent"],
                    "percent": plan["percent"],
                    "monthly_percent": plan["monthly_percent"],
                }
                for plan in normalized
                if plan is not None
            ],
            [
                {
                    "months": 3,
                    "annual_percent": 120.0,
                    "percent": 30.0,
                    "monthly_percent": 10.0,
                },
                {
                    "months": 6,
                    "annual_percent": 90.0,
                    "percent": 45.0,
                    "monthly_percent": 7.5,
                },
                {
                    "months": 12,
                    "annual_percent": 74.0,
                    "percent": 74.0,
                    "monthly_percent": 6.1667,
                },
            ],
        )

    def test_uses_stable_four_decimal_rounding(self):
        normalized = normalize_nasiya_plan(
            {
                "id": "rounded",
                "durationMonths": 5,
                "interestRatePct": "10.12345",
            }
        )

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["annual_percent"], 10.1235)
        self.assertEqual(normalized["percent"], 4.2181)
        self.assertEqual(normalized["monthly_percent"], 0.8436)

    def test_rejects_invalid_input_without_raising(self):
        invalid_plans = [
            None,
            [],
            {},
            {"id": "missing-months", "interestRatePct": 10},
            {"id": "zero-months", "durationMonths": 0, "interestRatePct": 10},
            {"id": "fractional-months", "durationMonths": 3.5, "interestRatePct": 10},
            {"id": "missing-rate", "durationMonths": 3},
            {"id": "negative-rate", "durationMonths": 3, "interestRatePct": -1},
            {"id": "bad-rate", "durationMonths": 3, "interestRatePct": "not-a-number"},
            {"id": "infinite-rate", "durationMonths": 3, "interestRatePct": "Infinity"},
        ]

        self.assertEqual(
            [normalize_nasiya_plan(plan) for plan in invalid_plans],
            [None] * len(invalid_plans),
        )


class NasiyaPlanEndpointTests(unittest.TestCase):
    @patch("app.routers.nasiya.normalize_nasiya_plan", wraps=normalize_nasiya_plan)
    @patch("app.routers.nasiya.fetch_plans")
    def test_nasiya_endpoint_uses_shared_normalizer(self, fetch_plans_mock, normalize_mock):
        fetch_plans_mock.return_value = {"data": [REAL_PLANS[0], {"invalid": True}]}

        result = get_plans()

        self.assertEqual(result["data"][0]["percent"], 30.0)
        self.assertEqual(result["data"][0]["annual_percent"], 120.0)
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(normalize_mock.call_count, 2)

    @patch("app.routers.products.normalize_nasiya_plan", wraps=normalize_nasiya_plan)
    @patch("app.routers.products.fetch_nasiya_plans")
    def test_products_endpoint_uses_shared_normalizer(
        self,
        fetch_plans_mock,
        normalize_mock,
    ):
        fetch_plans_mock.return_value = {"data": [REAL_PLANS[1], {"invalid": True}]}

        result = get_credit_tariffs()

        self.assertEqual(result["data"][0]["percent"], 45.0)
        self.assertEqual(result["data"][0]["annual_percent"], 90.0)
        self.assertEqual(len(result["data"]), 1)
        self.assertEqual(normalize_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
