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
from common.notify import _detail_present, build_report
from common.parsers import parse_product_reviews, review_content
from common.pdp_browser import (
    review_total_pages,
    review_written_count,
    should_fetch_more_review_pages,
)
from common.pdp_detail import (
    _refresh_review_row,
    merge_detail,
    recover_partial_reviews,
    review_row_is_partial,
)
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




def review_page(written: int, *, total_results: int = 652, start: int = 0):
    reviews = []
    for idx in range(10):
        full = f"review text {start + idx}" if idx < written else ""
        reviews.append({"id": f"r{start + idx}", "feedback": {"full": full}})
    return {"data": {"reviews": {"totalResults": total_results, "reviews": reviews}}}

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

    def test_review_content_combines_pros_cons_and_body_without_ui_metadata(self):
        review = {
            "id": "r1",
            "feedback": {
                "positive": "Schönes Design.",
                "negative": "Keine",
                "full": "Ich bin sehr zufrieden.",
            },
            "productVariant": "Ursprünglich erschienen auf Produktvariante: WW5000D",
            "author": "anonym",
        }
        self.assertEqual(
            review_content(review),
            "Vorteile: Schönes Design. | Nachteile: Keine | Inhalt: Ich bin sehr zufrieden.",
        )

    def test_pros_or_cons_only_review_counts_as_written(self):
        page = {
            "data": {
                "reviews": {
                    "totalResults": 1,
                    "reviews": [
                        {"id": "r1", "feedback": {"positive": "Sehr leise", "full": ""}}
                    ],
                }
            }
        }
        self.assertEqual(review_written_count([page]), 1)
        parsed = parse_product_reviews([page])
        self.assertEqual(parsed["detailed_review_content"], "review1 - Vorteile: Sehr leise")

    def test_pagination_stops_at_actual_last_page(self):
        pages = [
            review_page(5, total_results=13, start=0),
            review_page(2, total_results=13, start=10),
        ]
        self.assertEqual(review_total_pages(pages), 2)
        self.assertFalse(
            should_fetch_more_review_pages(pages, fetched_pages=2, max_pages=8)
        )

    def test_hundred_plus_requires_twenty_but_small_exhausted_set_does_not(self):
        large = {
            "count_of_reviews": "100",
            "review_collected_count": "19",
            "review_stop_reason": "actual_last_page",
            "gql_reviews": "200,200,200",
        }
        small = {
            "count_of_reviews": "13",
            "review_collected_count": "7",
            "review_stop_reason": "actual_last_page",
            "gql_reviews": "200,200",
        }
        self.assertTrue(review_row_is_partial(large))
        self.assertFalse(review_row_is_partial(small))

    def test_review_pagination_continues_until_twenty_written_reviews(self):
        pages = [
            review_page(5, start=0),
            review_page(5, start=10),
            review_page(5, start=20),
            review_page(4, start=30),
        ]
        self.assertEqual(review_written_count(pages), 19)
        self.assertTrue(
            should_fetch_more_review_pages(pages, fetched_pages=4, max_pages=8)
        )
        pages.append(review_page(1, start=40))
        self.assertEqual(review_written_count(pages), 20)
        self.assertFalse(
            should_fetch_more_review_pages(pages, fetched_pages=5, max_pages=8)
        )

    def test_review_recovery_retries_only_failed_page(self):
        resps = [
            review_page(5, total_results=397, start=0),
            review_page(5, total_results=397, start=10),
            review_page(5, total_results=397, start=20),
            review_page(2, total_results=397, start=30),
            None,
        ]
        row = {
            "sku_id": "2971010",
            "count_of_reviews": 397,
            "fetch_error": "gql_failed GetProductReviews=429",
            "_review_resps": resps,
            "_review_statuses": [200, 200, 200, 200, 429],
        }
        _refresh_review_row(row, max_pages=8)
        self.assertTrue(review_row_is_partial(row))

        class FakeSession:
            def __init__(self):
                self.pages = []

            def open(self):
                return None

            def fetch_review_page(self, sku_id, page_no):
                self.pages.append((sku_id, page_no))
                return {
                    "status": 200,
                    "data": review_page(3, total_results=397, start=40),
                    "error": None,
                }

            def close(self):
                return None

        session = FakeSession()
        args = SimpleNamespace(
            transport="uc",
            review_max_pages=8,
            review_recovery_max_pages=12,
            review_retry_cooldown=0,
            sleep=0,
        )
        with patch("common.pdp_detail.make_session", return_value=session):
            logs = recover_partial_reviews([row], args)

        self.assertEqual(session.pages, [("2971010", 5)])
        self.assertEqual(row["review_collected_count"], 20)
        self.assertFalse(row["review_partial"])
        self.assertEqual(row["fetch_error"], "")
        self.assertEqual(logs[0]["retried_pages"], [5])

    def test_email_report_includes_unresolved_review_partial(self):
        cfg = SimpleNamespace(
            OUTPUT_ROOT=Path("unused"),
            MAIN_TARGET_UNIQUE=1,
            BSR_TARGET_RANK=1,
            SPEC_FIELDS=["screen_size"],
            PRODUCT="TV",
        )
        rows = [{"main_rank": "1", "bsr_rank": "1", "sku": "MODEL", "screen_size": "55"}]
        step02 = {
            "review_collection": {
                "complete": 0,
                "partial": 1,
                "recovered_after_retry": 2,
            },
            "review_partial_items": [
                {
                    "sku_id": "2971010",
                    "sku": "WW90DG5G34AE",
                    "count_of_reviews": 397,
                    "collected": 17,
                    "failed_page": 5,
                    "stop_reason": "request_failed",
                }
            ],
        }
        with patch("common.notify._read_json", side_effect=[step02, {}, {}]):
            subject, report = build_report(cfg, rows)
        self.assertTrue(subject.startswith("[CHECK]"))
        self.assertIn("partial - 1/1", report)
        self.assertIn("recovered after retry - 2", report)
        self.assertIn("sku_id=2971010", report)
        self.assertIn("review_partial 1/1", report)

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

    def test_db_save_missing_detail_warns_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_path = root / "mmkt_full_output.csv"
            with output_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "batch_id", "item", "sku", "delivery_availability",
                        "pick_up_availability", "screen_size",
                        "estimated_annual_electricity_use", "model_year",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "batch_id": "m_test",
                        "item": "123",
                        "sku": "",
                        "delivery_availability": "",
                        "pick_up_availability": "",
                        "screen_size": "",
                        "estimated_annual_electricity_use": "",
                        "model_year": "",
                    }
                )
            cfg = SimpleNamespace(
                OUTPUT_ROOT=root,
                PRODUCT="TV",
                SPEC_FIELDS=list(tv_config.SPEC_FIELDS),
                DB_TABLE=("dx_seg", "dx_seg_tv_retail_com"),
            )
            db_args = SimpleNamespace(product="tv", input=str(output_path), dry_run=False)
            with (
                patch.object(db_save_module, "parse_args", return_value=db_args),
                patch.object(db_save_module, "load_cfg", return_value=cfg),
                patch.object(db_save_module, "db_config", return_value=None),
                patch("builtins.print"),
            ):
                with self.assertRaisesRegex(RuntimeError, "DB_CONFIG"):
                    db_save_module.main()

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
