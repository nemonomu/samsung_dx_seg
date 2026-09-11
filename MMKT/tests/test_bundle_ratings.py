"""Offline regression cases: bundle ratings must belong to the bundle SKU."""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.full_output import resolve_rating_fields
from common.parsers import IS_BUNDLE, parse_comparison_detail, parse_pdp_html
import common.pdp_detail as pdp_detail_module
from common.pdp_detail import (
    _merge_missing_values,
    _refresh_review_row,
    backfill_missing_pdp_fields,
    csv_columns,
    merge_detail,
    recover_partial_reviews,
    review_row_is_partial,
)


CFG = SimpleNamespace(SPEC_FIELDS=["ldy_capacity"], extract_pdp_spec=lambda features, name: {})
TITLE = "HISENSE Waschmaschine und Wärmepumpentrockner Bundle"
FIELDS = ("star_rating", "count_of_star_ratings", "count_of_reviews")


def distribution(count=0):
    return [{"value": n, "count": count if n == 5 else 0} for n in range(1, 6)]


def review_response(*, dist=None, total=0):
    return {"data": {"reviews": {
        "totalResults": total, "reviews": [], "rating": {"distribution": dist},
    }}}


def comparison_response(sku_id="123", title=TITLE):
    return {"data": {"comparisonTableRecommendations": {"tableData": {"products": [{
        "productAggregate": {"productId": sku_id, "product": {"title": title}},
        "cofrProductAggregate": {"productId": sku_id, "cofrCoreFeature": {
            "reviewStatistics": {"averageOverallRating": 4.7, "totalReviewCount": 601},
        }},
    }]}}}}


def detail_response(page, *, status=200, title=TITLE, sku_id="123"):
    return {
        "comparison_resp": comparison_response(sku_id, title),
        "review_resps": [page],
        "summary_resp": None,
        "gql_status": {"reviews": [status]},
    }


def ssr_html(title=TITLE):
    apollo = {
        "target": {"__typename": "GraphqlProduct", "id": "123", "title": title},
        "component": {"__typename": "GraphqlProduct", "id": "999", "title": "Waschmaschine"},
        "component-review": {
            "__typename": "GraphqlReview", "id": "r1", "rating": 5,
            "text": "Component review, not a bundle review",
        },
        "stats": {"__typename": "CofrCoreFeature", "id": "Media:de:123",
                  "reviewStatistics": {"averageOverallRating": 4.7, "totalReviewCount": 601}},
    }
    state = json.dumps({"apolloState": apollo})
    return (
        f"<script>window.__PRELOADED_STATE__ = {state};</script>"
        '<script type="application/ld+json">'
        '{"@type":"Product","sku":"999","aggregateRating":'
        '{"@type":"AggregateRating","ratingValue":"4.7","ratingCount":"601"}}'
        '</script>'
    )


