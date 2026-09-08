from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lxml import html

AMZN_ROOT = Path(__file__).resolve().parents[1]
if str(AMZN_ROOT) not in sys.path:
    sys.path.insert(0, str(AMZN_ROOT))

from common import full_output, merge_insert, parsers, selectors
from common.translations import translate_record_fields

FIELD = "available_quantity_for_purchase"
RAW = "Nur noch 2 auf Lager (mehr ist unterwegs)."


def card_html(message: str = RAW) -> str:
    # Minimal reproduction of the user's RDP listing card, without forms or tracking data.
    return (
        '<div role="listitem" data-component-type="s-search-result" data-asin="B0D3M3X7KN">'
        '<h2><span>Test product</span></h2>'
        f'<span aria-label="{message}"><span class="a-size-base a-color-price">{message}</span></span>'
        '</div>'
    )


class Element:
    """Read-only Selenium-shaped adapter to test the actual XPath against HTML."""

    def __init__(self, node):
        self.node = node

    @property
    def text(self):
        return self.node.text_content()

    def get_attribute(self, name):
        return self.text if name in {"textContent", "innerText"} else self.node.get(name)

    def find_elements(self, by, xpath):
        return [Element(node) for node in self.node.xpath(xpath)]


class AvailableQuantityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sql = (AMZN_ROOT / "sql" / "enable_seg_main_available_quantity.sql").read_text(encoding="utf-8")
        cls.quantity_selector = {}
        for sql_field, key in (("xpath_primary", "xpath"), ("fallback_xpath", "fallback")):
            match = re.search(sql_field + r" = '((?:''|[^'])*)'", sql)
            cls.quantity_selector[key] = match.group(1).replace("''", "'")

    def test_rdp_xpath_extracts_full_untranslated_suffix(self):
        card = Element(html.fromstring(card_html()))
        self.assertEqual(selectors.extract_single(card, self.quantity_selector), RAW)
        row = selectors.extract_card(card, {FIELD: self.quantity_selector}, sort="main", rank=1)
        self.assertEqual(row[FIELD], RAW)

    def test_fallback_handles_nested_text_without_price_class(self):
        card = Element(html.fromstring(card_html().replace('class="a-size-base a-color-price"', '')))
        self.assertEqual(selectors.extract_single(card, self.quantity_selector), RAW)

    def test_normalization_rejects_generic_stock_and_keeps_original_language(self):
        for raw, expected in (
            (RAW, RAW),
            ("  Nur noch 5\n auf Lager  ", "Nur noch 5 auf Lager"),
            ("Nur noch 11 auf Lager", "Nur noch 11 auf Lager"),
            ("Only 2 left in stock.", "Only 2 left in stock."),
            ("Auf Lager", None), ("In Stock", None),
            ("Derzeit nicht verfügbar", None), ("100+ gekauft Mal im letzten Monat", None),
            ("", None), (None, None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(selectors.normalize_field(FIELD, raw), expected)

    def test_listing_parser_preserves_quantity_and_existing_inventory_behavior(self):
        row = parsers.parse_listing_html(card_html(), page=1, sort="main")[0]
        self.assertEqual(row[FIELD], RAW)
        self.assertEqual(row["inventory_status"], "Only 2 left in stock")
        self.assertEqual(translate_record_fields({FIELD: RAW})[FIELD], RAW)

    def test_absent_quantity_and_bsr_are_null(self):
        for message, sort in (("Auf Lager", "main"), ("", "main"), (RAW, "bsr")):
            with self.subTest(message=message, sort=sort):
                self.assertIsNone(parsers.parse_listing_html(card_html(message), page=1, sort=sort)[0][FIELD])

    def test_db_row_uses_main_only_for_both_products(self):
        for product in ("TV", "REF"):
            for main_value in (RAW, None, "Auf Lager"):
                with self.subTest(product=product, value=main_value):
                    row = merge_insert.make_row(
                        SimpleNamespace(PRODUCT=product),
                        {"asin": "B0D3M3X7KN", FIELD: main_value},
                        {FIELD: "Nur noch 99 auf Lager"},
                        {FIELD: "Nur noch 88 auf Lager", "inventory_status": "Auf Lager"},
                    )
                    self.assertEqual(row[FIELD], RAW if main_value == RAW else None)
                    self.assertEqual(row["inventory_status"], "In Stock")
                    self.assertEqual(merge_insert._db_value(row[FIELD], FIELD), row[FIELD])

    def test_bsr_or_detail_only_never_supply_main_quantity(self):
        for product in ("TV", "REF"):
            with self.subTest(product=product):
                row = merge_insert.make_row(
                    SimpleNamespace(PRODUCT=product), None,
                    {"asin": "B0D3M3X7KN", FIELD: RAW}, {FIELD: RAW},
                )
                self.assertIsNone(row[FIELD])

    def test_full_output_preserves_main_quantity_and_null_without_main(self):
        for product in ("TV", "REF"):
            for rank, value, expected in ((1, RAW, RAW), (1, None, None), (None, RAW, None)):
                with self.subTest(product=product, rank=rank, value=value):
                    with (
                        patch.object(full_output, "category_output_root", return_value=Path("unused")),
                        patch.object(full_output, "read_csv", side_effect=[
                            [{"asin": "B0D3M3X7KN", "main_rank": rank, FIELD: value}],
                            [{"asin": "B0D3M3X7KN", FIELD: RAW, "inventory_status": "Auf Lager"}],
                        ]),
                        patch.object(full_output, "run_meta", return_value={"batch_id": "test"}),
                        patch.object(full_output, "write_csv") as output,
                        patch.object(full_output, "write_json"),
                    ):
                        full_output.run(SimpleNamespace(PRODUCT=product))
                    row = output.call_args.args[1][0]
                    self.assertEqual(row[FIELD], expected)
                    self.assertEqual(row["inventory_status"], "In Stock")
                    self.assertIn(FIELD, output.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
