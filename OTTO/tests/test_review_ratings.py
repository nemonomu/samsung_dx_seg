"""Offline rating regressions. Synthetic HTML only; no external side effects."""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import db_save, full_output, notify, parsers
from common.review_rating import parse_rating, rating_count
from bs4 import BeautifulSoup


def document(mean="4.3", count="200", font="oc-display-200"):
    return (f'<html><title>Kunden-Test &amp; Erfahrungen</title><body>'
            f'<div class="pdp_cr-rating"><div class="pdp_cr-rating-score">'
            f'<span class="{font}">{mean}</span><span>von 5</span></div>({count})</div>'
            '</body></html>').encode()


def response(body=None, status=200, **extra):
    return dict(status=status, body=document() if body is None else body, error=None, **extra)


def parse(html):
    return parse_rating(BeautifulSoup(html, "lxml"))


class RatingParserTests(unittest.TestCase):
    def test_old_current_and_unknown_font(self):
        for font in ("oc-headline-300", "oc-display-200", "future-font"):
            with self.subTest(font=font):
                r = parse(document(font=font))
                self.assertEqual(("rated", "4.3", 200),
                                 (r["rating_state"], r["average_rating"], r["rating_count"]))

    def test_decimal_and_grouped_counts(self):
        for value in ("1.234", "1,234", "1 234", "1\u202f234", "1\u00a0234"):
            with self.subTest(count=value):
                r = parse(document("4,3", value))
                self.assertEqual(("4.3", 1234, "rated"),
                                 (r["average_rating"], r["rating_count"], r["rating_state"]))

    def test_invalid_counts(self):
        for value in ("20.5", "-1", "2,00", "1.234,567", "many", ""):
            with self.subTest(value=value):
                self.assertIsNone(rating_count(value))
                r = parse(document(count=value))
                self.assertEqual("failed", r["rating_state"])
                self.assertEqual("4.3", r["average_rating"])
                self.assertIsNone(r["rating_count"])

    def test_invalid_means_keep_valid_count(self):
        for value in ("6", "5.1", "-1", "0.5", "bad", "4.3 4.5"):
            with self.subTest(value=value):
                r = parse(document(mean=value))
                self.assertEqual("failed", r["rating_state"])
                self.assertIsNone(r["average_rating"])
                self.assertEqual(200, r["rating_count"])

    def test_zero_contradiction_is_not_rated_or_unrated(self):
        r = parse(document("0.0", "200"))
        self.assertEqual("zero_average_with_ratings", r["rating_reason"])
        self.assertIsNone(r["average_rating"])
        self.assertEqual(200, r["rating_count"])

    def test_positive_mean_zero_count_keeps_mean(self):
        r = parse(document(count="0"))
        self.assertEqual("failed", r["rating_state"])
        self.assertEqual("4.3", r["average_rating"])
        self.assertIsNone(r["rating_count"])

    def test_confirmed_zero_summary(self):
        r = parse(document("0", "0"))
        self.assertEqual(("unrated", "0.0", 0),
                         (r["rating_state"], r["average_rating"], r["rating_count"]))

    def test_zero_summary_with_review_content_is_not_unrated(self):
        html = document("0", "0").replace(b'</body>', b'<div class="pdp_cr-item-content">Review</div></body>')
        r = parse(html)
        self.assertEqual("failed", r["rating_state"])
        self.assertIsNone(r["average_rating"])

    def test_observed_no_review_labels(self):
        for html in (
            '<div class="js_pdp_cr_sidebar__noReviewsText">Für diesen Artikel wurde noch keine Bewertung abgegeben.</div>',
            '<div class="cr_list-wrapper--no-review"><p>Für diesen Artikel wurde noch keine Bewertung abgegeben.</p></div>',
        ):
            with self.subTest(html=html):
                r = parse(html)
                self.assertEqual("unrated", r["rating_state"])
                self.assertEqual("0.0", r["average_rating"])
                self.assertEqual(0, r["rating_count"])

    def test_missing_summary_is_not_no_reviews(self):
        r = parse('<html><h1>Welcome</h1><p>Für diesen Artikel wurde noch keine Bewertung abgegeben.</p></html>')
        self.assertEqual("failed", r["rating_state"])
        self.assertIsNone(r["average_rating"])
        self.assertIsNone(r["rating_count"])

    def test_duplicate_summary_is_ambiguous(self):
        r = parse(document().replace(b'</body>', document("4.8").split(b'<body>')[1].split(b'</body>')[0] + b'</body>'))
        self.assertEqual("ambiguous_rating_summary", r["rating_reason"])
        self.assertIsNone(r["average_rating"])

    def test_duplicate_scores_and_counts(self):
        for html in (
            document().replace(b'</div>(200)', b'<div class="pdp_cr-rating-score">4.8</div></div>(200)'),
            document().replace(b'(200)', b'(200) (100)'),
        ):
            self.assertEqual("failed", parse(html)["rating_state"])

    def test_outside_recommendation_and_distribution_numbers_ignored(self):
        html = document() + b'<div class="pdp_cr-rating-score"><span class="oc-headline-300">5</span></div><div class="recommendations">4.9 (99)</div>'
        self.assertEqual("4.3", parse(html)["average_rating"])
        self.assertEqual(200, parse(html)["rating_count"])

    def test_blocked_page_with_stale_summary(self):
        html = document().replace(b'Kunden-Test &amp; Erfahrungen', b'Access denied')
        self.assertEqual("blocked_page", parse(html)["rating_reason"])

    def test_memory_and_file_parser_are_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "synthetic.html"
            p.write_bytes(document())
            self.assertEqual(parsers.parse_review_html(p), parsers.parse_review_document(document(), path=str(p)))


