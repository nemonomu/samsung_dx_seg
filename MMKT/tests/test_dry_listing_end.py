"""Observed pagination signals, checkpoint safety and failure reports, offline."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from common.parsers import extract_preloaded_state

from test_dry_pipeline import Browser, page_html, specs
from dry import checkpoint, config, database, notify, run
from dry.detail import CollectionError
from dry.listing import collect_pages
from dry.output import write_output


def block(ids, *, total=None, more=False, shown=None, ads=()):
    return page_html([(str(i), "Trockner") for i in ids], shown=shown, total=total, more=more, ads=ads)


class PaginationEvidenceTests(unittest.TestCase):
    def collect(self, pages, *, eligible=None, retries=2):
        requested = []
        def fetch(page):
            requested.append(page)
            value = pages[page]
            return value if isinstance(value, tuple) else (value, 200, 0, None, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            rows, logs, reason = collect_pages(fetch, target=300, retries=retries,
                                               eligibility=eligible or (lambda row: None))
        return rows, logs, reason, requested

    def test_original_signals_are_reproduced_without_inventing_page_metadata(self):
        # Observed log: 22 blocks of 12, 11-row tail, missing end counter,
        # then the identical tail again. This is synthetic HTML, not raw capture.
        for more in (False, True):
            # Earlier has_next=True can come from the counter alone. The result
            # must not depend on inventing an earlier enabled button.
            pages = {p: block(range((p-1)*12+1, p*12+1), total=275, shown=12, more=more)
                     for p in range(1, 23)}
            pages[23] = block(range(265, 276))
            pages[24] = pages[23]
            rows, logs, reason, requested = self.collect(pages)
            self.assertEqual((275, "last_page", 24), (len(rows), reason, requested[-1]))
            self.assertEqual(list(range(1, 25)), requested)
            self.assertEqual((None, None, None), tuple(logs[-2][key] for key in ("shown", "total", "has_next")))
            self.assertTrue(logs[-2]["end_candidate"])
            self.assertEqual("stable_total_slots_and_verified_tail_repeat", logs[-1]["end_basis"])
            self.assertFalse(logs[-1]["raw_last_page"])
            self.assertTrue(logs[-1]["last_page"])
            self.assertEqual((23, 275, 0), (logs[-1]["end_for_page"], logs[-1]["category_slots_read"], logs[-1]["new_unique"]))

    def test_full_size_tail_and_different_block_width_are_supported(self):
        for total in (8, 10):
            pages = {1: block(range(1, 5), total=total, shown=4, more=True)}
            if total == 8:
                pages[2] = block(range(5, 9))
                pages[3] = pages[2]
            else:
                pages[2] = block(range(5, 9), total=total, shown=4, more=True)
                pages[3] = block(range(9, 11))
                pages[4] = pages[3]
            rows, logs, reason, _ = self.collect(pages)
            self.assertEqual((total, "last_page", 4), (len(rows), reason, logs[-1]["observed_block_size"]))

    def test_middle_repeat_and_unknown_total_remain_failures(self):
        for total in (8, 20, None):
            first = block(range(1, 5), total=total, shown=4 if total else None, more=bool(total))
            rows, logs, reason, requested = self.collect({1: first, 2: block(range(1, 5))})
            self.assertEqual((4, "repeated_page"), (len(rows), reason))
            self.assertEqual([1, 2, 2, 2], requested)
            self.assertFalse(logs[-1]["end_basis"])

    def test_small_incomplete_block_and_visible_more_control_cannot_end(self):
        for tail in (block(range(5, 8)), block(range(5, 9), more=True)):
            pages = {1: block(range(1, 5), total=8, shown=4, more=True), 2: tail, 3: tail}
            _, logs, reason, _ = self.collect(pages)
            self.assertEqual("repeated_page", reason)
            self.assertFalse(logs[-2]["end_candidate"])
            self.assertFalse(logs[-1]["end_basis"])

    def test_changed_total_never_uses_stale_total_as_end_evidence(self):
        for tail in (block(range(5, 9), total=9, shown=4),
                     block(range(5, 9), total=8, shown=8)):
            _, logs, reason, _ = self.collect({1: block(range(1, 5), total=10, shown=4, more=True),
                                             2: tail, 3: tail})
            self.assertNotEqual("last_page", reason)
            self.assertTrue(logs[-1]["total_changed"])
            self.assertFalse(logs[-1]["end_basis"])

    def test_partial_dom_cannot_verify_complete_preloaded_state(self):
        tail = block(range(5, 9))
        soup = BeautifulSoup(tail, "html.parser")
        soup.select_one("li").decompose()
        tail = str(soup)
        rows, _, reason, _ = self.collect({1: block(range(1, 5), total=8, shown=4, more=True), 2: tail, 3: tail})
        self.assertEqual((8, "repeated_page"), (len(rows), reason))

    def test_failed_or_malformed_probe_never_confirms_a_tail(self):
        for response in (("BLOCKED", 403, 0, None, 0), ("bad HTML", 200, 0, None, 0),
                         ("", 200, 0, "navigation_failed", 0)):
            rows, logs, reason, _ = self.collect({1: block(range(1, 5), total=8, shown=4, more=True),
                                                2: block(range(5, 9)), 3: response})
            self.assertEqual(8, len(rows))
            self.assertNotEqual("last_page", reason)
            self.assertFalse(logs[-1]["end_basis"])

    def test_heading_total_change_during_missing_counter_blocks_tail_confirmation(self):
        tail = '<h1>Trockner (9 Artikel)</h1>' + block(range(5, 9))
        rows, logs, reason, _ = self.collect({1: block(range(1, 5), total=8, shown=4, more=True), 2: tail, 3: tail})
        self.assertEqual((8, "repeated_page"), (len(rows), reason))
        self.assertTrue(logs[-1]["total_changed"])

    def test_counter_alone_cannot_claim_more_rows_than_were_read(self):
        for shown, total, reason in ((8, 8, "counter_slot_mismatch"), (0, 0, "zero_total_with_products")):
            _, _, stop, _ = self.collect({1: block(range(1, 5), shown=shown, total=total)})
            self.assertEqual(reason, stop)
        html = block(range(1, 5), shown=4, total=4).rsplit('</div>', 1)[0]
        html += '<button disabled>weitere Produkte anzeigen</button><button>weitere Produkte anzeigen</button></div>'
        _, _, stop, _ = self.collect({1: html})
        self.assertEqual("counter_next_control_conflict", stop)

    def test_banners_and_eligibility_do_not_change_category_slot_accounting(self):
        pages = {1: block(range(1, 5), total=8, shown=4, more=True, ads=(("900", "Trockner"),)),
                 2: block(range(5, 9), ads=(("901", "Trockner"),))}
        pages[3] = pages[2]
        def eligible(row):
            if row["listing_card_type"] == "sponsored-ad": return "advertisement"
            return "washing_machine" if row["sku_id"] in {"2", "6"} else None
        rows, logs, reason, _ = self.collect(pages, eligible=eligible)
        self.assertEqual((6, "last_page", 8), (len(rows), reason, logs[-1]["category_slots_read"]))
        self.assertTrue(all(row["sku_status"] == "Sponsored" for row in rows))

    def test_standard_sponsored_extras_are_included_without_inflating_category_slots(self):
        tail = block(range(5, 9))
        extra = '<li><span>Gesponsert</span><a href="/de/product/_test-990.html">Trockner</a></li>'
        tail = tail.replace('</ul>', extra + '</ul>')
        probe = tail.replace('990', '991')  # an ad may rotate on the out-of-range probe
        rows, logs, reason, _ = self.collect({1: block(range(1, 5), total=8, shown=4, more=True), 2: tail, 3: probe})
        self.assertEqual((9, "last_page", 8), (len(rows), reason, logs[-1]["category_slots_read"]))
        self.assertIn("990", {row["sku_id"] for row in rows})
        self.assertNotIn("991", {row["sku_id"] for row in rows})

    def test_nonproduct_state_entries_and_multiple_page_objects_follow_existing_parser(self):
        def split(html):
            state = extract_preloaded_state(html)
            apollo = state["apolloState"]
            entries = apollo["page"]["products"]
            apollo["page"]["products"] = entries[:2] + [{}]
            apollo["next_part"] = {"__typename": "ProductListPage", "products": entries[2:]}
            soup = BeautifulSoup(html, "html.parser")
            soup.find('script').string = 'window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';'
            return str(soup)
        tail = split(block(range(5, 9)))
        rows, logs, reason, _ = self.collect({1: split(block(range(1, 5), total=8, shown=4, more=True)), 2: tail, 3: tail})
        self.assertEqual((8, "last_page", 8), (len(rows), reason, logs[-1]["category_slots_read"]))
        self.assertEqual((2, 1), (logs[-1]["state_page_objects"], logs[-1]["ignored_state_entries"]))

    def test_zero_category_count_does_not_exclude_an_extra_standard_sponsored_card(self):
        html = block((), shown=0, total=0)
        extra = '<li><span>Gesponsert</span><a href="/de/product/_test-990.html">Trockner</a></li>'
        html = html.replace('</ul>', extra + '</ul>')
        rows, logs, reason, _ = self.collect({1: html})
        self.assertEqual((["990"], "last_page", 0),
                         ([row["sku_id"] for row in rows], reason, logs[-1]["category_slots_read"]))

    def test_zero_heading_requires_empty_state_and_no_active_more_control(self):
        empty = '<h1>Trockner (0 Artikel)</h1>' + block(())
        rows, logs, reason, _ = self.collect({1: empty})
        self.assertEqual(([], "last_page"), (rows, reason))
        self.assertEqual("zero_category_heading_and_empty_state", logs[-1]["end_basis"])
        _, _, reason, _ = self.collect({1: block(())})
        self.assertEqual("parse_failed", reason)


class ProgressPreservationTests(unittest.TestCase):
    def execute(self, root, browser, *, options=()):
        browser.open, browser.close = Mock(), Mock()
        args = ["--output-dir", str(root / "output"), "--log-dir", str(root / "logs"),
                "--sleep", "0", "--render-settle", "0", "--sponsored-extra-wait", "0", *options]
        def insert(table, rows):
            return {"inserted": len(rows), "already_loaded": False,
                    "backfilled": {"sku": 0, "loading_type": 0, "capacity": 0}}
        with patch("common.uc.UcSession", return_value=browser) as uc, \
                patch.object(run, "inspect_or_insert", side_effect=insert) as db, \
                patch.object(notify, "env_value", return_value="0"), \
                patch.object(notify, "_send", side_effect=AssertionError("SMTP forbidden")) as smtp, \
                contextlib.redirect_stdout(io.StringIO()) as screen:
            code = run.main(args)
        smtp.assert_not_called()
        manifest = json.loads((root / "output/mmkt_dry_manifest.json").read_text(encoding="utf-8"))
        return code, db, uc, manifest, screen.getvalue()

    def short_browser(self):
        return Browser({("main", 1): block(range(1, 5), total=10, shown=4, more=True),
                        ("main", 2): block(range(5, 9), total=10, shown=4, more=True),
                        ("main", 3): block(range(9, 11)), ("main", 4): block(range(9, 11)),
                        ("bsr", 1): block(range(1, 11), total=10, shown=10)})

    def test_short_category_continues_through_bsr_merge_and_production_insert(self):
        browser = self.short_browser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, manifest, _ = self.execute(root, browser)
            rows = database.load_validated_output(root / "output/mmkt_dry_output.csv")
            self.assertEqual((0, 10, 10), (code, manifest["main"]["rows"], manifest["bsr"]["rows"]))
            self.assertEqual((3, 4), (manifest["main"]["last_successful_page"], manifest["main"]["last_requested_page"]))
            self.assertEqual(config.PRODUCTION_TABLE, db.call_args.args[0])
            self.assertEqual(10, manifest["database"]["inserted"])
            self.assertTrue(manifest["notification"]["success_report"])
            self.assertEqual(20, len(browser.calls))
            self.assertEqual([(str(i), str(i)) for i in range(1, 11)], [(row["main_rank"], row["bsr_rank"]) for row in rows])
            paths = sorted((root / "output/intermediate" / manifest["batch_id"]).glob("*.json"))
            self.assertEqual(5, len(paths))
            for path in paths:
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(saved["success"])
                self.assertFalse(saved["loadable"])
                self.assertEqual("intermediate", saved["artifact_kind"])
                self.assertTrue(all(tuple(row) == config.COLUMNS for row in saved["rows"]))

    def test_logged_counts_reach_bsr_and_db_while_preserving_individual_failure(self):
        # Count-only fixture transcribed from the supplied page_summary events.
        # Product IDs/names below are synthetic; no original product data is recovered.
        summaries = [(12, {}), (11, {"washer_dryer": 1}), (10, {"washer_dryer": 2}), (12, {}),
            (7, {"duplicate": 1, "washing_machine": 2, "washer_dryer": 2}),
            (4, {"washer_dryer": 3, "washing_machine": 4, "set_or_combo": 1}),
            (7, {"washer_dryer": 4, "portabel_title": 1}), (9, {"washer_dryer": 1, "washing_machine": 2}),
            (11, {"washer_dryer": 1}), (8, {"duplicate": 1, "washer_dryer": 3}),
            (9, {"washer_dryer": 2, "washing_machine": 1}), (11, {"washer_dryer": 1}),
            (9, {"washer_dryer": 3}), (7, {"washer_dryer": 5}), (11, {"washer_dryer": 1}),
            (8, {"washer_dryer": 3, "washing_machine": 1}), (9, {"washer_dryer": 3}),
            (9, {"washer_dryer": 3}), (12, {}),
            (6, {"duplicate": 1, "washer_dryer": 3, "washing_machine": 2}),
            (8, {"washer_dryer": 4}), (7, {"washer_dryer": 4, "product_failed": 1}), (8, {"washer_dryer": 3})]
        labels = {"washer_dryer": "Waschtrockner", "washing_machine": "Waschmaschine",
                  "set_or_combo": "Trockner Set", "portabel_title": "Portabel Trockner", "product_failed": "UNKNOWNMODEL"}
        pages, accepted, unknown, next_id = {}, [], set(), 1
        for page, (good, excluded) in enumerate(summaries, 1):
            products = []
            for _ in range(good):
                products.append((str(next_id), "Trockner")); accepted.append(str(next_id)); next_id += 1
            for reason, count in excluded.items():
                for _ in range(count):
                    if reason == "duplicate": products.append((accepted[0], "Trockner"))
                    else:
                        products.append((str(next_id), labels[reason]))
                        if reason == "product_failed": unknown.add(str(next_id))
                        next_id += 1
            html = page_html(products, shown=12 if page < 23 else None, total=275)
            html = html.replace('<span>Gesponsert</span>', '')
            if page == 1:  # observed first capture: 3 rendered rows, 12 state rows
                soup = BeautifulSoup(html, "html.parser")
                for card in soup.select('li')[3:]: card.decompose()
                html = str(soup)
            pages["main", page] = html
        pages["main", 24] = pages["main", 23]
        for page in range(1, 10):
            ids = accepted[(page-1)*12:page*12]
            pages["bsr", page] = page_html([(pid, "Trockner") for pid in ids], shown=12, total=275, more=True)
        browser = Browser(pages)
        original = browser.gql
        def gql(operation, variables):
            response = original(operation, variables)
            if operation == "GetComparisonTableRecommendations" and variables["ref"] in unknown:
                response["data"] = specs(variables["ref"], product_type="Unbekannt", name="UNKNOWNMODEL")
            return response
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, manifest, screen = self.execute(root, browser)
            self.assertEqual((1, 205, 100, 205), (code, manifest["main"]["rows"], manifest["bsr"]["rows"], manifest["output_rows"]))
            self.assertEqual((411, 1, 3), (manifest["api_calls"], manifest["product_failure_count"], manifest["main"]["duplicates"]))
            self.assertEqual((3, 12), (manifest["pages"]["main"][0]["rendered_listing_rows"],
                                      manifest["pages"]["main"][0]["state_listing_rows"]))
            self.assertEqual(66, sum(v for k, v in manifest["main"]["excluded"].items() if k != "duplicate"))
            self.assertEqual("last_page", manifest["main"]["stop_reason"])
            self.assertEqual("target_reached", manifest["bsr"]["stop_reason"])
            self.assertEqual(205, manifest["database"]["inserted"])
            self.assertTrue(manifest["database"]["db_written"])
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertEqual(205, len(db.call_args.args[1]))
            report = (root / "output/mmkt_dry_email_report.txt").read_text(encoding="utf-8")
            self.assertIn("main: 205/300 (last_page)", report)
            self.assertIn("Product failures (per listing): 1", report)
            self.assertIn('"event": "intermediate_saved"', screen)

    def failing_browser(self):
        browser = Browser({("main", 1): page_html([("1", "Trockner"), ("2", "UNKNOWNMODEL")], shown=2, total=10, more=True),
                           ("main", 2): page_html([("1", "Trockner"), ("2", "UNKNOWNMODEL")], shown=None)})
        original = browser.gql
        def gql(operation, variables):
            response = original(operation, variables)
            if operation == "GetComparisonTableRecommendations" and variables["ref"] == "2":
                response["data"] = specs("2", product_type="Unbekannt", name="UNKNOWNMODEL")
            return response
        browser.gql = gql
        return browser

    def test_real_listing_failure_preserves_counts_checkpoint_and_not_started_bsr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, manifest, _ = self.execute(root, self.failing_browser())
            self.assertEqual((1, 1, 1), (code, manifest["main"]["rows"], manifest["product_failure_count"]))
            self.assertFalse(manifest["success"])
            self.assertEqual(("failed", "not_started", None), (manifest["main"]["state"], manifest["bsr"]["state"], manifest["bsr"]["rows"]))
            self.assertEqual((1, 2), (manifest["main"]["last_successful_page"], manifest["main"]["last_requested_page"]))
            self.assertEqual("not_created", manifest["csv"]["state"])
            db.assert_not_called()
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(["1"], [r["item"] for r in saved["rows"]])
            self.assertEqual(1, saved["progress"]["product_failure_count"])
            report = (root / "output/mmkt_dry_email_report.txt").read_text(encoding="utf-8")
            self.assertIn("main: 1/300 (repeated_page) state=failed", report)
            self.assertIn("bsr: not started/100", report)
            self.assertIn("DB state: not_started", report)
            self.assertNotIn("Collected unique products: 0", report)
            _, db, uc, _, _ = self.execute(root, self.failing_browser(), options=("--write-db",))
            db.assert_not_called(); uc.assert_not_called()

    def test_checkpoint_save_failure_keeps_prior_snapshot_and_reports_actual_counts(self):
        original = checkpoint.write_manifest
        def save(path, payload):
            if path.name.startswith("main_0002"):
                raise OSError("RAW_CHECKPOINT_ERROR_MUST_NOT_PRINT")
            return original(path, payload)
        with tempfile.TemporaryDirectory() as directory, patch.object(checkpoint, "write_manifest", side_effect=save):
            code, db, _, manifest, screen = self.execute(Path(directory), self.short_browser())
            self.assertEqual((1, 8, "INTERMEDIATE_SAVE_FAILED"), (code, manifest["main"]["rows"], manifest["error"]))
            db.assert_not_called()
            self.assertEqual("failed", manifest["intermediate"]["state"])
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(4, len(saved["rows"]))
            self.assertNotIn("RAW_CHECKPOINT_ERROR", screen + json.dumps(manifest))

    def test_final_csv_and_manifest_failure_keep_intermediate_and_never_load(self):
        for stage in ("csv", "manifest"):
            original = run.write_json
            def write(path, value):
                if value.get("success"):
                    raise OSError("RAW_FINAL_WRITE_ERROR_MUST_NOT_PRINT")
                return original(path, value)
            with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                if stage == "csv": stack.enter_context(patch.object(run, "write_output", side_effect=OSError("RAW_FINAL_WRITE_ERROR_MUST_NOT_PRINT")))
                else: stack.enter_context(patch.object(run, "write_json", side_effect=write))
                root = Path(directory)
                code, db, _, manifest, screen = self.execute(root, self.short_browser())
                self.assertEqual((1, 10, 10), (code, manifest["main"]["rows"], manifest["bsr"]["rows"]))
                self.assertFalse(manifest["success"])
                self.assertEqual("FINAL_CSV_SAVE_FAILED" if stage == "csv" else "FINAL_MANIFEST_SAVE_FAILED", manifest["error"])
                db.assert_not_called()
                saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
                self.assertEqual(10, len(saved["rows"]))
                self.assertFalse(manifest["notification"]["success_report"])
                self.assertNotIn("RAW_FINAL_WRITE_ERROR", screen + json.dumps(manifest))
                with self.assertRaises(CollectionError): database.load_validated_output(root / "output/mmkt_dry_output.csv")

    def test_interrupt_preserves_successful_prefix_inside_current_block(self):
        browser = self.short_browser()
        original = browser.gql
        def gql(operation, variables):
            if variables.get("ref") == "2": raise KeyboardInterrupt()
            return original(operation, variables)
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            code, db, _, manifest, _ = self.execute(Path(directory), browser)
            self.assertEqual((130, 1), (code, manifest["main"]["rows"]))
            db.assert_not_called()
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(["1"], [r["item"] for r in saved["rows"]])

    def test_intermediate_artifact_cannot_be_loaded_even_if_success_flag_is_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, _, manifest, _ = self.execute(root, self.failing_browser())
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            target = root / "renamed/mmkt_dry_output.csv"
            write_output(target, saved["rows"])
            saved.update(success=True, output_rows=len(saved["rows"]), output_digest=database.file_digest(target))
            target.with_name("mmkt_dry_manifest.json").write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaisesRegex(CollectionError, "OUTPUT_INTERMEDIATE_NOT_LOADABLE"):
                database.load_validated_output(target)


if __name__ == "__main__":
    unittest.main()
