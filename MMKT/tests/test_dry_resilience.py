"""Product isolation and validated-artifact recovery, without external I/O."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_dry_pipeline import Browser, page_html, specs
from dry import config, database, notify, run
from dry.detail import CollectionError, DetailCollector, parse_specs


class ResilienceTests(unittest.TestCase):
    def execute(self, root, browser=None, *, options=(), save=None, notify_save=None, db_error=None):
        browser = browser or self.browser()
        browser.open = Mock()
        browser.close = Mock()
        args = ["--output-dir", str(root / "output"), "--log-dir", str(root / "logs"),
                "--target", "2", "--bsr-target", "2", "--page-retries", "0", "--sleep", "0",
                "--render-settle", "0", "--sponsored-extra-wait", "0", *options]
        with contextlib.ExitStack() as stack:
            uc = stack.enter_context(patch("common.uc.UcSession", return_value=browser))
            db = stack.enter_context(patch.object(run, "inspect_or_insert", side_effect=db_error, return_value={
                "inserted": 2, "already_loaded": False, "backfilled": {"sku": 0, "capacity": 0, "loading_type": 0}}))
            stack.enter_context(patch.object(notify, "env_value", return_value="0"))
            smtp = stack.enter_context(patch.object(notify, "_send", side_effect=AssertionError("SMTP forbidden")))
            if save:
                stack.enter_context(patch.object(run, "write_json", side_effect=save))
            if notify_save:
                stack.enter_context(patch.object(notify, "write_json", side_effect=notify_save))
            screen = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = run.main(args)
        logs = [json.loads(line) for file in sorted((root / "logs").glob("*.log"))
                for line in file.read_text(encoding="utf-8").splitlines()]
        smtp.assert_not_called()
        return code, db, uc, logs[-1], screen.getvalue()

    def browser(self):
        return Browser({(sort, 1): page_html([("1", "Trockner"), ("2", "Trockner")], shown=2, total=2)
                        for sort in ("main", "bsr")})

    def saved(self, root):
        path = root / "output/mmkt_dry_output.csv"
        return path, json.loads((path.parent / "mmkt_dry_manifest.json").read_text(encoding="utf-8"))

    def test_empty_specs_allow_title_fields_and_leave_unproven_fields_null(self):
        for empty in (None, {"featureGroups": None}, {"featureGroups": []},
                      {"featureGroups": [{"features": []}]}):
            payload = specs("1", name="BRAND REALMODEL456 Trockner (8 kg)")
            product = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]["productAggregate"]["product"]
            product["featureGroupsWithProductId"] = empty
            result = parse_specs(payload, "1")
            self.assertEqual(("REALMODEL456", "8kg", None),
                             (result["sku"], result["capacity"], result["loading_type"]))
            self.assertEqual({}, result["_features"])
            self.assertEqual("title", result["_sources"]["capacity"]["source"])
        browser = self.browser()
        original = browser.gql
        def gql(operation, variables):
            response = original(operation, variables)
            if operation == "GetComparisonTableRecommendations":
                product = response["data"]["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]["productAggregate"]["product"]
                product["featureGroupsWithProductId"] = {"featureGroups": []}
            return response
        browser.gql = gql
        collector = DetailCollector(browser, retries=0, sleep_s=0)
        detail = collector.get("1", {"retailer_sku_name": "Trockner 4kg"})
        self.assertEqual("4kg", detail["capacity"])
        self.assertIn("star_rating", detail)
        self.assertEqual(2, len(browser.calls))

    def test_missing_or_malformed_spec_structure_remains_product_failure(self):
        for invalid in ("missing", {}, [], "bad", {"featureGroups": {}},
                        {"featureGroups": [None]}, {"featureGroups": [{}]},
                        {"featureGroups": [{"features": {}}]}, {"featureGroups": [{"features": [None]}]}):
            payload = specs("1")
            product = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]["productAggregate"]["product"]
            if invalid == "missing":
                product.pop("featureGroupsWithProductId")
            else:
                product["featureGroupsWithProductId"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(CollectionError):
                parse_specs(payload, "1", "Trockner 8kg")

    def test_failed_product_does_not_block_later_pages_targets_ranks_or_loading(self):
        browser = Browser({("main", 1): page_html([("9", "Trockner")], shown=1, total=4, more=True),
                           ("main", 2): page_html([("9", "Trockner"), ("1", "Trockner"), ("2", "Trockner")], shown=4, total=4),
                           ("bsr", 1): page_html([("2", "Trockner"), ("1", "Trockner")], shown=2, total=2)})
        original = browser.gql
        failed_calls = []
        def gql(operation, variables):
            if variables.get("ref") == "9":
                failed_calls.append(operation)
                return {"status": 403, "raw": "RAW_API_MUST_NOT_PRINT"}
            return original(operation, variables)
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, end, screen = self.execute(root, browser)
            path, manifest = self.saved(root)
            rows = database.load_validated_output(path)
            self.assertEqual(1, code)  # completed partial result still requires review
            self.assertEqual([("1", "1", "2"), ("2", "2", "1")],
                             [(r["item"], r["main_rank"], r["bsr_rank"]) for r in rows])
            self.assertEqual(rows, db.call_args.args[1])
            self.assertEqual("target_reached", manifest["main"]["stop_reason"])
            self.assertEqual(1, manifest["product_failure_count"])
            self.assertEqual("partial", manifest["collection_status"])
            self.assertEqual({"sort": "main", "page": 1, "item": "9", "error": "DETAIL_API_FAILED",
                              "error_type": "CollectionError"}, manifest["product_failures"][0])
            self.assertFalse(manifest["notification"]["success_report"])
            report = (path.parent / "mmkt_dry_email_report.txt").read_text(encoding="utf-8")
            self.assertIn("partial collection", report)
            self.assertIn("page=1 item=9", report)
            self.assertTrue(end["collection_completed"])
            self.assertFalse(end["collection_success"])
            self.assertTrue(end["db_written"])
            self.assertNotIn("RAW_API_MUST_NOT_PRINT", screen + json.dumps(manifest))
            digest = database.file_digest(path)
            code, db, uc, end, _ = self.execute(root, options=("--write-db",))
            self.assertEqual(1, code)
            uc.assert_not_called()
            db.assert_called_once()
            self.assertEqual(digest, database.file_digest(path))
            self.assertFalse(end["collection_success"])
        self.assertEqual(1, len(failed_calls))  # failed occurrence dedup within Main
        self.assertEqual(4, len(browser.calls))  # accepted Main/BSR still share cache

    def test_rating_parse_failure_skips_product_instead_of_defaulting_to_zero(self):
        browser = self.browser()
        original = browser.gql
        def gql(operation, variables):
            if operation == "GetProductReviews" and variables["id"] == "1":
                return {"status": 200, "data": {"data": {"reviews": {"totalResults": 1, "rating": {
                    "distribution": [{"value": 9, "count": 1}]}}}}}
            return original(operation, variables)
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, end, screen = self.execute(root, browser)
            path, manifest = self.saved(root)
            self.assertEqual(1, code)
            self.assertEqual(["2"], [row["item"] for row in db.call_args.args[1]])
            self.assertEqual(2, manifest["product_failure_count"])
            self.assertEqual({"REVIEW_DISTRIBUTION_INVALID"}, {r["error"] for r in manifest["product_failures"]})
            self.assertNotIn('"event": "rating_defaulted"', screen)
            self.assertTrue(end["db_written"])

    def test_unconfirmed_type_can_retry_in_bsr_without_accepting_incomplete_cache(self):
        browser = Browser({("main", 1): page_html([("1", "MODEL123"), ("2", "Trockner")], shown=2, total=2),
                           ("bsr", 1): page_html([("1", "Trockner"), ("2", "Trockner")], shown=2, total=2)})
        original = browser.gql
        def gql(operation, variables):
            if operation == "GetComparisonTableRecommendations" and variables["ref"] == "1":
                browser.calls.append((operation, copy.deepcopy(variables)))
                return {"status": 200, "data": specs("1", product_type="Unbekannt", name="MODEL123")}
            return original(operation, variables)
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            code, db, _, end, _ = self.execute(Path(directory), browser)
        self.assertEqual(1, code)
        rows = {row["item"]: row for row in db.call_args.args[1]}
        self.assertEqual(("", "1", "4.7"), (rows["1"]["main_rank"], rows["1"]["bsr_rank"], rows["1"]["star_rating"]))
        self.assertEqual(1, sum(op == "GetProductReviews" and v["id"] == "1" for op, v in browser.calls))
        self.assertEqual(1, end["product_failure_count"])

    def test_common_transport_failure_stops_and_preserves_intermediate_without_db(self):
        browser = self.browser()
        browser.gql = Mock(side_effect=RuntimeError("RAW_PRODUCT_ERROR_MUST_NOT_PRINT"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, end, screen = self.execute(root, browser)
            path, manifest = self.saved(root)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertFalse(path.exists())
            self.assertEqual(2, manifest["product_failure_count"])
            self.assertEqual("DETAIL_API_COMMON_FAILURE", end["error"])
            self.assertFalse(end["csv_preserved"])
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(2, len(saved["detail_results"]))
            self.assertFalse(saved["loadable"])
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertNotIn("RAW_PRODUCT_ERROR_MUST_NOT_PRINT", screen + json.dumps(manifest))
            code, db, uc, end, _ = self.execute(root, options=("--write-db",))
            db.assert_not_called()
            uc.assert_not_called()
            self.assertEqual(1, code)

    def test_load_state_failure_preserves_batch_digest_validation_and_retry(self):
        for notification_write_fails in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.assertEqual(0, self.execute(root, options=("--collect-only",))[0])
                path, before = self.saved(root)
                digest = database.file_digest(path)
                original = run.write_json
                def write(target, value):
                    if value.get("database", {}).get("state") == "running":
                        raise OSError("RAW_STATE_FILE_ERROR_MUST_NOT_PRINT")
                    original(target, value)
                def broken_notify(*_):
                    raise OSError("RAW_NOTIFICATION_ERROR_MUST_NOT_PRINT")
                code, db, uc, end, screen = self.execute(root, options=("--write-db",), save=write,
                    notify_save=broken_notify if notification_write_fails else None)
                db.assert_not_called()
                uc.assert_not_called()
                self.assertEqual(1, code)
                self.assertTrue(end["collection_success"])
                self.assertTrue(end["csv_preserved"])
                self.assertFalse(end["db_written"])
                self.assertEqual("DB_STATE_SAVE_FAILED", end["error"])
                _, after = self.saved(root)
                for key in ("batch_id", "success", "output_rows", "output_digest", "targets", "main", "bsr"):
                    self.assertEqual(before[key], after[key])
                self.assertEqual(2, len(database.load_validated_output(path)))
                self.assertEqual(digest, database.file_digest(path))
                self.assertNotIn("RAW_STATE_FILE_ERROR", screen)
                self.assertNotIn("RAW_NOTIFICATION_ERROR", screen)
                code, db, uc, end, _ = self.execute(root, options=("--write-db",))
                self.assertEqual(0, code)
                db.assert_called_once()
                uc.assert_not_called()
                self.assertTrue(end["db_written"])
                self.assertEqual(before["batch_id"], self.saved(root)[1]["batch_id"])

    def test_all_status_writes_failing_leave_original_validated_manifest_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, options=("--collect-only",))
            path, before = self.saved(root)
            def write(*_):
                raise OSError("RAW_FILE_ERROR_MUST_NOT_PRINT")
            code, db, _, end, screen = self.execute(root, options=("--write-db",), save=write, notify_save=write)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertTrue(end["csv_preserved"])
            self.assertFalse(end["db_written"])
            self.assertEqual(before, self.saved(root)[1])
            self.assertEqual(2, len(database.load_validated_output(path)))
            self.assertNotIn("RAW_FILE_ERROR", screen)

    def test_committed_data_stays_confirmed_when_status_and_notification_writes_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, options=("--collect-only",))
            path, before = self.saved(root)
            original = run.write_json
            def write(target, value):
                if value.get("database", {}).get("state") == "success":
                    raise OSError("RAW_FILE_ERROR_MUST_NOT_PRINT")
                original(target, value)
            def broken_notify(*_):
                raise OSError("RAW_MAIL_FILE_ERROR_MUST_NOT_PRINT")
            code, db, _, end, screen = self.execute(root, options=("--write-db",), save=write, notify_save=broken_notify)
            self.assertEqual(1, code)
            db.assert_called_once()
            self.assertTrue(end["db_written"])
            self.assertTrue(end["db_success"])
            self.assertEqual("DB_COMMITTED_MANIFEST_SAVE_FAILED", end["error"])
            # A crash after COMMIT could leave the on-disk state at running;
            # it must say unknown, never incorrectly claim no DB write.
            after = self.saved(root)[1]
            self.assertEqual("running", after["database"]["state"])
            self.assertIsNone(after["database"]["db_written"])
            self.assertEqual(before["batch_id"], after["batch_id"])
            self.assertEqual(2, len(database.load_validated_output(path)))
            self.assertNotIn("RAW_FILE_ERROR", screen)
            self.assertNotIn("RAW_MAIL_FILE_ERROR", screen)

    def test_transient_load_validation_failure_does_not_replace_source_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, options=("--collect-only",))
            path, before = self.saved(root)
            with patch.object(run, "load_validated_output", side_effect=OSError("RAW_READ_ERROR_MUST_NOT_PRINT")):
                code, db, _, end, screen = self.execute(root, options=("--write-db",))
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertFalse(end["collection_success"])
            self.assertEqual(before, self.saved(root)[1])
            self.assertEqual(2, len(database.load_validated_output(path)))
            failure_path = path.parent / "mmkt_dry_load_failure.json"
            self.assertFalse(json.loads(failure_path.read_text(encoding="utf-8"))["success"])
            self.assertNotIn("RAW_READ_ERROR", screen + failure_path.read_text(encoding="utf-8"))

    def test_partial_manifest_cannot_send_success_even_if_caller_mislabels_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"success": True, "batch_id": "m_test", "output_rows": 1, "product_failure_count": 1,
                        "product_failures": [{"sort": "main", "page": 1, "item": "9", "error": "DETAIL_API_FAILED"}],
                        "database": {"table": ".".join(config.PRODUCTION_TABLE), "state": "success", "db_written": True}}
            with patch.object(notify, "env_value", return_value="0"), patch.object(notify, "_send") as send:
                result = notify.notify_run(root, manifest, config.PRODUCTION_TABLE,
                                           collection_success=True, db_written=True)
            self.assertFalse(result["success_report"])
            self.assertIn("[CHECK]", (root / "mmkt_dry_email_report.txt").read_text(encoding="utf-8"))
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