class RatingCollectionTests(unittest.TestCase):
    URL = "https://www.otto.de/kundenbewertungen/123/"

    def collect(self, values):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(full_output, "fetch_html", side_effect=values) as fetch, \
                patch.object(full_output.time, "sleep"), \
                patch.object(full_output.raw_html, "save") as save, \
                patch("sys.stdout", new=io.StringIO()):
            r = full_output.collect_review(self.URL, Path(tmp), "123", product="TV")
            self.assertFalse(list(Path(tmp).iterdir()), "No HTML or response files")
            save.assert_not_called()
            return r, fetch.call_count

    def test_valid_page_no_extra_request(self):
        r, calls = self.collect([response()])
        self.assertEqual(1, calls)
        self.assertEqual("rated", r["rating_state"])

    def test_transient_layout_failure_then_success(self):
        r, calls = self.collect([response(b'<html>Changed</html>'), response()])
        self.assertEqual(2, calls)
        self.assertEqual("4.3", r["average_rating"])
        self.assertEqual(2, len(r["_rating_history"]))

    def test_exhausted_parse_failure_is_null(self):
        r, calls = self.collect([response(b'<html>Changed</html>')] * 3)
        self.assertEqual(3, calls)
        self.assertEqual("failed", r["rating_state"])
        self.assertIsNone(r["average_rating"])
        self.assertIsNone(r["rating_count"])

    def test_http_error_and_retry_bound(self):
        for status in (403, 429, 500, 503):
            with self.subTest(status=status):
                r, calls = self.collect([response(status=status)] * 3)
                self.assertEqual(3, calls)
                self.assertIsNone(r["average_rating"])
                self.assertEqual("failed", r["rating_state"])
                self.assertEqual(status, r["_review_status"])

    def test_request_exception_does_not_escape(self):
        r, calls = self.collect([TimeoutError("Do not log raw error details")] * 3)
        self.assertEqual(3, calls)
        self.assertEqual("request_exception:TimeoutError", r["rating_reason"])
        self.assertNotIn("Do not log", json.dumps(r))

    def test_parse_exception_keeps_http_200_diagnostic(self):
        with patch.object(full_output, "parse_review_document", side_effect=ValueError("Raw details omitted")):
            r, calls = self.collect([response()] * 3)
        self.assertEqual(200, r["_review_status"])
        self.assertEqual("parse_exception:ValueError", r["rating_reason"])
        self.assertNotIn("Raw details", json.dumps(r))

    def test_200_wrong_redirect_is_failed(self):
        for url in ("https://www.otto.de/login/", "https://www.otto.de/kundenbewertungen/456/"):
            r, calls = self.collect([response(final_url=url)] * 3)
            self.assertEqual("unexpected_review_page", r["rating_reason"])
            self.assertIsNone(r["average_rating"])

    def test_partial_value_survives_bad_retry(self):
        r, calls = self.collect([response(document(mean="bad")), response(status=503), response(status=403)])
        self.assertEqual(200, r["rating_count"])
        self.assertIsNone(r["average_rating"])
        self.assertEqual("failed", r["rating_state"])

    def test_200_other_product_content_is_failed(self):
        body = document().replace(b'</body>', b'<aside class="js_cr_sidebar"><a href="/p/other-product-456/">Product</a></aside></body>')
        r, calls = self.collect([response(body)] * 3)
        self.assertEqual("unexpected_review_product", r["rating_reason"])
        self.assertIsNone(r["average_rating"])

    def test_rating_retry_keeps_written_reviews_and_recommendation(self):
        first = parsers.parse_review_document(document(mean="bad"))
        first.update(reviews=[dict(review_id="r1", review_text="Useful detail")],
                     review_text_rows=1, recommendation_intent="90%")
        second = parsers.parse_review_document(document())
        with patch.object(full_output, "parse_review_document", side_effect=[first, second]):
            r, calls = self.collect([response(), response()])
        self.assertEqual(2, calls)
        self.assertEqual("4.3", r["average_rating"])
        self.assertEqual("90%", r["recommendation_intent"])
        self.assertIn("Useful detail", r["detailed_review_content"])

    def test_confirmed_unrated_no_retry(self):
        r, calls = self.collect([response(document("0", "0"))])
        self.assertEqual(1, calls)
        self.assertEqual("unrated", r["rating_state"])

    def test_independent_valid_fields_are_not_overwritten_by_partial_retry(self):
        r, calls = self.collect([response(document(count="bad")),
                                 response(document(mean="bad")), response(status=503)])
        self.assertEqual(3, calls)
        self.assertEqual("4.3", r["average_rating"])
        self.assertEqual(200, r["rating_count"])
        self.assertEqual("failed", r["rating_state"])

    def test_missing_url_diagnostic(self):
        with patch.object(full_output, "fetch_html") as fetch:
            r = full_output.collect_review(None, Path("unused"), "123")
        fetch.assert_not_called()
        self.assertEqual("missing_review_url", r["rating_reason"])


