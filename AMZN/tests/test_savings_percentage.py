"""Offline PDP percentage collection and CSV/DB propagation contracts."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import db_save, full_output, merge_insert, parsers, selectors, siel_logging


def pdp(value: str | None, *, block: str = "corePriceDisplay_desktop_feature_div") -> str:
    badge = (f'<span aria-hidden="true" class="a-color-price savingsPercentage '
             f'apex-savings-percentage">{value}</span>') if value is not None else ""
    return (
        '<html><body><span id="productTitle">Test appliance A-10%</span>'
        f'<div id="{block}"><span class="aok-offscreen">'
        '199,00 € mit 31 Prozent Einsparungen</span>' + badge +
        '<span class="priceToPay">199,00 €</span>'
        '<span class="basisPrice">UVP: 289,00 €</span></div>'
        '<div id="sp_detail"><span class="savingsPercentage">-90%</span></div>'
        '<div id="coupon">Spare 10%</div></body></html>'
    )


class Driver:
    def __init__(self, html: str):
        self.page_source = html

    def find_elements(self, *_args):
        raise AssertionError("Savings must not use the supplied unscoped DB XPath")


class SavingsPercentageTests(unittest.TestCase):
    def test_ref_and_ldy_samples_and_tv_use_the_displayed_percentage(self):
        for product, raw, expected in (
            ("REF", "-31&nbsp;%", "-31%"),
            ("LDY", "-15 %", "-15%"),
            ("TV", "-20\u202f%", "-20%"),
        ):
            with self.subTest(product=product):
                self.assertEqual(parsers.parse_product_detail_html(pdp(raw), product=product)["savings"], expected)
                self.assertEqual(selectors.extract_detail(Driver(pdp(raw)), {}, product=product)["savings"], expected)

    def test_absent_badge_ignores_price_difference_energy_label_and_recommendations(self):
        self.assertIsNone(parsers.parse_product_detail_html(pdp(None))["savings"])
        self.assertIsNone(selectors.extract_detail(
            Driver(pdp(None)), {"savings": {"xpath": "//span[contains(@class,'savingsPercentage')]"}}
        )["savings"])

    def test_explicit_badge_overrides_stale_db_selector(self):
        result = selectors.extract_detail(
            Driver(pdp("-15%")), {"savings": {"xpath": "//div[@id='coupon']"}}
        )
        self.assertEqual(result["savings"], "-15%")

    def test_no_html_means_null(self):
        self.assertIsNone(selectors.extract_detail(Driver(""), {})["savings"])

    def test_hidden_price_blocks_and_badges_are_not_collected(self):
        for attr in ('hidden', 'class="aok-hidden"', 'style="display: none"', 'style="visibility:hidden"'):
            with self.subTest(attr=attr):
                html = f'<div {attr}>' + pdp("-31%") + '</div>'
                # Keep a well-formed outer document for BeautifulSoup/lxml.
                html = html.replace('<html><body>', '').replace('</body></html>', '')
                self.assertIsNone(parsers.parse_product_detail_html(html)["savings"])

    def test_supported_price_containers(self):
        for block in ("corePriceDisplay_desktop_feature_div", "corePrice_desktop", "corePrice_feature_div"):
            with self.subTest(block=block):
                self.assertEqual(parsers.parse_product_detail_html(pdp("-15%", block=block))["savings"], "-15%")

    def test_only_explicit_negative_percentages_are_accepted(self):
        for raw in (None, "", "NULL", "90,00€", 31, "31", "31%", "+31%", "-101%", "save -31%", "199€ mit 31 Prozent"):
            with self.subTest(raw=raw):
                self.assertIsNone(parsers.normalize_savings_percentage(raw))
                self.assertIsNone(merge_insert._db_value(raw, "savings"))
                self.assertIsNone(db_save._empty_to_none(raw, "savings"))
        for raw, expected in (("-31&nbsp;%", "-31%"), ("\u221215\u00a0%", "-15%"), ("-12,5 %", "-12,5%")):
            with self.subTest(raw=raw):
                self.assertEqual(parsers.normalize_savings_percentage(raw), expected)
                self.assertEqual(merge_insert._db_value(raw, "savings"), expected)
                self.assertEqual(db_save._empty_to_none(raw, "savings"), expected)

    def test_displayed_savings_survives_missing_equal_and_reversed_prices(self):
        for final, original in ((None, None), ("199,00€", None), ("199,00€", "199,00€"), ("299,00€", "199,00€")):
            with self.subTest(final=final, original=original):
                row = {"final_sku_price": final, "original_sku_price": original, "savings": "-15 %"}
                siel_logging.apply_price_relationship(row)
                self.assertEqual(row["savings"], "-15%")
                if final == original and final is not None:
                    self.assertTrue(row["_original_matches_final"])
                    self.assertIsNone(row["original_sku_price"])

    def test_jsonl_uses_detail_only_and_never_backfills_absent_savings(self):
        for main_stage in ("main", "bsr"):
            for detail_savings in ("-31%", None, "", "90,00€"):
                with self.subTest(stage=main_stage, savings=detail_savings):
                    listing = {"asin": "B0TEST1234", "stage": main_stage, "savings": "-90%",
                               "final_sku_price": "199,00€", "original_sku_price": "289,00€"}
                    detail = {"asin": "B0TEST1234", "stage": "detail", "savings": detail_savings}
                    with patch.object(merge_insert, "read_jsonl", return_value=[listing, detail]):
                        rows = merge_insert.merge_jsonl(SimpleNamespace(PRODUCT="REF"), "unused.jsonl")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["savings"], "-31%" if detail_savings == "-31%" else None)

    def test_listing_only_redirect_does_not_use_another_products_savings(self):
        row = merge_insert.make_row(
            SimpleNamespace(PRODUCT="TV"),
            {"asin": "B0TEST1234", "savings": "-90%"}, None,
            {"asin": "B0TEST1234", "redirect": True, "_detail_skip": "asin_mismatch", "savings": "-31%"},
        )
        self.assertIsNone(row["savings"])

    def test_full_output_uses_detail_only_including_null(self):
        for raw, expected in (("-31 %", "-31%"), (None, None), ("", None), ("90,00€", None)):
            with self.subTest(raw=raw):
                target = {"asin": "B0TEST1234", "savings": "-90%", "final_sku_price": "199,00€", "original_sku_price": "289,00€"}
                detail = {"asin": "B0TEST1234", "savings": raw}
                with (
                    patch.object(full_output, "category_output_root", return_value=Path("unused")),
                    patch.object(full_output, "run_meta", return_value={"batch_id": "test"}),
                    patch.object(full_output, "read_csv", side_effect=[[target], [detail]]),
                    patch.object(full_output, "write_csv") as write,
                    patch.object(full_output, "write_json"),
                ):
                    full_output.run(SimpleNamespace(PRODUCT="REF"))
                row = write.call_args.args[1][0]
                self.assertEqual(row["savings"], expected)
                self.assertEqual(db_save._empty_to_none(row["savings"], "savings"), expected)

    def test_batch_preview_and_db_boundary_reject_legacy_amounts(self):
        rows = [{"savings": "90,00€"}, {"savings": "-31 %"}, {"savings": None}]
        with (
            patch.object(merge_insert, "category_output_root", return_value=Path("unused")),
            patch.object(merge_insert, "write_csv") as write,
            patch.object(merge_insert, "write_json"),
            patch.object(merge_insert, "_connect") as connect,
        ):
            merge_insert.insert_rows(SimpleNamespace(PRODUCT="TV", DB_TABLE="test.rows"), rows, dry_run=True)
        connect.assert_not_called()
        preview = write.call_args.args[1]
        self.assertEqual([row["savings"] for row in preview], [None, "-31%", None])
        self.assertEqual([merge_insert._db_value(row["savings"], "savings") for row in rows], [None, "-31%", None])


if __name__ == "__main__":
    unittest.main()
