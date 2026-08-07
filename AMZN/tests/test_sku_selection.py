from __future__ import annotations

import unittest

from common.parsers import parse_product_detail_html
from common.selectors import extract_detail


def _detail_table(*rows: tuple[str, str]) -> str:
    body = "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
    return f"<html><body><table class='prodDetTable'><tbody>{body}</tbody></table></body></html>"


class SkuSelectionTests(unittest.TestCase):
    def test_unavailable_pdp_uses_visible_german_text_as_final_price(self) -> None:
        html = (
            "<html><body><div id='outOfStock'><div class='a-box-inner'>"
            "<span class='a-color-base a-text-bold'>Derzeit nicht verfügbar.</span>"
            "</div></div></body></html>"
        )

        self.assertEqual(
            parse_product_detail_html(html, product="TV")["final_sku_price"],
            "Derzeit nicht verfügbar.",
        )

    def test_current_product_unavailable_text_outranks_other_page_prices(self) -> None:
        html = (
            "<html><body><span class='a-price'><span class='a-offscreen'>499,00€</span></span>"
            "<div id='outOfStock'><span class='a-color-base a-text-bold'>"
            "Derzeit nicht verfügbar.</span></div></body></html>"
        )

        self.assertEqual(
            parse_product_detail_html(html, product="TV")["final_sku_price"],
            "Derzeit nicht verfügbar.",
        )

    def test_html_unavailable_fallback_fills_missed_db_price_selector(self) -> None:
        html = (
            "<html><body><div id='outOfStock'>"
            "<span class='a-color-base a-text-bold'>Derzeit nicht verfügbar.</span>"
            "</div></body></html>"
        )

        class Driver:
            page_source = html

            @staticmethod
            def find_elements(_by: str, _xpath: str) -> list[object]:
                return []

        result = extract_detail(
            Driver(),
            {"final_sku_price": {"xpath": "//missing-price", "fallback": None}},
            product="TV",
        )

        self.assertEqual(result["final_sku_price"], "Derzeit nicht verfügbar.")

    def test_tv_prefers_manufacturer_model_number(self) -> None:
        html = _detail_table(
            ("Hersteller-Teilenummer", "50468986159444"),
            ("Hersteller-Modellnummer", "Samsung UE65DU8070"),
            ("Modellname", "DU8070"),
        )

        self.assertEqual(
            parse_product_detail_html(html, product="TV")["sku"],
            "Samsung UE65DU8070",
        )

    def test_ref_prefers_model_number(self) -> None:
        html = _detail_table(
            ("Hersteller-Modellnummer", "SECONDARY"),
            ("Modellnummer", "740846"),
            ("Modellname", "R 619 EES5"),
        )

        self.assertEqual(parse_product_detail_html(html, product="REF")["sku"], "740846")

    def test_uses_sku_number_when_higher_priority_fields_are_missing(self) -> None:
        html = _detail_table(("SKU Number", "SKU-ONLY"))

        self.assertEqual(parse_product_detail_html(html, product="TV")["sku"], "SKU-ONLY")

    def test_bundle_value_falls_back_to_model_name(self) -> None:
        html = _detail_table(
            ("Hersteller-Modellnummer", "BNDL_123"),
            ("Modellname", "MODEL-FROM-NAME"),
        )

        self.assertEqual(
            parse_product_detail_html(html, product="TV")["sku"],
            "MODEL-FROM-NAME",
        )

    def test_structured_priority_overrides_broad_db_selector_value(self) -> None:
        html = _detail_table(
            ("Hersteller-Teilenummer", "50468986159444"),
            ("Hersteller-Modellnummer", "Samsung UE65DU8070"),
        )

        class Element:
            text = "50468986159444"

            @staticmethod
            def get_attribute(_name: str) -> None:
                return None

        class Driver:
            page_source = html

            @staticmethod
            def find_elements(_by: str, xpath: str) -> list[Element]:
                return [Element()] if xpath == "db-sku" else []

        result = extract_detail(
            Driver(),
            {"sku": {"xpath": "db-sku", "fallback": None}},
            product="TV",
        )

        self.assertEqual(result["sku"], "Samsung UE65DU8070")


if __name__ == "__main__":
    unittest.main()