class RatingPipelineTests(unittest.TestCase):
    def test_three_categories_continue_after_failed_item_and_preserve_other_fields(self):
        for product in ("TV", "REF", "LDY"):
            with self.subTest(product=product), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                targets = [dict(product_id=str(n), main_rank=str(n), bsr_rank="",
                                retailer_sku_name="Test ABC12345", product_url=f"https://www.otto.de/p/test-{n}/",
                                final_sku_price="499.99", average_rating_listing="4.5", count_of_reviews_listing="200")
                           for n in (1, 2)]
                with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(targets[0])); w.writeheader(); w.writerows(targets)
                cfg = SimpleNamespace(PRODUCT=product, ACCOUNT_NAME="OTTO", COUNTRY="DE", USE_DATASHEET=False,
                                      SPEC_FIELDS=["test_spec"], extract_spec=lambda *a, **kw: {"test_spec": "preserved"})
                requests = [response(document(mean="bad"))] * 3 + [response()]
                with patch.object(full_output, "category_output_root", return_value=out), \
                        patch.object(full_output, "fetch_html", side_effect=requests), \
                        patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                        patch.object(full_output, "refresh_sticker_fields"), \
                        patch.object(full_output, "sticker_diagnostics", return_value={}), \
                        patch.object(full_output.time, "sleep"), patch("sys.stdout", new=io.StringIO()) as log:
                    manifest = full_output.run(cfg, pdp_supplement="none", detail_sleep=0)
                rows = list(csv.DictReader((out / "otto_full_output.csv").open(encoding="utf-8-sig")))
                self.assertEqual(2, len(rows))
                self.assertEqual("", rows[0]["star_rating"])
                self.assertEqual("200", rows[0]["count_of_star_ratings"])
                self.assertEqual("4.3", rows[1]["star_rating"])
                self.assertEqual({"checked": 2, "rated": 1, "unrated": 0, "failed": 1, "contradictions": 0}, manifest["rating_qa"])
                for n, row in enumerate(rows, 1):
                    self.assertEqual(str(n), row["main_rank"])
                    self.assertEqual(str(n), row["item"])
                    self.assertEqual("499.99", row["final_sku_price"])
                    self.assertEqual("ABC12345", row["sku"])
                    self.assertEqual("preserved", row["test_spec"])
                    self.assertEqual(manifest["batch_id"], row["batch_id"])
                self.assertIn('"product": "' + product + '"', log.getvalue())
                self.assertIn("rating-QA", log.getvalue())

    def test_current_failure_report_and_stale_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            rows = [dict(batch_id="current", main_rank="1", bsr_rank="", star_rating="", count_of_star_ratings="200")]
            (out / "step02_final_targets_manifest.json").write_text(json.dumps(dict(main_target_unique=1, bsr_rank_limit=0)))
            (out / "step09_full_output_manifest.json").write_text(json.dumps(dict(
                batch_id="current", rating_qa=dict(rated=0, unrated=0, failed=1))))
            cfg = SimpleNamespace(PRODUCT="TV", SPEC_FIELDS=[])
            with patch.object(notify, "category_output_root", return_value=out):
                subject, text = notify.build_report(cfg, rows)
                self.assertIn("확인필요", subject)
                self.assertIn("별점 요청·추출 실패 1/1", text)
                rows[0]["batch_id"] = "later"
                subject, text = notify.build_report(cfg, rows)
                self.assertNotIn("별점 요청·추출 실패", text)

    def test_report_warns_on_legacy_zero_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(notify, "category_output_root", return_value=Path(tmp)):
            _, text = notify.build_report(SimpleNamespace(PRODUCT="TV", SPEC_FIELDS=[]), [
                dict(main_rank="1", star_rating="0.0", count_of_star_ratings="200")])
        self.assertIn("평가가 있는데 평균 별점이 0인 모순", text)


