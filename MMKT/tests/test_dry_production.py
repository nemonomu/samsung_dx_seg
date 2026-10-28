"""Production defaults and notification gates, all external services mocked."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_dry_pipeline import Browser, page_html, ratings, specs
from dry import config, database, notify, run
from dry.detail import CollectionError, parse_ratings, parse_specs
from dry.fields import title_sku, valid_sku
from dry.output import merge_rows, run_metadata, write_output
from dry import output


class ModelAndRatingTests(unittest.TestCase):
    def test_spaced_model_and_attached_suffix_without_energy_class(self):
        cases = {
            "BOMANN WPT 7155 Wärmepumpentrockner (D)": "WPT 7155",
            "HEINNER HHPD-M8G3PDD++ Wärmepumpentrockner (8 kg, A++)": "HHPD-M8G3PDD++",
            "HEINNER HHPD-M8G3D++ Wärmepumpentrockner (8 kg, D)": "HHPD-M8G3D++",
            "BRAND MODEL123 A+++ Trockner": "MODEL123",
            "BRAND MODEL123 A++ Trockner": "MODEL123",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                parsed = parse_specs(specs("1", name=title, extra={"Modelkennung": None}), "1")
                self.assertEqual(expected, parsed["sku"])
                self.assertEqual("title", parsed["_sources"]["sku"]["source"])

    def test_quantities_programs_energy_and_series_are_not_models(self):
        for title in ("BRAND 10 Programme 8kg Trockner", "BRAND Serie 8000 Trockner",
                      "BRAND iQ700 Trockner", "BRAND PTC 1200 W 3-in-1 Trockner",
                      "BRAND LED 2026 Trockner", "BRAND 800W/400W Trockner",
                      "BRAND UV 3000 g Trockner", "AEG 8000 Trockner", "BRAND A+++ Trockner",
                      "BRAND 12Programme Trockner", "BRAND 10Programs Trockner", "BRAND 800W++ Trockner"):
            with self.subTest(title=title):
                self.assertIsNone(title_sku(title))
        for value in ("A++", "A+++", "800W", "3-in-1-Filter"):
            self.assertIsNone(valid_sku(value))

    def test_valid_spec_model_is_preserved_when_title_has_no_model(self):
        value = "SPEC-MODEL123++"
        result = parse_specs(specs("1", name="BRAND Trockner", extra={"Modelkennung": value}), "1")
        self.assertEqual(value, result["sku"])
        self.assertEqual("spec", result["_sources"]["sku"]["source"])

    def merged(self, detail, **listing):
        return merge_rows([{"sku_id": "1", "product_url": "https://www.mediamarkt.de/de/product/_test-1.html",
                            **listing}], [], {"1": detail}, run_metadata())[0]

    def test_missing_stars_become_zero_only_after_successful_statistics(self):
        detail = parse_ratings({"data": {"reviews": {"totalResults": 0, "rating": None}}})
        with contextlib.redirect_stdout(io.StringIO()) as log:
            row = self.merged(detail)
        self.assertEqual((0, 0, 0), tuple(row[k] for k in ("star_rating", "count_of_star_ratings", "count_of_reviews")))
        self.assertEqual(2, log.getvalue().count('"event": "rating_defaulted"'))
        self.assertIn("missing_after_success", log.getvalue())

    def test_real_values_actual_zero_and_listing_fallback_survive(self):
        self.assertEqual(4.6, self.merged({"star_rating": 4.6, "count_of_star_ratings": 20})["star_rating"])
        row = self.merged({"star_rating": 0, "count_of_star_ratings": 0}, star_rating=4.2)
        self.assertEqual((0, 0), (row["star_rating"], row["count_of_star_ratings"]))
        row = self.merged(parse_ratings({"data": {"reviews": {"totalResults": None, "rating": None}}}),
                          star_rating=4.2, count_of_reviews=17)
        self.assertEqual((4.2, 17, None), (row["star_rating"], row["count_of_star_ratings"], row["count_of_reviews"]))
        row = self.merged(parse_ratings(ratings(0, (0, 0, 0, 0, 0))), star_rating=4.2)
        self.assertEqual((0, 0), (row["star_rating"], row["count_of_star_ratings"]))

    def test_malformed_or_missing_statistics_fail_instead_of_zero(self):
        for payload in ({"data": {}}, {"data": {"reviews": {}}},
                        {"data": {"reviews": {"totalResults": -1}}},
                        {"data": {"reviews": {"totalResults": 0, "rating": "bad"}}},
                        {"data": {"reviews": {"totalResults": 0, "rating": {"distribution": {}}}}},
                        {"data": {"reviews": {"totalResults": 0, "rating": {"distribution": [{"value": 6, "count": 1}]}}}}):
            with self.subTest(payload=payload), self.assertRaises(CollectionError):
                parse_ratings(payload)

    def test_observed_sparse_distribution_preserves_real_average_and_counts(self):
        # Real response: zero-count score buckets are omitted, not malformed.
        payload = {"data": {"reviews": {"totalResults": 11, "rating": {"distribution": [
            {"value": 5, "count": 9}, {"value": 3, "count": 1}, {"value": 4, "count": 1},
        ]}}}}
        self.assertEqual({"count_of_reviews": 11, "star_rating": 4.7, "count_of_star_ratings": 11},
                         parse_ratings(payload))
        self.assertEqual(3, len(payload["data"]["reviews"]["rating"]["distribution"]))

    def test_sparse_scores_can_arrive_in_any_order_and_review_count_is_independent(self):
        from itertools import permutations
        for distribution in permutations([{"value": 5, "count": 9}, {"value": 3, "count": 1}, {"value": 4, "count": 1}]):
            result = parse_ratings({"data": {"reviews": {"totalResults": 2, "rating": {"distribution": list(distribution)}}}})
            self.assertEqual((2, 11, 4.7), tuple(result[k] for k in ("count_of_reviews", "count_of_star_ratings", "star_rating")))

    def test_single_score_and_explicit_zero_sparse_distribution(self):
        for count, expected in ((3, (3, 5.0)), (0, (0, None))):
            result = parse_ratings({"data": {"reviews": {"totalResults": count, "rating": {
                "distribution": [{"value": 5, "count": count}]}}}})
            self.assertEqual(expected, (result["count_of_star_ratings"], result["star_rating"]))

    def test_bad_sparse_distributions_still_fail_instead_of_becoming_zero(self):
        invalid = ([{"value": 5, "count": 1}, {"value": 5, "count": 2}],
                   [{"value": 0, "count": 1}], [{"value": 6, "count": 1}],
                   [{"value": 4.5, "count": 1}], [{"value": 5, "count": .5}],
                   [{"value": 5, "count": -1}], [{"value": 5, "count": None}],
                   [{"value": True, "count": 1}], [{"value": 5, "count": True}],
                   [{"value": 5, "count": "NaN"}], [{"value": "Infinity", "count": 1}],
                   [None], ["invalid"])
        for distribution in invalid:
            with self.subTest(distribution=distribution), self.assertRaisesRegex(CollectionError, "REVIEW_DISTRIBUTION_INVALID"):
                parse_ratings({"data": {"reviews": {"totalResults": 0, "rating": {"distribution": distribution}}}})


class ProductionExecutionTests(unittest.TestCase):
    def browser(self):
        browser = Browser({("main", 1): page_html([("9", "Waschmaschine"), ("1", "BRAND WPT 7155 Trockner")],
                                                  shown=2, total=3, more=True),
                           ("main", 2): page_html([("2", "Mini Trockner 4kg")], shown=3, total=3),
                           ("bsr", 1): page_html([("2", "Mini Trockner 4kg")], shown=1, total=1)})
        browser.open = Mock()
        browser.close = Mock()
        return browser

    def execute(self, root, *, options=(), db_error=None, mail_result=None, browser=None):
        browser = browser or self.browser()
        args = ["--output-dir", str(root / "output"), "--log-dir", str(root / "logs"),
                "--render-settle", "0", "--sponsored-extra-wait", "0", "--sleep", "0", "--page-retries", "0", *options]
        with patch("common.uc.UcSession", return_value=browser) as uc, \
                patch.object(run, "inspect_or_insert", side_effect=db_error, return_value={
                    "inserted": 2, "already_loaded": False, "backfilled": {"sku": 0, "loading_type": 0, "capacity": 0}}
                ) as db, patch.object(run, "notify_run", return_value=mail_result or {
                    "state": "sent", "sent": True, "failed": False}) as mail, \
                contextlib.redirect_stdout(io.StringIO()):
            code = run.main(args)
        logs = [json.loads(line) for file in sorted((root / "logs").glob("*.log"))
                for line in file.read_text(encoding="utf-8").splitlines()]
        return code, db, mail, uc, logs

    def test_short_category_defaults_production_and_single_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, mail, _, logs = self.execute(root)
            manifest = json.loads((root / "output/mmkt_dry_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(0, code)
        self.assertEqual(config.PRODUCTION_TABLE, db.call_args.args[0])
        self.assertEqual({"main": 300, "bsr": 100}, manifest["targets"])
        self.assertEqual((2, 1), (manifest["main"]["rows"], manifest["bsr"]["rows"]))
        self.assertEqual("last_page", manifest["main"]["stop_reason"])
        self.assertEqual([1, 2], [page["page"] for page in manifest["pages"]["main"]])
        self.assertEqual(4, manifest["api_calls"])  # two operations per unique product, BSR cached
        self.assertEqual(1, mail.call_count)
        self.assertTrue(mail.call_args.kwargs["collection_success"])
        self.assertIs(True, mail.call_args.kwargs["db_written"])
        self.assertIsNone(mail.call_args.kwargs["error"])
        self.assertIn("collection_elapsed_s", logs[-1])
        self.assertIn("total_elapsed_s", logs[-1])
        detail = [event for event in logs if event["stage"] == "detail"]
        self.assertTrue(detail)
        self.assertTrue(all(event["page"] in (1, 2) for event in detail))
        self.assertEqual([1, 2, 1], [e["page"] for e in logs if e["event"] == "detail_start"])

    def test_explicit_test_selection_is_used_for_load_and_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            code, db, mail, _, _ = self.execute(Path(directory), options=("--table", "test"))
        self.assertEqual(0, code)
        self.assertEqual(config.TEST_TABLE, db.call_args.args[0])
        self.assertEqual(config.TEST_TABLE, mail.call_args.args[2])

    def test_collect_only_has_no_db_or_mail_and_check_table_has_no_browser_or_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, mail, _, _ = self.execute(root, options=("--collect-only",))
            self.assertEqual(0, code)
            db.assert_not_called()
            mail.assert_not_called()
            code, _, mail, uc, _ = self.execute(root, options=("--check-table",))
            self.assertEqual(0, code)
            uc.assert_not_called()
            mail.assert_not_called()

    def test_legacy_csv_stars_normalize_on_memory_copy_after_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, options=("--collect-only",))
            path = root / "output/mmkt_dry_output.csv"
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0].update(star_rating=None, count_of_star_ratings=None, count_of_reviews=0)
            rows[1].update(star_rating="4.6", count_of_star_ratings="12")
            write_output(path, rows)
            manifest_path = path.parent / "mmkt_dry_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["output_digest"] = database.file_digest(path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            digest = database.file_digest(path)
            code, db, mail, uc, logs = self.execute(root, options=("--write-db",))
            self.assertEqual(0, code)
            uc.assert_not_called()
            loaded = db.call_args.args[1]
            self.assertEqual((0, 0, "0"), tuple(loaded[0][k] for k in ("star_rating", "count_of_star_ratings", "count_of_reviews")))
            self.assertEqual(("4.6", "12"), (loaded[1]["star_rating"], loaded[1]["count_of_star_ratings"]))
            self.assertEqual(digest, database.file_digest(path))
            self.assertEqual(2, len(database.load_validated_output(path)))
            self.assertEqual(1, mail.call_count)
            self.assertTrue(any(e.get("event") == "rating_defaulted" and e.get("source") == "validated_csv" for e in logs))

    def test_db_failure_and_unknown_commit_only_request_failure_alert(self):
        for written in (False, None):
            with tempfile.TemporaryDirectory() as directory:
                code, _, mail, _, logs = self.execute(Path(directory), db_error=database.DatabaseError("DB_COMMIT_FAILED", db_written=written))
                self.assertEqual(1, code)
                self.assertEqual(1, mail.call_count)
                self.assertIs(written, mail.call_args.kwargs["db_written"])
                self.assertEqual("DB_COMMIT_FAILED", mail.call_args.kwargs["error"])
                self.assertTrue(logs[-1]["collection_success"])
                self.assertIs(written, logs[-1]["db_written"])

    def test_api_failure_has_no_db_and_cannot_send_success_or_zero_data(self):
        browser = self.browser()
        browser.gql = Mock(return_value={"status": 403})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, mail, _, logs = self.execute(root, browser=browser)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertFalse(mail.call_args.kwargs["collection_success"])
            self.assertTrue(mail.call_args.kwargs["error"])
            self.assertFalse(any(e["event"] == "rating_defaulted" for e in logs))
            self.assertFalse((root / "output/mmkt_dry_output.csv").exists())
            self.assertEqual("failed", logs[-1]["collection_status"])
            self.assertEqual("DETAIL_API_COMMON_FAILURE", logs[-1]["error"])

    def test_sparse_rating_response_reaches_csv_db_and_success_mail_without_extra_calls(self):
        browser = self.browser()
        original = browser.gql
        def gql(operation, variables):
            response = original(operation, variables)
            if operation == "GetProductReviews":
                response["data"] = {"data": {"reviews": {"totalResults": 11, "rating": {"distribution": [
                    {"value": 5, "count": 9}, {"value": 3, "count": 1}, {"value": 4, "count": 1}]}}}}
            return response
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, mail, _, logs = self.execute(root, browser=browser)
            with (root / "output/mmkt_dry_output.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(0, code)
        self.assertEqual([("4.7", "11", "11")] * 2,
                         [tuple(row[k] for k in ("star_rating", "count_of_star_ratings", "count_of_reviews")) for row in rows])
        self.assertEqual(rows, db.call_args.args[1])
        self.assertEqual(4, len(browser.calls))
        self.assertTrue(mail.call_args.kwargs["collection_success"])
        self.assertTrue(mail.call_args.kwargs["db_written"])
        self.assertIsNone(mail.call_args.kwargs["error"])
        self.assertFalse(any(event["event"] == "rating_defaulted" for event in logs))

    def test_mail_failure_returns_nonzero_but_does_not_undo_confirmed_data_success(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, mail, _, logs = self.execute(Path(directory), mail_result={"failed": True, "sent": False})
        self.assertEqual(1, code)
        self.assertEqual(1, mail.call_count)
        self.assertFalse(logs[-1]["success"])
        self.assertTrue(logs[-1]["collection_success"])
        self.assertTrue(logs[-1]["db_written"])


class NotificationTests(unittest.TestCase):
    def manifest(self, table=config.PRODUCTION_TABLE):
        return {"success": True, "batch_id": "m_test", "output_rows": 3, "targets": {"main": 300, "bsr": 100},
                "main": {"rows": 3, "stop_reason": "last_page"}, "bsr": {"rows": 2, "stop_reason": "last_page"},
                "database": {"table": ".".join(table), "state": "success", "db_written": True, "inserted": 3}}

    def invoke(self, root, manifest, *, written=True, collected=True, error=None, enabled=True, dry=False, sender=None, table=config.PRODUCTION_TABLE):
        settings = {"SEG_EMAIL_NOTIFY": "1" if enabled else "0", "SEG_EMAIL_DRY_RUN": "1" if dry else "0"}
        with patch.object(notify, "env_value", side_effect=lambda key, default=None: settings.get(key, default)), \
                patch.object(notify, "_send", side_effect=sender, return_value=(True, 1, None)) as send, \
                contextlib.redirect_stdout(io.StringIO()) as screen:
            result = notify.notify_run(root, manifest, table, collection_success=collected,
                                       db_written=written, error=error, elapsed_s=21.5)
        return result, send, screen.getvalue()

    def test_success_short_category_report_and_same_batch_resend_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = Path(directory), self.manifest()
            result, send, _ = self.invoke(root, manifest)
            self.assertEqual("sent", result["state"])
            self.assertTrue(result["success_report"])
            subject, body = send.call_args.args
            self.assertFalse(subject.startswith("[CHECK]"))
            self.assertIn("main: 3/300 (last_page)", body)
            manifest["database"].update(inserted=0, already_loaded=True)
            result, send, _ = self.invoke(root, manifest)
            self.assertEqual("already_sent", result["state"])
            send.assert_not_called()

    def test_failed_or_uncertain_stages_use_failure_subject_only(self):
        for written, collected, error in ((False, False, "DETAIL_API_FAILED"),
                                           (False, True, "DB_INSERT_FAILED"),
                                           (None, True, "DB_COMMIT_FAILED"),
                                           (True, True, "DB_COMMITTED_MANIFEST_SAVE_FAILED")):
            with tempfile.TemporaryDirectory() as directory:
                manifest = self.manifest()
                result, send, _ = self.invoke(Path(directory), manifest, written=written, collected=collected, error=error)
            self.assertFalse(result["success_report"])
            self.assertTrue(send.call_args.args[0].startswith("[CHECK]"))

    def test_db_target_and_state_must_match_before_success_mail(self):
        for fields in ({"table": ".".join(config.TEST_TABLE)}, {"state": "failed"}, {"db_written": None}):
            with tempfile.TemporaryDirectory() as directory:
                manifest = self.manifest()
                manifest["database"].update(fields)
                result, send, _ = self.invoke(Path(directory), manifest)
            self.assertFalse(result["success_report"])
            self.assertTrue(send.call_args.args[0].startswith("[CHECK]"))

    def test_disabled_and_dry_run_never_call_smtp(self):
        for enabled, dry, state in ((False, False, "disabled"), (True, True, "dry_run")):
            with tempfile.TemporaryDirectory() as directory:
                result, send, _ = self.invoke(Path(directory), self.manifest(), enabled=enabled, dry=dry)
            self.assertEqual(state, result["state"])
            self.assertFalse(result["failed"])
            send.assert_not_called()

    def test_smtp_exception_and_returned_error_are_not_logged_or_saved(self):
        for raises in (True, False):
            def sender(*args):
                if raises:
                    raise RuntimeError("RAW_MAIL_ERROR_MUST_NOT_PRINT")
                return False, 1, "RAW_MAIL_ERROR_MUST_NOT_PRINT"
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result, _, screen = self.invoke(root, self.manifest(), sender=sender)
                text = screen + "".join(file.read_text(encoding="utf-8") for file in root.iterdir())
            self.assertNotIn("RAW_MAIL_ERROR_MUST_NOT_PRINT", text)
            self.assertTrue(result["failed"])
            self.assertEqual("SMTP_SEND_FAILED", result["error"])

    def test_known_smtp_causes_are_classified_without_original_error_text(self):
        for message, code in (("missing SMTP settings", "SMTP_CONFIGURATION_MISSING"),
                              ("SMTPAuthenticationError: RAW_AUTH_MUST_NOT_PRINT", "SMTP_AUTHENTICATION_FAILED"),
                              ("TimeoutError: RAW_AUTH_MUST_NOT_PRINT", "SMTP_TIMEOUT")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                result, _, screen = self.invoke(root, self.manifest(), sender=lambda *_: (False, 1, message))
                persisted = (root / "mmkt_dry_manifest.json").read_text(encoding="utf-8")
            self.assertEqual(code, result["error"])
            self.assertNotIn("RAW_AUTH_MUST_NOT_PRINT", screen + persisted)

    def test_post_send_persistence_failure_does_not_resend_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = Path(directory), self.manifest()
            original = notify.write_json
            def save(path, data):
                if data.get("notification", {}).get("state") == "sent":
                    raise OSError("RAW_FILE_ERROR_MUST_NOT_PRINT")
                original(path, data)
            with patch.object(notify, "write_json", side_effect=save):
                result, send, screen = self.invoke(root, manifest)
            self.assertTrue(result["sent"])
            self.assertTrue(result["failed"])
            send.assert_called_once()
            self.assertNotIn("RAW_FILE_ERROR_MUST_NOT_PRINT", screen)
            saved = json.loads((root / "mmkt_dry_manifest.json").read_text(encoding="utf-8"))
            result, send, _ = self.invoke(root, saved)
            self.assertEqual("previous_send_unknown", result["state"])
            send.assert_not_called()

    def test_test_table_sent_marker_does_not_suppress_production_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root, manifest = Path(directory), self.manifest(config.TEST_TABLE)
            self.invoke(root, manifest, table=config.TEST_TABLE)
            manifest["database"]["table"] = ".".join(config.PRODUCTION_TABLE)
            _, send, _ = self.invoke(root, manifest)
            send.assert_called_once()
            manifest["database"]["table"] = ".".join(config.TEST_TABLE)
            result, send, _ = self.invoke(root, manifest, table=config.TEST_TABLE)
            self.assertEqual("already_sent", result["state"])
            send.assert_not_called()

    def test_manifest_partial_write_preserves_previous_valid_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mmkt_dry_manifest.json"
            output.write_manifest(path, {"success": True, "output_rows": 3})
            def partial_write(temporary, data):
                temporary.write_text('{"partial":', encoding="utf-8")
                raise OSError("mock disk failure")
            with patch.object(output, "_write_json", side_effect=partial_write), self.assertRaises(OSError):
                output.write_manifest(path, {"success": False})
            self.assertEqual({"success": True, "output_rows": 3}, json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
