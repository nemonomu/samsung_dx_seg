"""Amazon.de discount_type structural extraction and policy regression tests."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import full_output, merge_insert, parsers, selectors
from common.translations import (
    normalize_discount_type,
    translate_field,
    translate_record_fields,
)
from discount_type_smoke_test import (
    PROPOSED_DETAIL_FALLBACK_XPATH,
    PROPOSED_DETAIL_XPATH,
    PROPOSED_MAIN_FALLBACK_XPATH,
    PROPOSED_MAIN_XPATH,
)


AMZN_ROOT = Path(__file__).resolve().parents[1]


class DiscountTypePolicyTests(unittest.TestCase):
    def test_supported_german_and_english_fixed_labels_are_canonicalized(self) -> None:
        for raw, expected in (
            ("Befristetes Angebot", "Limited Time Offer"),
            ("Zeitlich begrenztes Angebot", "Limited Time Offer"),
            ("Limited Time Offer", "Limited Time Offer"),
            ("HOT DEAL", "Hot deal"),
            ("Limited time deal", "Limited time deal"),
            ("Black Friday Big Deal", "Black Friday Big Deal"),
            ("Black Friday deal", "Black Friday Deal"),
            ("Black-Friday-Angebot", "Black Friday Deal"),
            ("Angebot des Tages", "Deal of the Day"),
            ("Blitzangebot", "Lightning Deal"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_discount_type(raw), expected)
                self.assertEqual(translate_field("discount_type", raw), expected)

    def test_safe_new_deal_labels_are_preserved_and_idempotent(self) -> None:
        for raw, expected in (
            ("Cyber Monday Deal", "Cyber Monday Deal"),
            ("Prime Day Deal", "Prime Day Deal"),
            ("  Holiday   Mega Deal  ", "Holiday Mega Deal"),
        ):
            with self.subTest(raw=raw):
                normalized = normalize_discount_type(raw)
                self.assertEqual(normalized, expected)
                self.assertEqual(normalize_discount_type(normalized), expected)

    def test_supported_german_and_english_timers_are_canonicalized(self) -> None:
        for raw, expected in (
            ("Endet in", "Ends in"),
            ("Endet in 13:44:04", "Ends in 13:44:04"),
            ("Angebot endet in 7 Std.", "Ends in 7 Std."),
            ("Ends in 03:21:45", "Ends in 03:21:45"),
            ("Offer Ends in 7h 20m", "Ends in 7h 20m"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_discount_type(raw), expected)

    def test_coupons_popularity_and_other_promotions_are_rejected(self) -> None:
        for raw in (
            None,
            "",
            "Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet",
            "Du zahlst 139,99 € mit Rabattgutschein",
            "You pay ₹16,950 ₹250 off coupon applied",
            "Amazons Tipp",
            "Amazon's Choice",
            "Prime Exklusives Angebot",
            "Top Angebot",
            "Ends tomorrow",
            "Ends inside",
            "Black Friday Week",
            "Black Friday Deal 20% off",
            "Black Friday Deal for 299 €",
            "Black Friday Big Deal NO_OF_HOURS hours NO_OF_MINUTES minutes",
            "Deal " + "x" * 80,
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_discount_type(raw))

    def test_record_translation_applies_the_same_allowlist(self) -> None:
        row = translate_record_fields({"discount_type": "Du zahlst 132,99 € mit Rabattgutschein"})
        self.assertIsNone(row["discount_type"])


class DiscountTypeExtractionTests(unittest.TestCase):
    def test_listing_parser_ignores_amazon_tip_before_a_supported_deal(self) -> None:
        html = """
        <div data-component-type="s-search-result" data-asin="B0TEST0001">
          <h2><a href="/dp/B0TEST0001"><span>Test TV</span></a></h2>
          <span class="a-badge-label">
            <span class="a-badge-text">Amazons</span>
            <span class="a-badge-text">Tipp</span>
          </span>
          <span id="DEAL_B0TEST0001-label">
            <span class="a-badge-text">Befristetes Angebot</span>
          </span>
          <span class="s-coupon-clipped">Du zahlst 100 € Coupon mit 5 % Rabatt angewendet</span>
        </div>
        """
        row = parsers.parse_listing_html(html, page=1, sort="main")[0]
        self.assertEqual(row["discount_type"], "Limited Time Offer")

    def test_listing_parser_collects_a_new_seasonal_deal_only_from_deal_wrapper(self) -> None:
        html = """
        <div data-component-type="s-search-result" data-asin="B0TEST0003">
          <h2><a href="/dp/B0TEST0003"><span>Test TV</span></a></h2>
          <span class="a-badge-label"><span class="a-badge-text">Amazon's Choice</span></span>
          <span id="DEAL_B0TEST0003-label">
            <span class="a-badge-text">Black Friday Big Deal</span>
          </span>
          <span class="s-coupon-clipped">20% Coupon</span>
        </div>
        """
        row = parsers.parse_listing_html(html, page=1, sort="main")[0]
        self.assertEqual(row["discount_type"], "Black Friday Big Deal")

    def test_listing_parser_uses_the_deal_wrapper_fallback_for_safe_new_labels(self) -> None:
        html = """
        <div data-component-type="s-search-result" data-asin="B0TEST0004">
          <h2><a href="/dp/B0TEST0004"><span>Test refrigerator</span></a></h2>
          <span id="DEAL_B0TEST0004-label"><span>Cyber Monday Deal</span></span>
        </div>
        """
        row = parsers.parse_listing_html(html, page=1, sort="main")[0]
        self.assertEqual(row["discount_type"], "Cyber Monday Deal")

    def test_listing_parser_rejects_coupon_inside_a_deal_wrapper(self) -> None:
        html = """
        <div data-component-type="s-search-result" data-asin="B0TEST0002">
          <h2><a href="/dp/B0TEST0002"><span>Test refrigerator</span></a></h2>
          <span id="DEAL_B0TEST0002-label">
            <span><span>Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet</span></span>
          </span>
        </div>
        """
        row = parsers.parse_listing_html(html, page=1, sort="main")[0]
        self.assertIsNone(row["discount_type"])

    def test_detail_parser_extracts_dynamic_german_timer(self) -> None:
        html = """
        <html><body>
          <span id="productTitle">Test refrigerator</span>
          <div id="dealBadge_feature_div">
            <span id="dealBadgeSupportingText">Endet in 13:44:04</span>
          </div>
        </body></html>
        """
        row = parsers.parse_product_detail_html(html, product="REF")
        self.assertEqual(row["discount_type"], "Ends in 13:44:04")

    def test_detail_parser_prefers_the_visible_current_product_badge(self) -> None:
        html = """
        <html><body>
          <div id="apex_desktop">
            <div id="dealBadge_feature_div">
              <span class="aok-hidden">Black Friday Big Deal NO_OF_HOURS hours</span>
              <span id="dealBadgeSupportingText" class="dealBadgeTextColor">
                Black Friday Big Deal
              </span>
            </div>
          </div>
          <div id="sims-discoveryAndInspiration_feature_div_0">
            <span id="dealBadgeSupportingText">Cyber Monday Deal</span>
          </div>
        </body></html>
        """
        row = parsers.parse_product_detail_html(html, product="TV")
        self.assertEqual(row["discount_type"], "Black Friday Big Deal")

    def test_detail_parser_does_not_use_a_recommendation_badge(self) -> None:
        html = """
        <html><body>
          <div id="sims-discoveryAndInspiration_feature_div_0">
            <span id="dealBadgeSupportingText">Cyber Monday Deal</span>
            <span class="a-badge-text">Black Friday Big Deal</span>
          </div>
        </body></html>
        """
        row = parsers.parse_product_detail_html(html, product="TV")
        self.assertIsNone(row["discount_type"])

    def test_detail_parser_uses_visible_class_fallback(self) -> None:
        html = """
        <html><body>
          <div id="dealBadge_feature_div">
            <span class="dealBadgeTextColor">Cyber Monday Deal</span>
          </div>
        </body></html>
        """
        row = parsers.parse_product_detail_html(html, product="TV")
        self.assertEqual(row["discount_type"], "Cyber Monday Deal")

    def test_detail_parser_ignores_hidden_class_fallback(self) -> None:
        html = """
        <html><body>
          <div id="dealBadge_feature_div">
            <span class="dealBadgeTextColor aok-hidden">Cyber Monday Deal</span>
          </div>
        </body></html>
        """
        row = parsers.parse_product_detail_html(html, product="TV")
        self.assertIsNone(row["discount_type"])

    def test_selector_normalization_uses_the_same_policy(self) -> None:
        self.assertEqual(
            selectors.normalize_field("discount_type", "Befristetes Angebot"),
            "Limited Time Offer",
        )
        self.assertEqual(
            selectors.normalize_field("discount_type", "Cyber Monday Deal"),
            "Cyber Monday Deal",
        )
        self.assertIsNone(
            selectors.normalize_field(
                "discount_type",
                "Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet",
            )
        )

    def test_migration_contains_the_live_tested_xpaths(self) -> None:
        sql = (AMZN_ROOT / "sql" / "update_seg_amzn_discount_type_selectors.sql").read_text(
            encoding="utf-8"
        )
        compact_sql = re.sub(r"\s+", "", sql)
        for xpath in (
            PROPOSED_MAIN_XPATH,
            PROPOSED_MAIN_FALLBACK_XPATH,
            PROPOSED_DETAIL_XPATH,
            PROPOSED_DETAIL_FALLBACK_XPATH,
        ):
            with self.subTest(xpath=xpath[:40]):
                self.assertIn(re.sub(r"\s+", "", xpath), compact_sql)
        self.assertNotIn("s-coupon-clipped", compact_sql.split("BEGIN;", 1)[1])
        self.assertIn("fallback_xpath=EXCLUDED.fallback_xpath", compact_sql)
        self.assertNotIn("fallback_xpath=NULL", compact_sql)


class DiscountTypeMergeTests(unittest.TestCase):
    def _make_row(self, product: str, main_value: str | None, detail_value: str | None):
        with patch.object(merge_insert, "_calendar_week", return_value="w38"):
            return merge_insert.make_row(
                SimpleNamespace(PRODUCT=product),
                {"asin": "B0TEST0001", "discount_type": main_value},
                None,
                {"asin": "B0TEST0001", "discount_type": detail_value},
            )

    def test_invalid_main_falls_back_to_valid_detail(self) -> None:
        row = self._make_row(
            "REF",
            "Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet",
            "Endet in 13:44:04",
        )
        self.assertEqual(row["discount_type"], "Ends in 13:44:04")

    def test_valid_main_keeps_priority(self) -> None:
        row = self._make_row("TV", "Befristetes Angebot", "Hot deal")
        self.assertEqual(row["discount_type"], "Limited Time Offer")

    def test_safe_new_main_label_keeps_priority(self) -> None:
        row = self._make_row("TV", "Cyber Monday Deal", "Befristetes Angebot")
        self.assertEqual(row["discount_type"], "Cyber Monday Deal")

    def test_blocked_main_falls_back_to_a_safe_new_detail_label(self) -> None:
        row = self._make_row("REF", "Black Friday Week", "Cyber Monday Deal")
        self.assertEqual(row["discount_type"], "Cyber Monday Deal")

    def test_all_products_reject_coupon_values(self) -> None:
        for product in ("TV", "REF"):
            with self.subTest(product=product):
                row = self._make_row(
                    product,
                    "Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet",
                    None,
                )
                self.assertIsNone(row["discount_type"])

    def test_final_db_serialization_reapplies_the_policy(self) -> None:
        self.assertIsNone(
            merge_insert._db_value(
                "Du zahlst 132,99 € Coupon mit 5 % Rabatt angewendet",
                "discount_type",
            )
        )
        self.assertEqual(
            merge_insert._db_value("Endet in 13:44:04", "discount_type"),
            "Ends in 13:44:04",
        )
        self.assertEqual(
            merge_insert._db_value("Cyber Monday Deal", "discount_type"),
            "Cyber Monday Deal",
        )

    def test_full_output_keeps_a_safe_new_label_after_retranslation(self) -> None:
        written_rows: list[dict[str, object]] = []

        def capture_csv(_path, rows, _fields):
            written_rows.extend(rows)

        with (
            patch.object(full_output, "category_output_root", return_value=Path(".")),
            patch.object(
                full_output,
                "read_csv",
                side_effect=[
                    [{"asin": "B0TEST0001", "main_rank": "1", "discount_type": "Cyber Monday Deal"}],
                    [],
                ],
            ),
            patch.object(
                full_output,
                "run_meta",
                return_value={
                    "crawl_strdatetime": "2026-11-24 00:00:00",
                    "calendar_week": "w48",
                    "batch_id": "a_test",
                },
            ),
            patch.object(full_output, "write_csv", side_effect=capture_csv),
            patch.object(full_output, "write_json"),
        ):
            full_output.run(SimpleNamespace(PRODUCT="TV"))

        self.assertEqual(written_rows[0]["discount_type"], "Cyber Monday Deal")


if __name__ == "__main__":
    unittest.main()
