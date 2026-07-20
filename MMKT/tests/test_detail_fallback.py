from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import parse_comparison_detail, parse_pdp_html
import common.pdp_detail as pdp_detail_module
from common.pdp_detail import (
    backfill_missing_pdp_fields,
    detail_attempt_is_rate_limited,
    detail_attempt_is_usable,
    merge_detail,
    needs_pdp_backfill,
)
from ldy import config as ldy_config
from tv import config as tv_config


def comparison_response(product_id: str, features):
    return {
        "data": {
            "comparisonTableRecommendations": {
                "tableData": {
                    "products": [
                        {
                            "productAggregate": {
                                "productId": product_id,
                                "product": {
                                    "id": product_id,
                                    "featureGroupsWithProductId": {
                                        "featureGroups": [{"features": features}]
                                    },
                                },
                            },
                            "cofrProductAggregate": {
                                "productId": product_id,
                                "cofrCoreFeature": {
                                    "reviewStatistics": {
                                        "averageOverallRating": 1.0,
                                        "totalReviewCount": 2,
                                    }
                                },
                            },
                        }
                    ]
                }
            }
        }
    }


def pdp_html(product_id: str, features):
    apollo = {
        f"GraphqlProduct:Media:de-DE:{product_id}": {
            "__typename": "GraphqlProduct",
            "id": product_id,
            "featureGroups": [
                {
                    "features": [
                        {"__ref": f"Feature:{index}"}
                        for index, _ in enumerate(features)
                    ]
                }
            ],
        }
    }
    for index, (name, value) in enumerate(features):
        apollo[f"Feature:{index}"] = {"name": name, "value": value}
    state = json.dumps({"apolloState": apollo}, ensure_ascii=False)
    return f"<script>window.__PRELOADED_STATE__ = {state};</script>"