class BundleRatingTests(unittest.TestCase):
    def merge(self, page, **kwargs):
        return merge_detail("", detail_response(page, **kwargs), kwargs.get("sku_id", "123"), CFG)

    def test_all_reported_bundle_ids_use_empty_own_reviews(self):
        for sku_id in ("3066915", "3066916", "3066917", "3066918", "3066919", "3068893", "3068894", "3068895"):
            for dist in (None, [], distribution()):
                with self.subTest(sku_id=sku_id, dist=dist):
                    row = self.merge(review_response(dist=dist), sku_id=sku_id)
                    self.assertEqual(tuple(row[f] for f in FIELDS), (0.0, 0, 0))
                    self.assertFalse(review_row_is_partial(row))
                    self.assertEqual(resolve_rating_fields(row, {"star_rating": "4.7", "count_of_reviews": "601"}, None), (0.0, 0, 0))

    def test_rated_bundle_uses_own_distribution_not_comparison_average(self):
        row = self.merge(review_response(dist=distribution(2), total=2))
        self.assertEqual(tuple(row[f] for f in FIELDS), (5.0, 2, 2))

    def test_zero_reviews_does_not_erase_positive_rating_distribution(self):
        row = self.merge(review_response(dist=distribution(2), total=0))
        self.assertEqual(tuple(row[f] for f in FIELDS), (5.0, 2, 0))

    def test_missing_and_failed_responses_remain_unknown(self):
        error_page = review_response()
        error_page["errors"] = [{"message": "review service unavailable"}]
        cases = [(None, 200), ({"data": None}, 200), ({"data": {"reviews": None}}, 200), ({}, 200),
                 (review_response(), 403), (error_page, 200)]
        for page, status in cases:
            with self.subTest(page=page, status=status):
                row = self.merge(page, status=status)
                self.assertEqual(tuple(row[f] for f in FIELDS), (None, None, None))
                self.assertTrue(review_row_is_partial(row))
                self.assertEqual(resolve_rating_fields(row, {"star_rating": "4.7", "count_of_reviews": "601"}, None), ("", "", ""))

    def test_total_zero_alone_is_not_evidence_of_zero_ratings(self):
        row = self.merge({"data": {"reviews": {"totalResults": 0, "reviews": []}}})
        self.assertEqual(tuple(row[f] for f in FIELDS), (None, None, 0))
        self.assertTrue(review_row_is_partial(row))

    def test_malformed_distribution_does_not_become_zero(self):
        for dist in ([{"value": 1, "count": 0}], [{"value": 5, "count": 0}] * 5,
                     [{"value": 6, "count": 0}], "broken"):
            with self.subTest(dist=dist):
                row = self.merge(review_response(dist=dist))
                self.assertIsNone(row["star_rating"])
                self.assertIsNone(row["count_of_star_ratings"])

    def test_empty_second_page_cannot_hide_failed_first_page(self):
        detail = detail_response(None, status=403)
        detail["review_resps"].append(review_response())
        detail["gql_status"]["reviews"].append(200)
        row = merge_detail("", detail, "123", CFG)
        self.assertIsNone(row["star_rating"])
        self.assertIsNone(row["count_of_star_ratings"])
        self.assertTrue(review_row_is_partial(row))

    def test_listing_title_identifies_bundle_when_comparison_is_unavailable(self):
        detail = detail_response(review_response())
        detail["comparison_resp"] = None
        row = merge_detail("", detail, "123", CFG, product_name=TITLE)
        self.assertTrue(row[IS_BUNDLE])
        self.assertEqual(tuple(row[f] for f in FIELDS), (0.0, 0, 0))

    def test_other_product_title_does_not_classify_target_as_bundle(self):
        detail = detail_response(review_response(dist=distribution(2), total=2), title="Waschmaschine")
        detail["comparison_resp"]["data"]["comparisonTableRecommendations"]["tableData"]["products"].extend(
            comparison_response("999")["data"]["comparisonTableRecommendations"]["tableData"]["products"]
        )
        row = merge_detail("", detail, "123", CFG)
        self.assertFalse(row[IS_BUNDLE])
        self.assertEqual(row["star_rating"], 4.7)

    def test_ssr_bundle_does_not_use_component_jsonld_or_embedded_reviews(self):
        row = parse_pdp_html(ssr_html(), "123", CFG)
        self.assertTrue(row[IS_BUNDLE])
        self.assertIsNone(row["star_rating"])
        self.assertIsNone(row["count_of_star_ratings"])
        self.assertFalse(row["detailed_review_content"])
        self.assertIsNone(parse_pdp_html(ssr_html(), "888", CFG))
        self.assertIsNone(parse_comparison_detail(comparison_response("999"), "123", CFG))

    def test_bundle_discovered_during_ssr_backfill_reapplies_own_reviews(self):
        row = self.merge(review_response(), title="Waschmaschine")
        self.assertEqual(row["star_rating"], 4.7)
        valid, _ = backfill_missing_pdp_fields(row, ssr_html(), "123", CFG)
        self.assertTrue(valid)
        self.assertEqual(tuple(row[f] for f in FIELDS), (0.0, 0, 0))

    def test_spec_recovery_does_not_replace_confirmed_zero_or_unknown_ratings(self):
        for page in (review_response(), None):
            with self.subTest(page=page):
                row = self.merge(page)
                before = tuple(row[f] for f in FIELDS)
                _merge_missing_values(row, {"star_rating": 4.7, "count_of_star_ratings": 601,
                                          "count_of_reviews": 601, "ldy_capacity": "8kg"}, CFG)
                self.assertEqual(tuple(row[f] for f in FIELDS), before)
                self.assertEqual(row["ldy_capacity"], "8kg")

    def test_csv_roundtrip_preserves_zero_and_bundle_marker(self):
        row = self.merge(review_response())
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=csv_columns(CFG), extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)
        stream.seek(0)
        saved = next(csv.DictReader(stream))
        self.assertEqual(resolve_rating_fields(saved, {"star_rating": "4.7", "count_of_reviews": "601"}, None), ("0.0", "0", "0"))

    def test_legacy_bundle_ratings_are_not_reused_by_final_join(self):
        legacy = {"star_rating": "4.7", "count_of_star_ratings": "601", "count_of_reviews": "0"}
        self.assertEqual(resolve_rating_fields(legacy, {"retailer_sku_name": TITLE}, None), ("", "", ""))

    def test_resume_recollects_legacy_bundle_then_keeps_verified_zero(self):
        calls = []

        def fetch(url, sku_id):
            calls.append(sku_id)
            detail = detail_response(review_response())
            detail.update(html="", nav_status=200)
            detail["gql_status"].update(summary=200, comparison=200)
            return detail

        session = SimpleNamespace(open=lambda: None, close=lambda: None, fetch_pdp_detail=fetch)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            listing = root / "listing.csv"
            output = root / "detail.csv"
            with listing.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["sku_id", "rank", "product_url", "retailer_sku_name"])
                writer.writeheader()
                writer.writerow({"sku_id": "123", "rank": "1", "product_url": "https://example.test/123", "retailer_sku_name": TITLE})
            with output.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["sku_id", "rank", "ldy_capacity", *FIELDS])
                writer.writeheader()
                writer.writerow({"sku_id": "123", "rank": "1", "ldy_capacity": "8kg",
                                 "star_rating": "4.7", "count_of_star_ratings": "601", "count_of_reviews": "0"})
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="ldy", SPEC_FIELDS=["ldy_capacity"],
                                  extract_pdp_spec=lambda features, name: {"ldy_capacity": "8kg"})
            args = SimpleNamespace(product="ldy", input=str(listing), bsr=str(root / "no-bsr.csv"),
                                   output=str(output), start=1, limit=1, sleep=0, review_pages=1,
                                   review_max_pages=1, transport="uc", concurrency=1, max_retries=0,
                                   resume=True, missing_retry="none")
            with (
                patch.object(pdp_detail_module, "parse_args", return_value=args),
                patch.object(pdp_detail_module, "load_cfg", return_value=cfg),
                patch.object(pdp_detail_module, "make_session", return_value=session),
                patch.object(pdp_detail_module.sys, "stdout", io.StringIO()),
            ):
                self.assertEqual(pdp_detail_module.main(), 0)
                self.assertEqual(calls, ["123"])
                self.assertEqual(pdp_detail_module.main(), 0)
                self.assertEqual(calls, ["123"])
            with output.open(encoding="utf-8-sig") as fh:
                row = next(csv.DictReader(fh))
            self.assertEqual(resolve_rating_fields(row, {"retailer_sku_name": TITLE}, None), ("0.0", "0", "0"))

    def test_retry_updates_bundle_average_and_counts_together(self):
        row = self.merge(None, status=503)
        row["_review_resps"] = [review_response(dist=distribution(3), total=3)]
        row["_review_statuses"] = [200]
        _refresh_review_row(row, max_pages=8)
        self.assertEqual(tuple(row[f] for f in FIELDS), (5.0, 3, 3))
        self.assertFalse(review_row_is_partial(row))

    def test_graphql_error_recovery_retries_first_page(self):
        row = self.merge({"errors": [{"message": "unavailable"}], "data": {"reviews": None}})
        session = SimpleNamespace(open=lambda: None, close=lambda: None)
        calls = []

        def fetch(sku_id, page_no):
            calls.append((sku_id, page_no))
            return {"status": 200, "data": review_response()}

        session.fetch_review_page = fetch
        args = SimpleNamespace(transport="uc", review_max_pages=8, review_recovery_max_pages=8,
                               review_retry_cooldown=0, sleep=0)
        with patch("common.pdp_detail.make_session", return_value=session), patch("builtins.print"):
            recover_partial_reviews([row], args)
        self.assertEqual(calls, [("123", 1)])
        self.assertEqual(tuple(row[f] for f in FIELDS), (0.0, 0, 0))

    def test_ordinary_product_preserves_comparison_and_listing_fallbacks(self):
        row = self.merge(review_response(dist=None, total=67), title="Waschmaschine")
        self.assertEqual(tuple(row[f] for f in FIELDS), (4.7, 601, 67))
        self.assertEqual(resolve_rating_fields({}, {"star_rating": "4.7", "count_of_reviews": "601"}, None), ("4.7", "601", 0))

    def test_confirmed_zero_distribution_clears_stale_ordinary_average(self):
        row = self.merge(review_response(dist=distribution()), title="Waschmaschine")
        self.assertEqual(tuple(row[f] for f in FIELDS), (0.0, 0, 0))


if __name__ == "__main__":
    unittest.main()
