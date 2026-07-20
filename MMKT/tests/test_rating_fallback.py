from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

import common.db_save as db_save_module
import common.full_output as full_output_module
from common.full_output import resolve_rating_fields
from common.notify import _detail_present
from common.parsers import parse_product_reviews
from common.pdp_detail import merge_detail
from tv import config as tv_config


def review_response(distribution, *, total_results=67):
    return {
        "data": {
            "reviews": {
                "totalResults": total_results,
                "rating": {"distribution": distribution},
                "reviews": [],
            }
        }
    }


def comparison_response(*, average=None, total=None):
    stats = {}
    if average is not None:
        stats["averageOverallRating"] = average
    if total is not None:
        stats["totalReviewCount"] = total
    return {
        "data": {
            "comparisonTableRecommendations": {
                "tableData": {
                    "products": [
                        {
                            "productAggregate": {
                                "productId": "123",
                                "product": {
                                    "featureGroupsWithProductId": {"featureGroups": []}
                                },
                            },
                            "cofrProductAggregate": {
                                "cofrCoreFeature": {"reviewStatistics": stats}
                            },
                        }
                    ]
                }
            }
        }
    }


CAPTURE_DISTRIBUTION = [
    {"value": 5, "count": 591},
    {"value": 4, "count": 114},
    {"value": 3, "count": 20},
    {"value": 1, "count": 12},
    {"value": 2, "count": 9},
]


class RatingFallbackTests(unittest.TestCase):
    def test_full_output_to_db_dry_run_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing_path = root / "listing.csv"
            detail_path = root / "detail.csv"
            output_path = root / "full.csv"
            with listing_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "sku_id", "rank", "product_url", "retailer_sku_name",
                        "star_rating", "count_of_reviews", "crawl_strdatetime",
                        "calendar_week", "batch_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "sku_id": "123",
                        "rank": "1",
                        "product_url": "https://example.test/123",
                        "retailer_sku_name": "Example TV",
                        "star_rating": "4.5",
                        "count_of_reviews": "52",
                        "crawl_strdatetime": "2026-07-20 12:00:00",
                        "calendar_week": "30",
                        "batch_id": "m_test",
                    }
                )
            with detail_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "sku_id", "sku", "screen_size",
                        "estimated_annual_electricity_use", "model_year",
                        "star_rating", "count_of_star_ratings", "count_of_reviews",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "sku_id": "123",
                        "sku": "MODEL-123",
                        "screen_size": "32 inches",
                        "count_of_reviews": "3",
                    }
                )
            cfg = SimpleNamespace(
                OUTPUT_ROOT=root,
                PRODUCT="TV",
                SPEC_FIELDS=list(tv_config.SPEC_FIELDS),
                DB_TABLE=("dx_seg", "dx_seg_tv_retail_com"),
            )
            full_args = SimpleNamespace(
                product="tv",
                listing=str(listing_path),
                bsr=str(root / "missing-bsr.csv"),
                detail=str(detail_path),
                output=str(output_path),
            )
            with (
                patch.object(full_output_module, "parse_args", return_value=full_args),
                patch.object(full_output_module, "load_cfg", return_value=cfg),
                patch.object(full_output_module.sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(full_output_module.main(), 0)
            with output_path.open(encoding="utf-8-sig") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(row["star_rating"], "4.5")
            self.assertEqual(row["count_of_star_ratings"], "52")
            self.assertEqual(row["count_of_reviews"], "3")
            self.assertEqual(row["screen_size"], "32 inches")

            db_args = SimpleNamespace(product="tv", input=str(output_path), dry_run=True)
            with (
                patch.object(db_save_module, "parse_args", return_value=db_args),
                patch.object(db_save_module, "load_cfg", return_value=cfg),
                patch.object(db_save_module, "write_json") as write_json_mock,
                patch("builtins.print"),
            ):
                self.assertEqual(db_save_module.main(), 0)
            manifest = write_json_mock.call_args.args[1]
            self.assertTrue(manifest["success"])
            self.assertTrue(manifest["skipped"])
            self.assertEqual(manifest["csv_rows"], 1)
            self.assertEqual(manifest["batch_ids"], ["m_test"])

    def test_saved_capture_distribution_reproduces_average_and_counts(self):
        parsed = parse_product_reviews(
            review_response(CAPTURE_DISTRIBUTION, total_results=67)
        )
        self.assertEqual(parsed["star_rating"], 4.7)
        self.assertEqual(parsed["count_of_star_ratings"], 746)
        self.assertEqual(parsed["count_of_reviews"], 67)

    def test_comparison_average_wins_over_review_distribution(self):
        row = merge_detail(
            "",
            {
                "comparison_resp": comparison_response(average=4.8, total=10),
                "review_resps": [review_response(CAPTURE_DISTRIBUTION)],
                "summary_resp": None,
            },
            "123",
            tv_config,
        )
        self.assertEqual(row["star_rating"], 4.8)
        self.assertEqual(row["count_of_star_ratings"], 746)

    def test_review_distribution_fills_missing_comparison_average(self):
        row = merge_detail(
            "",
            {
                "comparison_resp": comparison_response(),
                "review_resps": [review_response(CAPTURE_DISTRIBUTION)],
                "summary_resp": None,
            },
            "123",
            tv_config,
        )
        self.assertEqual(row["star_rating"], 4.7)

    def test_missing_distribution_preserves_comparison_rating_count(self):
        row = merge_detail(
            "",
            {
                "comparison_resp": comparison_response(average=4.8, total=746),
                "review_resps": [review_response([], total_results=67)],
                "summary_resp": None,
            },
            "123",
            tv_config,
        )
        self.assertEqual(row["count_of_star_ratings"], 746)
        self.assertEqual(row["count_of_reviews"], 67)

    def test_zero_and_malformed_distribution_are_not_fabricated(self):
        zero = parse_product_reviews(
            review_response([{"value": n, "count": 0} for n in range(1, 6)], total_results=0)
        )
        bad = parse_product_reviews(
            review_response([{"value": 6, "count": 3}], total_results=None)
        )
        self.assertIsNone(zero["star_rating"])
        self.assertEqual(zero["count_of_star_ratings"], 0)
        self.assertIsNone(bad["star_rating"])
        self.assertIsNone(bad["count_of_star_ratings"])

    def test_full_output_priority_and_count_semantics(self):
        self.assertEqual(
            resolve_rating_fields(
                {"star_rating": "4.9", "count_of_star_ratings": "20", "count_of_reviews": "3"},
                {"star_rating": "4.5", "count_of_reviews": "10"},
                None,
            ),
            ("4.9", "20", "3"),
        )
        self.assertEqual(
            resolve_rating_fields(
                {},
                {"star_rating": "", "count_of_reviews": ""},
                {"star_rating": "4.2", "count_of_reviews": "8"},
            ),
            ("4.2", "8", 0),
        )
        self.assertEqual(resolve_rating_fields({}, None, None), ("0.0", 0, 0))

    def test_listing_rating_does_not_fake_detail_health(self):
        self.assertFalse(
            _detail_present(
                {"star_rating": "4.5", "count_of_star_ratings": "52"},
                list(tv_config.SPEC_FIELDS),
            )
        )
        self.assertTrue(
            _detail_present({"sku": "MODEL-1"}, list(tv_config.SPEC_FIELDS))
        )


if __name__ == "__main__":
    unittest.main()
