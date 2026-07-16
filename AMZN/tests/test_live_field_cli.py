from __future__ import annotations

import unittest

from test_live_fields import _available_fields, _parse_fields, _selector_subset


class LiveFieldSelectionTests(unittest.TestCase):
    def test_accepts_multiple_numbered_fields_in_display_order(self) -> None:
        available = ("asin", "item", "product_url", "final_sku_price", "sku")

        self.assertEqual(
            _parse_fields("1,4,5", available),
            ("asin", "final_sku_price", "sku"),
        )

    def test_accepts_exact_field_names_and_removes_duplicates(self) -> None:
        available = ("asin", "final_sku_price", "sku")

        self.assertEqual(
            _parse_fields("sku,final_sku_price,sku", available),
            ("sku", "final_sku_price"),
        )

    def test_available_fields_keep_output_names_and_hide_structural_selectors(self) -> None:
        selectors = {
            "base_container": {"xpath": "//card", "fallback": None},
            "expand_item_details": {"xpath": "//button", "fallback": None},
            "sku": {"xpath": "//sku", "fallback": None},
            "final_sku_price": {"xpath": "//price", "fallback": None},
        }

        self.assertEqual(
            _available_fields("detail", selectors),
            ("asin", "item", "product_url", "final_sku_price", "sku"),
        )

    def test_listing_subset_keeps_required_container_and_url_selectors(self) -> None:
        selectors = {
            "base_container": {"xpath": "//card", "fallback": None},
            "product_url": {"xpath": ".//a", "fallback": None},
            "final_sku_price": {"xpath": ".//price", "fallback": None},
            "retailer_sku_name": {"xpath": ".//name", "fallback": None},
        }

        subset = _selector_subset("main", selectors, ("final_sku_price",))

        self.assertEqual(
            set(subset),
            {"base_container", "product_url", "final_sku_price"},
        )

    def test_detail_subset_keeps_expand_selectors_internal(self) -> None:
        selectors = {
            "expand_additional_details": {"xpath": "//button-a", "fallback": None},
            "expand_item_details": {"xpath": "//button-b", "fallback": None},
            "sku": {"xpath": "//sku", "fallback": None},
            "final_sku_price": {"xpath": "//price", "fallback": None},
        }

        subset = _selector_subset("detail", selectors, ("sku",))

        self.assertEqual(
            set(subset),
            {"expand_additional_details", "expand_item_details", "sku"},
        )


if __name__ == "__main__":
    unittest.main()