class RatingDBNullTests(unittest.TestCase):
    def run_save(self, nullability):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        out = Path(tmp)
        data = dict(item="123", batch_id="new", star_rating="", count_of_star_ratings="200", count_of_reviews="200")
        with (out / "otto_full_output.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data)); w.writeheader(); w.writerow(data)
        conn = MagicMock(); cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [(k, nullability if k == "star_rating" else "YES") for k in data]
        for obj, name, value in ((db_save, "category_output_root", out), (db_save, "db_config", {"host": "mock"}),
                                 (db_save, "safe_backfill_from_retail_history", dict(recovered_rows=0, recovered_fields=0, error=None))):
            self.enterContext(patch.object(obj, name, return_value=value))
        self.enterContext(patch("psycopg2.connect", return_value=conn))
        cfg = SimpleNamespace(PRODUCT="TV", ACCOUNT_NAME="OTTO", DB_TABLE="dx_seg.dx_seg_tv_retail_com", SPEC_FIELDS=[])
        return out, cur, cfg

    def test_nullable_schema_passes_none_to_existing_insert(self):
        out, cur, cfg = self.run_save("YES")
        with patch("sys.stdout", new=io.StringIO()):
            result = db_save.run(cfg, dry_run=False)
        self.assertTrue(result["success"])
        sql, values = cur.executemany.call_args.args
        self.assertTrue(sql.startswith("INSERT INTO"))
        self.assertIsNone(values[0][2])
        self.assertEqual("200", values[0][3])

    def test_not_null_or_unknown_schema_blocks_without_insert(self):
        for nullable in ("NO", "unknown"):
            with self.subTest(nullable=nullable):
                out, cur, cfg = self.run_save(nullable)
                with self.assertRaisesRegex(RuntimeError, "rating_null_not_supported:star_rating"):
                    db_save.run(cfg, dry_run=False)
                cur.executemany.assert_not_called()
                saved = json.loads((out / "step14_db_save_manifest.json").read_text(encoding="utf-8"))
                self.assertFalse(saved["success"])
                self.assertEqual(0, saved["inserted"])


if __name__ == "__main__":
    unittest.main()