class DetailFallbackTests(unittest.TestCase):
    def test_ssr_429_retries_same_session_without_stale_error(self):
        class FakeSession:
            def __init__(self):
                self.fetch_calls = 0
                self.reconnect_calls = 0

            def open(self):
                return None

            def fetch_pdp_detail(self, url, sku_id):
                self.fetch_calls += 1
                if self.fetch_calls == 1:
                    comparison = comparison_response(
                        sku_id,
                        [{"name": "Modelkennung", "value": "PARTIAL"}],
                    )
                else:
                    comparison = comparison_response(
                        sku_id,
                        [{"name": "Bildschirmdiagonale (Zoll)", "value": "43"}],
                    )
                return {
                    "html": "",
                    "nav_status": 200,
                    "comparison_resp": comparison,
                    "summary_resp": None,
                    "review_resps": [],
                    "gql_status": {
                        "comparison": 200,
                        "summary": 200,
                        "reviews": [200],
                    },
                    "error": None,
                }

            def fetch_page_response(self, url):
                return {"status": 429, "body": "", "error": None}

            def fetch_page_text(self, url):
                return ""

            def reconnect(self):
                self.reconnect_calls += 1

            def close(self):
                return None

        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "listing.csv"
            output_path = root / "detail.csv"
            with input_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["rank", "sku_id", "product_url"]
                )
                writer.writeheader()
                writer.writerow(
                    {"rank": "1", "sku_id": "123", "product_url": "https://example.test/123"}
                )
            cfg = SimpleNamespace(
                OUTPUT_ROOT=root,
                SPEC_FIELDS=list(tv_config.SPEC_FIELDS),
                PRODUCT="tv",
                extract_pdp_spec=tv_config.extract_pdp_spec,
            )
            args = SimpleNamespace(
                product="tv",
                input=str(input_path),
                bsr=str(root / "missing-bsr.csv"),
                output=str(output_path),
                start=1,
                limit=1,
                sleep=0,
                review_pages=1,
                transport="uc",
                concurrency=1,
                max_retries=1,
                resume=False,
            )
            with (
                patch.object(pdp_detail_module, "parse_args", return_value=args),
                patch.object(pdp_detail_module, "load_cfg", return_value=cfg),
                patch.object(pdp_detail_module, "make_session", return_value=session),
                patch.object(pdp_detail_module.time, "sleep") as sleep_mock,
                patch.object(pdp_detail_module.sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(pdp_detail_module.main(), 0)
            with output_path.open(encoding="utf-8-sig") as fh:
                row = next(csv.DictReader(fh))

        self.assertEqual(session.fetch_calls, 2)
        self.assertEqual(session.reconnect_calls, 0)
        sleep_mock.assert_called_once_with(20)
        self.assertEqual(row["attempts"], "2")
        self.assertEqual(row["screen_size"], "43 inches")
        self.assertEqual(row["fetch_error"], "")

    def test_target_can_match_cofr_product_id_when_aggregate_id_differs(self):
        response = comparison_response(
            "999",
            [{"name": "Bildschirmdiagonale (Zoll)", "value": "43"}],
        )
        product = response["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]
        product["cofrProductAggregate"]["productId"] = "123"
        row = parse_comparison_detail(response, "123", tv_config)
        self.assertEqual(row["screen_size"], "43 inches")

    def test_target_absent_does_not_use_first_alternative(self):
        response = comparison_response(
            "999",
            [
                {"name": "Modelkennung", "value": "WRONG"},
                {"name": "Bildschirmdiagonale (Zoll)", "value": "43"},
            ],
        )
        self.assertIsNone(parse_comparison_detail(response, "123", tv_config))

    def test_duplicate_feature_prefers_later_nonblank_value(self):
        response = comparison_response(
            "123",
            [
                {"name": "Bildschirmdiagonale (Zoll)", "value": None},
                {"name": "Bildschirmdiagonale (Zoll)", "value": "43"},
            ],
        )
        row = parse_comparison_detail(response, "123", tv_config)
        self.assertEqual(row["screen_size"], "43 inches")

    def test_empty_html_returns_neutral_row_without_false_pickup(self):
        row = merge_detail(
            "",
            {
                "comparison_resp": {
                    "data": {
                        "comparisonTableRecommendations": {
                            "tableData": {"products": []}
                        }
                    }
                },
                "review_resps": [],
                "summary_resp": None,
            },
            "123",
            tv_config,
        )
        self.assertEqual(row["sku_id"], "123")
        self.assertNotIn("pick_up_availability", row)
        self.assertIsNone(parse_pdp_html("", "123", tv_config))

    def test_exact_target_ssr_fills_only_blank_sku_and_specs(self):
        html = pdp_html(
            "123",
            [
                ("Modelkennung", "SSR-MODEL"),
                ("Bildschirmdiagonale (Zoll)", "43"),
                ("Leistungsaufnahme in Ein-Zustand (HDR)", "102"),
                ("Modelljahr", "2025"),
            ],
        )
        row = {
            "sku_id": "123",
            "sku": "KEEP-MODEL",
            "screen_size": None,
            "estimated_annual_electricity_use": None,
            "model_year": "KEEP-YEAR",
        }
        valid, recovered = backfill_missing_pdp_fields(row, html, "123", tv_config)
        self.assertTrue(valid)
        self.assertEqual(
            recovered, ["screen_size", "estimated_annual_electricity_use"]
        )
        self.assertEqual(row["sku"], "KEEP-MODEL")
        self.assertEqual(row["screen_size"], "43 inches")
        self.assertEqual(row["estimated_annual_electricity_use"], "102 W")
        self.assertEqual(row["model_year"], "KEEP-YEAR")

    def test_wrong_target_ssr_is_rejected(self):
        html = pdp_html(
            "999",
            [
                ("Modelkennung", "WRONG"),
                ("Bildschirmdiagonale (Zoll)", "43"),
            ],
        )
        row = {"sku_id": "123", "sku": None, "screen_size": None}
        valid, recovered = backfill_missing_pdp_fields(row, html, "123", tv_config)
        self.assertFalse(valid)
        self.assertEqual(recovered, [])
        self.assertIsNone(row["sku"])
        self.assertIsNone(row["screen_size"])

    def test_backfill_runs_only_for_http_200_semantic_gap(self):
        missing = {"screen_size": None}
        complete = {"screen_size": "43 inches"}
        optional_only = {"screen_size": "43 inches", "model_year": None}
        self.assertTrue(needs_pdp_backfill(missing, 200, tv_config))
        self.assertFalse(needs_pdp_backfill(missing, 403, tv_config))
        self.assertFalse(needs_pdp_backfill(complete, 200, tv_config))
        self.assertFalse(needs_pdp_backfill(optional_only, 200, tv_config))

    def test_semantic_empty_200_requires_valid_target_ssr(self):
        self.assertFalse(detail_attempt_is_usable(200, False, True, False))
        self.assertTrue(detail_attempt_is_usable(200, False, True, True))
        self.assertFalse(detail_attempt_is_usable(403, True, False, False))
        self.assertTrue(detail_attempt_is_usable(200, True, False, False))
        self.assertTrue(detail_attempt_is_usable(200, True, True, False))

    def test_rate_limit_includes_required_ssr_fallback(self):
        self.assertTrue(detail_attempt_is_rate_limited(429, False, None))
        self.assertTrue(detail_attempt_is_rate_limited(200, True, 429))
        self.assertFalse(detail_attempt_is_rate_limited(200, True, 403))
        self.assertFalse(detail_attempt_is_rate_limited(200, False, 429))

    def test_ldy_capacity_unit_is_canonical_and_exact_key_only(self):
        for raw in ("11", "11kg", "11 kg", "11 KG"):
            with self.subTest(raw=raw):
                parsed = ldy_config.extract_pdp_spec(
                    {ldy_config.CAPACITY_FEATURE: raw}
                )
                self.assertEqual(parsed["ldy_capacity"], "11kg")
        self.assertIsNone(
            ldy_config.extract_pdp_spec({"Füllmenge": "11"})["ldy_capacity"]
        )


if __name__ == "__main__":
    unittest.main()
