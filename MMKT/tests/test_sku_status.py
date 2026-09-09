from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import (
    extract_rendered_listing_rows,
    extract_sponsored_diagnostics,
    parse_listing_html,
)


def listing_html(*, first_ad_data=None, rendered_sponsored: bool = False) -> str:
    state = {
        "apolloState": {
            "ProductListPage:test": {
                "__typename": "ProductListPage",
                "products": [
                    {"productId": "Media:de:111", "adData": first_ad_data},
                    {"productId": "Media:de:222", "adData": None},
                ],
            },
            "GraphqlProduct:111": {
                "__typename": "GraphqlProduct",
                "id": "Media:de:111",
                "title": "Sponsored TV",
                "url": "/de/product/_sponsored-tv-111.html",
            },
            "GraphqlProduct:222": {
                "__typename": "GraphqlProduct",
                "id": "Media:de:222",
                "title": "Organic TV",
                "url": "/de/product/_organic-tv-222.html",
            },
            "CofrBadgesFeature:111": {
                "__typename": "CofrBadgesFeature",
                "id": "Media:de:111",
                "computedBadges": [{"name": "Preisheld"}],
            },
        }
    }
    label = "<span>Gesponsert</span>" if rendered_sponsored else ""
    return (
        "<html><body>"
        f"<script>window.__PRELOADED_STATE__ = {json.dumps(state)};</script>"
        '<div id="mms-search-productlist"><ul role="list">'
        f'<li><article>{label}<a data-test="mms-router-link-product-list-item-link" '
        'href="/de/product/_sponsored-tv-111.html">Sponsored TV</a></article></li>'
        '<li><article><a data-test="mms-router-link-product-list-item-link" '
        'href="/de/product/_organic-tv-222.html">Organic TV</a></article></li>'
        '</ul></div>'
        "</body></html>"
    )


class SkuStatusTests(unittest.TestCase):
    def test_rendered_gesponsert_recovers_null_ad_data(self):
        diagnostics = {}
        rows = parse_listing_html(
            listing_html(first_ad_data=None, rendered_sponsored=True),
            diagnostics=diagnostics,
        )

        self.assertEqual(rows[0]["sku_status"], "Sponsored")
        self.assertEqual(rows[0]["discount_type"], "Preisheld")
        self.assertIsNone(rows[1]["sku_status"])
        self.assertEqual(diagnostics["raw_gesponsert_occurrences"], 1)
        self.assertEqual(diagnostics["visible_label_occurrences"], 1)
        self.assertEqual(diagnostics["sponsored_product_ids"], ["111"])
        self.assertEqual(diagnostics["state_listing_rows"], 2)
        self.assertEqual(diagnostics["parsed_sponsored_rows"], 1)
        self.assertEqual(diagnostics["unmatched_sponsored_ids"], [])

    def test_legacy_nonempty_ad_data_remains_supported(self):
        rows = parse_listing_html(
            listing_html(first_ad_data={"campaign": "legacy"})
        )

        self.assertEqual(rows[0]["sku_status"], "Sponsored")
        self.assertIsNone(rows[1]["sku_status"])

    def test_script_occurrence_is_not_treated_as_visible_label(self):
        diagnostics = extract_sponsored_diagnostics(
            "<html><script>const label = 'Gesponsert';</script></html>"
        )

        self.assertEqual(diagnostics["raw_gesponsert_occurrences"], 1)
        self.assertEqual(diagnostics["visible_label_occurrences"], 0)
        self.assertEqual(diagnostics["sponsored_product_ids"], [])

    def test_equal_original_and_final_price_is_collected(self):
        html = (
            '<div id="mms-search-productlist"><ul role="list"><li>'
            '<article data-test="mms-sba-product-tile">'
            '<div data-test="mms-plp-sponsored">Gesponsert</div>'
            '<a href="/de/product/_equal-price-ad-444.html">Equal Price Ad</a>'
            '<div data-test="cofr-price mms-price">'
            '<div data-test="mms-strike-price-type-lop">449,– €</div>'
            '<span>449,00€</span></div></article></li></ul></div>'
        )

        rows, diagnostics = extract_rendered_listing_rows(html)

        self.assertEqual(rows[0]["original_sku_price"], "449,– €")
        self.assertEqual(rows[0]["final_sku_price"], "449,– €")
        self.assertEqual(rows[0]["sku_status"], "Sponsored")
        self.assertEqual(diagnostics["mapped_label_occurrences"], 1)

    def test_lazy_sponsored_card_is_inserted_and_outside_carousel_is_excluded(self):
        html = listing_html(first_ad_data=None)
        inserted = (
            '<aside><span>Gesponsert</span>'
            '<a href="/de/product/_outside-ad-999.html">Outside ad</a></aside>'
            '<div id="mms-search-productlist"><ul role="list">'
            '<li><aside><span>Gesponsert</span></aside></li>'
            '<li><article data-test="mms-product-card">'
            '<div data-test="mms-plp-sponsored">Gesponsert</div>'
            '<h3 data-test="product-title">Dynamic Sponsored TV</h3>'
            '<a data-test="mms-router-link-product-list-item-link" '
            'href="/de/product/_dynamic-sponsored-tv-333.html">Dynamic Sponsored TV</a>'
            '<div data-test="mms-price"><div data-test="mms-strike-price-type-rrp">'
            'UVP 499,00 €</div><span>199,99 €</span><span>-59%</span></div>'
            '</article></li>'
            '<li><article><a href="/de/product/_organic-tv-222.html">Organic TV</a>'
            '</article></li></ul></div>'
        )
        html = re.sub(
            r'<div id="mms-search-productlist">.*?</div>\s*</body>',
            inserted + "</body>",
            html,
            flags=re.S,
        )
        diagnostics = {}

        rows = parse_listing_html(html, diagnostics=diagnostics)

        self.assertEqual([row["sku_id"] for row in rows], ["333", "222", "111"])
        self.assertEqual(rows[0]["sku_status"], "Sponsored")
        self.assertEqual(rows[0]["retailer_sku_name"], "Dynamic Sponsored TV")
        self.assertEqual(rows[0]["listing_card_type"], "standard")
        self.assertEqual(rows[0]["final_sku_price"], "199,99 €")
        self.assertEqual(rows[0]["original_sku_price"], "499,– €")
        self.assertEqual(rows[0]["savings"], "-59%")
        self.assertNotIn("999", diagnostics["sponsored_product_ids"])
        self.assertEqual(diagnostics["sponsored_product_ids"], ["333"])
        self.assertEqual(diagnostics["visible_label_occurrences"], 2)
        self.assertEqual(diagnostics["mapped_label_occurrences"], 1)
        self.assertEqual(diagnostics["unmapped_label_occurrences"], 1)
        self.assertEqual(diagnostics["outside_product_list_label_occurrences"], 1)


if __name__ == "__main__":
    unittest.main()
