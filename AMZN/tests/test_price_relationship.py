from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from common import email_report, merge_insert, siel_logging


class AmazonPriceRelationshipTests(unittest.TestCase):
    def test_derives_savings_as_original_minus_final(self) -> None:
        row = {
            "final_sku_price": "799,99€",
            "original_sku_price": "1.099,99€",
            "savings": "ignored",
        }

        siel_logging.apply_price_relationship(row)

        self.assertEqual(row["original_sku_price"], "1.099,99€")
        self.assertEqual(row["savings"], "300,00€")
        self.assertNotIn("_original_matches_final", row)

    def test_equal_prices_clear_original_and_savings_and_set_review_flag(self) -> None:
        row = {
            "final_sku_price": "€999.00",
            "original_sku_price": "999,00€",
            "savings": "10,00€",
        }

        siel_logging.apply_price_relationship(row)

        self.assertIsNone(row["original_sku_price"])
        self.assertIsNone(row["savings"])
        self.assertTrue(row["_original_matches_final"])

    def test_email_lists_equal_price_product_url_as_review_required(self) -> None:
        records = [
            {
                "stage": "main",
                "asin": "B0TEST",
                "product_url": "https://www.amazon.de/dp/B0TEST",
                "final_sku_price": "999,00€",
                "original_sku_price": "999,00€",
            },
            {
                "stage": "detail",
                "asin": "B0TEST",
                "product_url": "https://www.amazon.de/dp/B0TEST",
                "sku": "MODEL-1",
            },
        ]
        with patch.object(email_report, "read_jsonl", return_value=records):
            body, severity = email_report.build_email_report_with_severity(
                SimpleNamespace(PRODUCT="TV"), email_report.__file__
            )

        self.assertEqual(severity, "warning")
        self.assertIn("확인 필요: original_sku_price와 final_sku_price가 동일", body)
        self.assertIn("URL=https://www.amazon.de/dp/B0TEST", body)

    def test_make_row_uses_derived_savings(self) -> None:
        row = merge_insert.make_row(
            SimpleNamespace(PRODUCT="TV"),
            {
                "stage": "main",
                "asin": "B0TEST",
                "final_sku_price": "749,00€",
                "original_sku_price": "999,00€",
            },
            None,
            {"stage": "detail", "asin": "B0TEST"},
        )

        self.assertIsNotNone(row)
        self.assertEqual(row["savings"], "250,00€")


if __name__ == "__main__":
    unittest.main()
