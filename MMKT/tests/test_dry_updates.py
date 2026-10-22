"""DRY regression cases with fake browser/API/DB only; no external connections."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_dry_pipeline import Browser, page_html, specs
from dry import config, database, run
from dry.detail import CollectionError, DetailCollector, parse_specs
from dry.events import RunLog, emit, failure, quiet_external
from dry.fields import valid_sku
from dry.policy import exclusion_reason


def schema():
    return {key: {"nullable": True, "default": False, "identity": key == "id", "type": "text",
                  "max_length": None, "udt": "text"} for key in config.COLUMNS}


def db_row(item="1", **values):
    row = dict.fromkeys(config.COLUMNS)
    row.update(country="SEG", account_name="Mediamarkt", item=item, batch_id="m_20261022_120000",
               crawl_datetime="2026-10-22 12:00:00", **values)
    return row


def fake_db():
    connection, cursor = Mock(), Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    cursor.__enter__ = Mock(return_value=cursor)
    cursor.__exit__ = Mock(return_value=False)
    connection.cursor.return_value = cursor
    cursor.fetchall.return_value = []
    cursor.fetchmany.return_value = []
    return connection, cursor


class ExtractionUpdates(unittest.TestCase):
    def test_explicit_model_aliases_override_title_without_guessing_power(self):
        for label in ("Modellname / -kennzeichen:", "Modellname/-kennzeichen", "Modellkennung"):
            result = parse_specs(specs("1", name="FLYSTAR 800W/400W Trockner",
                                       extra={"Modelkennung": None, label: "T3323P474522"}), "1")
            self.assertEqual("T3323P474522", result["sku"])
            self.assertEqual("spec", result["_sources"]["sku"]["source"])

    def test_power_and_function_tokens_are_not_skus_but_real_models_survive(self):
        for name in ("FLYSTAR Mini 800W/400W Trockner", "BRAND 1200W 3-in-1 Trockner"):
            result = parse_specs(specs("1", name=name, extra={"Modelkennung": None}), "1")
            self.assertIsNone(result["sku"])
        for value in ("800W", "400 W", "800W/400W", "800W-1200W", "3-in-1", "8kg"):
            self.assertIsNone(valid_sku(value))
        for value in ("BMS3T382CW", "WQA233CE1", "DV80DG52B0TEEG", "WA56110-021A"):
            self.assertEqual(value, valid_sku(value))
        result = parse_specs(specs("1", name="BRAND 1200W REALMODEL123 Trockner",
                                   extra={"Modelkennung": None}), "1")
        self.assertEqual("REALMODEL123", result["sku"])

    def test_title_first_kg_before_g_and_no_conversion(self):
        for title, expected in (("DEBUY 4kg Trockner (3000 g)", "4kg"),
                                ("Trockner (3000 g)", "3000g"),
                                ("Trockner (7,5 kg)", "7,5kg")):
            result = parse_specs(specs("1", name=title, extra={"Nennkapazität": "3 kg"}), "1")
            self.assertEqual(expected, result["capacity"])
            self.assertEqual("title", result["_sources"]["capacity"]["source"])

    def test_separate_spec_unit_and_label_unit_are_preserved(self):
        for value, unit, label, expected in (("3000", "g", "Füllmenge Baumwolle (Trocknen)", "3000g"),
                                             ("3", "kg", "Nennkapazität", "3kg"),
                                             ("3", None, "Füllmenge Baumwolle (Trocknen) in kg", "3kg")):
            payload = specs("1", extra={"Nennkapazität": None})
            features = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]["productAggregate"]["product"]["featureGroupsWithProductId"]["featureGroups"][0]["features"]
            features.append({"name": label, "value": value, "unit": unit})
            result = parse_specs(payload, "1")
            self.assertEqual(expected, result["capacity"])
            self.assertEqual(value, result["_sources"]["capacity"]["raw_value"])
            self.assertEqual(unit, result["_sources"]["capacity"]["raw_unit"])

    def test_spec_kg_wins_over_g_and_weight_or_unitless_is_not_capacity(self):
        result = parse_specs(specs("1", extra={"Füllmenge Baumwolle (Trocknen)": "3000 g",
                                               "Nennkapazität": "3 kg"}), "1")
        self.assertEqual("3kg", result["capacity"])
        result = parse_specs(specs("1", name="MODEL123 Trockner Gewicht: 20kg",
                                   extra={"Nennkapazität": "3", "Gewicht": "20 kg"}), "1")
        self.assertIsNone(result["capacity"])

    def test_listing_title_precedes_specs_and_loading_requires_evidence(self):
        result = parse_specs(specs("1", name="MODEL123 Trockner 7 kg"), "1", "MODEL123 Toplader Trockner 4kg")
        self.assertEqual(("4kg", "Top load"), (result["capacity"], result["loading_type"]))
        result = parse_specs(specs("1", extra={"Beladung": None}), "1")
        self.assertIsNone(result["loading_type"])

    def test_portabel_is_title_only_and_not_extended_to_other_words(self):
        for name in ("Portabel Trockner", "PORTABEL Trockner", "Trockner portabel"):
            self.assertEqual("portabel_title", exclusion_reason({"retailer_sku_name": name}))
        for name in ("Mini Trockner 3kg", "Portable Wäschetrockner", "Tragbar Wäschetrockner"):
            self.assertIsNone(exclusion_reason({"retailer_sku_name": name}))
        self.assertIsNone(exclusion_reason({"retailer_sku_name": "Trockner"}, features={"Bauform": "Portabel"}))
        self.assertEqual("portabel_title", exclusion_reason({"retailer_sku_name": "Trockner"}, detail_name="Trockner Portabel"))


class DatabaseUpdates(unittest.TestCase):
    def setUp(self):
        self.connection, self.cursor = fake_db()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(database, "connect", return_value=self.connection))
        self.stack.enter_context(patch.object(database, "table_metadata", return_value=schema()))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.addCleanup(self.stack.close)

    def test_latest_nonnull_is_selected_independently_and_input_is_unchanged(self):
        original = [db_row()]
        self.cursor.fetchmany.side_effect = [[
            ("SEG", "Mediamarkt", "1", None, "Front load", None),
            ("SEG", "Mediamarkt", "1", "800W/400W", None, "3000g"),
            ("SEG", "Mediamarkt", "1", "REALMODEL123", "Top load", "4kg"),
        ]]
        result = database.inspect_or_insert(config.DB_TABLE, original)
        values = self.cursor.executemany.call_args.args[1][0]
        inserted = dict(zip(config.COLUMNS[1:], values))
        self.assertEqual(("REALMODEL123", "Front load", "3000g"),
                         tuple(inserted[field] for field in database.BACKFILL_FIELDS))
        self.assertEqual(dict.fromkeys(database.BACKFILL_FIELDS, 1), result["backfilled"])
        self.assertTrue(all(original[0][field] is None for field in database.BACKFILL_FIELDS))
        statement = str(self.cursor.execute.call_args.args[0])
        self.assertIn("crawl_datetime DESC NULLS LAST, id DESC", statement)
        self.assertEqual(["SEG", "Mediamarkt", "1"], self.cursor.execute.call_args.args[1])

    def test_fresh_values_win_and_other_fields_are_never_backfilled(self):
        row = db_row(sku="FRESH123", capacity="4kg")
        self.cursor.fetchmany.side_effect = [[("SEG", "Mediamarkt", "1", "OLDMODEL123", "Top load", "3kg")]]
        result = database.inspect_or_insert(config.DB_TABLE, [row])
        inserted = dict(zip(config.COLUMNS[1:], self.cursor.executemany.call_args.args[1][0]))
        self.assertEqual(("FRESH123", "Top load", "4kg"), tuple(inserted[k] for k in database.BACKFILL_FIELDS))
        self.assertIsNone(inserted["final_sku_price"])
        self.assertIsNone(inserted["count_of_reviews"])
        self.assertEqual({"sku": 0, "loading_type": 1, "capacity": 0}, result["backfilled"])

    def test_missing_history_stays_null_and_other_product_is_not_used(self):
        self.cursor.fetchmany.side_effect = [[("OTHER", "Mediamarkt", "1", "OTHER123", "Top load", "8kg")], []]
        database.inspect_or_insert(config.DB_TABLE, [db_row()])
        inserted = dict(zip(config.COLUMNS[1:], self.cursor.executemany.call_args.args[1][0]))
        self.assertTrue(all(inserted[field] is None for field in database.BACKFILL_FIELDS))

    def test_same_batch_retry_skips_insert_and_partial_batch_is_rejected(self):
        self.cursor.fetchall.return_value = [("SEG", "Mediamarkt", "1")]
        result = database.inspect_or_insert(config.DB_TABLE, [db_row()])
        self.assertTrue(result["already_loaded"])
        self.assertEqual(0, result["inserted"])
        self.cursor.executemany.assert_not_called()
        self.assertIn("pg_advisory_xact_lock", self.cursor.execute.call_args_list[0].args[0])
        with self.assertRaisesRegex(database.DatabaseError, "DB_BATCH_PARTIAL_OR_CONFLICT"):
            database.inspect_or_insert(config.DB_TABLE, [db_row(), db_row("2")])
        self.cursor.executemany.assert_not_called()

    def test_query_error_blocks_insert_and_commit_error_is_unknown(self):
        self.cursor.fetchmany.side_effect = RuntimeError("RAW_DB_ERROR_MUST_NOT_PRINT")
        with self.assertRaises(database.DatabaseError) as caught:
            database.inspect_or_insert(config.DB_TABLE, [db_row()])
        self.assertEqual("DB_BACKFILL_FAILED", str(caught.exception))
        self.assertFalse(caught.exception.db_written)
        self.cursor.executemany.assert_not_called()
        self.cursor.fetchmany.side_effect = None
        self.cursor.fetchmany.return_value = []
        self.connection.__exit__.side_effect = RuntimeError("RAW_COMMIT_ERROR_MUST_NOT_PRINT")
        with self.assertRaises(database.DatabaseError) as caught:
            database.inspect_or_insert(config.DB_TABLE, [db_row()])
        self.assertEqual("DB_COMMIT_FAILED", str(caught.exception))
        self.assertIsNone(caught.exception.db_written)

    def test_connection_failure_has_safe_stage_code_and_does_not_insert(self):
        with patch.object(database, "connect", side_effect=RuntimeError("RAW_CONNECTION_ERROR_MUST_NOT_PRINT")):
            with self.assertRaises(database.DatabaseError) as caught:
                database.inspect_or_insert(config.DB_TABLE, [db_row()])
        self.assertEqual("DB_CONNECT_FAILED", str(caught.exception))
        self.assertFalse(caught.exception.db_written)
        self.cursor.executemany.assert_not_called()

    def test_failure_after_confirmed_commit_keeps_written_true(self):
        def event(name, **fields):
            if name == "db_complete":
                raise OSError("RAW_LOG_ERROR_MUST_NOT_PRINT")
        with patch.object(database, "emit", side_effect=event):
            with self.assertRaises(database.DatabaseError) as caught:
                database.inspect_or_insert(config.DB_TABLE, [db_row()])
        self.assertTrue(caught.exception.db_written)
        self.assertEqual("DB_COMMITTED_FAILED", str(caught.exception))

    def test_selected_production_table_is_used_for_history_and_insert(self):
        database.inspect_or_insert(config.PRODUCTION_TABLE, [db_row()])
        self.assertIn("dx_seg_ldy_dryer_retail", str(self.cursor.execute.call_args.args[0]))
        self.assertNotIn("retail_test", str(self.cursor.execute.call_args.args[0]))
        self.assertIn("dx_seg_ldy_dryer_retail", str(self.cursor.executemany.call_args.args[0]))


class ExecutionAndLogs(unittest.TestCase):
    def browser(self):
        browser = Browser({("main", 1): page_html([("9", "PORTABEL Trockner")], shown=1, total=2, more=True),
                           ("main", 2): page_html([("1", "Mini Trockner 4kg")], shown=2, total=2),
                           ("bsr", 1): page_html([("1", "Mini Trockner 4kg")], shown=1, total=1)})
        browser.open = Mock()
        browser.close = Mock()
        return browser

    def execute(self, root, browser, mode=(), db_result=None, db_error=None):
        args = ["--output-dir", str(root / "output"), "--log-dir", str(root / "logs"),
                "--target", "1", "--bsr-target", "1", "--render-settle", "0", "--sleep", "0", *mode]
        with patch("common.uc.UcSession", return_value=browser) as uc, \
                patch.object(run, "inspect_or_insert", side_effect=db_error, return_value=db_result or {
                    "inserted": 1, "already_loaded": False, "backfilled": dict.fromkeys(database.BACKFILL_FIELDS, 0)}
                ) as db, contextlib.redirect_stdout(io.StringIO()) as screen:
            code = run.main(args)
        logs = [json.loads(line) for path in sorted((root / "logs").glob("*.log")) for line in path.read_text(encoding="utf-8").splitlines()]
        return code, db, uc, logs, screen.getvalue()

    def test_default_collects_then_loads_test_and_logs_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root, browser = Path(directory), self.browser()
            code, db, _, logs, screen = self.execute(root, browser)
            self.assertEqual(0, code)
            self.assertEqual(config.DB_TABLE, db.call_args.args[0])
            self.assertEqual("4kg", db.call_args.args[1][0]["capacity"])
            self.assertEqual(2, len(browser.calls))
            self.assertNotIn("9", {variables.get("ref") or variables.get("id") for _, variables in browser.calls})
            manifest = json.loads((root / "output/mmkt_dry_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["success"])
            self.assertTrue(manifest["database"]["db_written"])
            self.assertEqual({"run_start", "page_start", "page_ready", "product_excluded", "field_selected",
                              "product_accepted", "listing_complete", "csv_saved", "run_complete"},
                             {e["event"] for e in logs} & {"run_start", "page_start", "page_ready", "product_excluded",
                              "field_selected", "product_accepted", "listing_complete", "csv_saved", "run_complete"})
            self.assertEqual(1, next(e for e in logs if e["event"] == "product_accepted")["accepted"])
            self.assertIn('"reason": "portabel_title"', screen)
            browser.close.assert_called_once()

    def test_collect_only_never_connects_and_load_only_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, _, _ = self.execute(root, self.browser(), mode=("--collect-only",))
            self.assertEqual(0, code)
            db.assert_not_called()
            csv_path = root / "output/mmkt_dry_output.csv"
            digest = database.file_digest(csv_path)
            code, db, uc, _, _ = self.execute(root, self.browser(), mode=("--write-db",))
            self.assertEqual(0, code)
            uc.assert_not_called()
            self.assertEqual(1, db.call_count)
            self.assertEqual(digest, database.file_digest(csv_path))

    def test_failed_load_preserves_validated_csv_and_collection_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, _, _, logs, screen = self.execute(root, self.browser(),
                db_error=database.DatabaseError("DB_COMMIT_FAILED", db_written=None))
            self.assertEqual(1, code)
            manifest = json.loads((root / "output/mmkt_dry_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["success"])
            self.assertIsNone(manifest["database"]["db_written"])
            self.assertEqual(1, len(database.load_validated_output(root / "output/mmkt_dry_output.csv")))
            end = [e for e in logs if e["event"] == "run_complete"][-1]
            self.assertTrue(end["collection_success"])
            self.assertIsNone(end["db_written"])

    def test_failed_api_does_not_load_db_or_report_short_category(self):
        browser = self.browser()
        browser.gql = Mock(return_value={"status": 403, "raw": "RAW_API_RESPONSE_MUST_NOT_PRINT"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, logs, screen = self.execute(root, browser)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertNotIn("RAW_API_RESPONSE_MUST_NOT_PRINT", screen)
            self.assertFalse((root / "output/mmkt_dry_output.csv").exists())
            self.assertFalse(logs[-1]["collection_success"])
            self.assertTrue(any(e.get("http") == 403 for e in logs))

    def test_invalid_csv_does_not_connect_or_claim_collection_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, self.browser(), mode=("--collect-only",))
            (root / "output/mmkt_dry_output.csv").write_text("modified", encoding="utf-8")
            code, db, uc, logs, _ = self.execute(root, self.browser(), mode=("--write-db",))
            self.assertEqual(1, code)
            db.assert_not_called()
            uc.assert_not_called()
            end = [e for e in logs if e["event"] == "run_complete"][-1]
            self.assertFalse(end["collection_success"])

    def test_committed_load_is_not_reported_unwritten_when_manifest_save_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.execute(root, self.browser(), mode=("--collect-only",))
            original_write = run.write_json
            def write(path, value):
                if value.get("database", {}).get("state") == "success":
                    raise OSError("RAW_FILE_ERROR_MUST_NOT_PRINT")
                original_write(path, value)
            with patch.object(run, "write_json", side_effect=write):
                code, _, _, logs, screen = self.execute(root, self.browser(), mode=("--write-db",))
            self.assertEqual(1, code)
            end = [e for e in logs if e["event"] == "run_complete"][-1]
            self.assertTrue(end["db_written"])
            self.assertTrue(end["db_success"])
            self.assertEqual("DB_COMMITTED_MANIFEST_SAVE_FAILED", end["error"])
            self.assertNotIn("RAW_FILE_ERROR", screen)

    def test_interrupt_is_logged_and_browser_is_closed_without_db_load(self):
        browser = self.browser()
        browser.gql = Mock(side_effect=KeyboardInterrupt())
        with tempfile.TemporaryDirectory() as directory:
            code, db, _, logs, _ = self.execute(Path(directory), browser)
            self.assertEqual(130, code)
            db.assert_not_called()
            browser.close.assert_called_once()
            self.assertEqual("KeyboardInterrupt", logs[-1]["error"])

    def test_safe_log_discards_library_output_and_raw_exception(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as screen:
            with RunLog(Path(directory), heartbeat_s=.01) as log:
                with quiet_external():
                    print("RAW_EXTERNAL_MUST_NOT_PRINT")
                    emit("public_progress", accepted=7, target=100)
                try:
                    raise RuntimeError("RAW_EXCEPTION_MUST_NOT_PRINT")
                except RuntimeError as exc:
                    failure(exc, code="SAFE_ERROR")
                import time
                deadline = time.monotonic() + 1
                while "waiting" not in screen.getvalue() and time.monotonic() < deadline:
                    time.sleep(.01)
            raw = log.path.read_text(encoding="utf-8")
            self.assertNotIn("MUST_NOT_PRINT", raw + screen.getvalue())
            self.assertIn('"accepted": 7', raw)
            self.assertIn('"event": "waiting"', raw)
            self.assertIn('"code": "SAFE_ERROR"', raw)


if __name__ == "__main__":
    unittest.main()
