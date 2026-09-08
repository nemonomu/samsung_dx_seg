from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import extract_sponsored_diagnostics, parse_listing_html


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
        f'<article>{label}<a href="/de/product/_sponsored-tv-111.html">Sponsored TV</a></article>'
        '<article><a href="/de/product/_organic-tv-222.html">Organic TV</a></article>'
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


if __name__ == "__main__":
    unittest.main()
