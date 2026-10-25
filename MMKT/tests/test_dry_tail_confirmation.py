"""Tail confirmation in the actual DRY CLI, using synthetic pages and services."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from test_dry_pipeline import Browser, page_html
from dry import checkpoint, config, database, notify, run


def block(ids, total=None, *, more=False, names=None, ads=()):
    return page_html([(str(pid), (names or {}).get(str(pid), "Trockner")) for pid in ids],
                     shown=len(ids) if total is not None else None, total=total, more=more, ads=ads)


class ScriptedBrowser(Browser):
    def __init__(self, pages):
        super().__init__(pages)
        self.requests = []
        self.counts = Counter()
        self.open, self.close = Mock(), Mock()

    def navigate(self, url, **kwargs):
        params = parse_qs(urlparse(url).query)
        key = ("bsr" if "sort" in params else "main", int(params.get("page", [1])[0]))
        self.requests.append(key)
        self.counts[key] += 1
        choices = self.pages[key]
        value = choices[min(self.counts[key] - 1, len(choices) - 1)] if isinstance(choices, list) else choices
        if isinstance(value, BaseException):
            raise value
        self.driver.page_source = value
        return {"html": value, "error": None}


class TailPipelineTests(unittest.TestCase):
    def execute(self, root, browser, *, options=(), db_error=None):
        def insert(table, rows):
            if db_error:
                raise db_error
            return {"inserted": len(rows), "already_loaded": False,
                    "backfilled": {"sku": 0, "loading_type": 0, "capacity": 0}}
        args = ["--output-dir", str(root / "output"), "--log-dir", str(root / "logs"),
                "--sleep", "0", "--render-settle", "0", "--sponsored-extra-wait", "0",
                "--captcha-wait", "0", *options]
        with patch("common.uc.UcSession", return_value=browser), \
                patch.object(run, "inspect_or_insert", side_effect=insert) as db, \
                patch.object(notify, "env_value", side_effect=lambda key, default=None:
                             "1" if key == "SEG_EMAIL_NOTIFY" else "0"), \
                patch.object(notify, "_send", return_value=(True, 1, None)) as mail, \
                contextlib.redirect_stdout(io.StringIO()) as screen:
            code = run.main(args)
        manifest = json.loads((root / "output/mmkt_dry_manifest.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in screen.getvalue().splitlines() if line.startswith('{')]
        return code, manifest, db, mail, events

    def short_browser(self):
        return ScriptedBrowser({
            ("main", 1): block((1, 2), 7, more=True),
            ("main", 2): [block((3, 4), 6, more=True), block((3, 4), 5, more=True)],
            ("main", 3): block((5,)), ("main", 4): block((5,)),
            ("bsr", 1): block((2, 1), 2),
        })

    def test_changed_total_short_main_reaches_bsr_csv_db_and_single_success_mail(self):
        browser = self.short_browser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, mail, events = self.execute(root, browser)
            self.assertEqual(0, code)
            self.assertEqual((5, 2, "last_page"),
                (manifest["main"]["rows"], manifest["bsr"]["rows"], manifest["main"]["stop_reason"]))
            self.assertEqual(4, manifest["main"]["tail_confirmation"]["queries"])
            self.assertEqual("confirmed", manifest["main"]["tail_confirmation"]["state"])
            self.assertEqual((10, 5), (len(browser.calls), manifest["database"]["inserted"]))
            self.assertEqual(config.PRODUCTION_TABLE, db.call_args.args[0])
            rows = database.load_validated_output(root / "output/mmkt_dry_output.csv")
            self.assertEqual(5, len(rows))
            self.assertTrue(all(tuple(row) == config.COLUMNS for row in rows))
            self.assertEqual([str(i) for i in range(1, 6)], [r["main_rank"] for r in rows])
            self.assertEqual(["2", "1", "", "", ""], [r["bsr_rank"] for r in rows])
            self.assertEqual(1, mail.call_count)
            self.assertTrue(manifest["notification"]["success_report"])
            self.assertTrue(any(e["event"] == "tail_confirmation_result" for e in events))

    def test_new_following_products_return_to_regular_collection_with_shared_budget(self):
        browser = ScriptedBrowser({
            ("main", 1): block((1, 2), 7, more=True),
            ("main", 2): [block((3, 4), 6, more=True), block((3, 4), 8, more=True)],
            ("main", 3): [block((5, 6)), block((5, 6)), block((5, 6), 8, more=True)],
            ("main", 4): block((7, 8)), ("main", 5): block((7, 8)),
            ("bsr", 1): block((1, 2), 2),
        })
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, mail, events = self.execute(Path(directory), browser)
            self.assertEqual(0, code)
            self.assertEqual((8, 8), (manifest["main"]["rows"], manifest["main"]["tail_confirmation"]["queries"]))
            self.assertEqual(16, len(browser.calls))
            self.assertEqual(8, len(db.call_args.args[1]))
            starts = [e for e in events if e["event"] == "detail_start" and e.get("item") in {"7", "8"}]
            self.assertTrue(starts)
            self.assertTrue(all(e["page"] == 4 and e["sort"] == "main" for e in starts))
            mail.assert_called_once()

    def assert_failure(self, browser, *, options=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, mail, events = self.execute(root, browser, options=options)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertFalse(manifest["notification"]["success_report"])
            mail.assert_called_once()
            self.assertIn("[CHECK]", mail.call_args.args[0])
            self.assertEqual("not_started", manifest["database"]["state"])
            self.assertEqual("not_created", manifest["csv"]["state"])
            self.assertLessEqual(manifest["main"].get("tail_confirmation", {}).get("queries", 0), 8)
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertFalse(saved["loadable"])
            self.assertEqual(manifest["output_rows"], len(saved["rows"]))
            return manifest, events

    def test_larger_or_smaller_latest_total_never_loads_partial_results(self):
        for total in (4, 6):
            with self.subTest(total=total):
                browser = self.short_browser()
                browser.pages["main", 2] = [block((3, 4), 6, more=True), block((3, 4), total, more=True)]
                manifest, _ = self.assert_failure(browser, options=("--page-retries", "20"))
                self.assertEqual((5, "not_started"), (manifest["main"]["rows"], manifest["bsr"]["state"]))
                self.assertEqual(3, manifest["main"]["last_successful_page"])
                self.assertEqual(10, len(browser.calls))

    def test_unstable_reference_uses_at_most_eight_probes(self):
        browser = self.short_browser()
        browser.pages["main", 2] = [block((3, 4), total, more=True) for total in (6, 5, 6, 5, 6)]
        manifest, events = self.assert_failure(browser)
        state = manifest["main"]["tail_confirmation"]
        self.assertEqual((8, "failed", 6, None),
            (state["queries"], state["state"], state["latest_total"], state["confirmed_total"]))
        self.assertEqual(8, sum(e["event"] == "tail_confirmation_read" for e in events))
        self.assertIn("reference_unstable", state["reason"])

    def test_changed_preceding_or_tail_products_are_not_success(self):
        for page in (2, 3):
            with self.subTest(page=page):
                browser = self.short_browser()
                initial = block((3, 4), 6, more=True) if page == 2 else block((5,))
                changed = block((3, 99), 5, more=True) if page == 2 else block((99,))
                browser.pages["main", page] = [initial, changed]
                manifest, _ = self.assert_failure(browser)
                state = manifest["main"]["tail_confirmation"]
                self.assertEqual(["99"], state["unexpected_ids"])
                self.assertEqual("failed", state["state"])

    def test_missing_tail_and_following_dom_cards_are_not_success(self):
        for page in (3, 4):
            with self.subTest(page=page):
                browser = self.short_browser()
                soup = BeautifulSoup(block((5,)), "html.parser")
                soup.select_one("li").decompose()
                browser.pages["main", page] = [block((5,)), str(soup)] if page == 3 else str(soup)
                self.assert_failure(browser)

    def test_blocked_wrong_screen_and_navigation_failures_are_not_success(self):
        for bad in ("BLOCKED", "<h1>Wrong screen</h1>", RuntimeError("RAW_NAVIGATION_MUST_NOT_PRINT")):
            with self.subTest(error_type=type(bad).__name__):
                browser = self.short_browser()
                browser.pages["main", 2] = [block((3, 4), 6, more=True), bad]
                _, events = self.assert_failure(browser)
                self.assertNotIn("RAW_NAVIGATION_MUST_NOT_PRINT", json.dumps(events))

    def test_transient_probe_failure_recovers_without_extra_detail_calls(self):
        browser = self.short_browser()
        browser.pages["main", 2] = [block((3, 4), 6, more=True), RuntimeError("synthetic"),
                                    block((3, 4), 5, more=True)]
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, _, _, _ = self.execute(Path(directory), browser)
            self.assertEqual((0, 5, 10), (code, manifest["main"]["tail_confirmation"]["queries"], len(browser.calls)))

    def test_explicit_changed_end_counter_is_revalidated_even_at_target(self):
        browser = self.short_browser()
        browser.pages["main", 3] = page_html([("5", "Trockner")], shown=5, total=5)
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, _, _, _ = self.execute(Path(directory), browser, options=("--target", "5"))
            self.assertEqual((0, 4), (code, manifest["main"]["tail_confirmation"]["queries"]))

    def test_both_lists_have_separate_confirmation_budgets_and_share_detail_cache(self):
        browser = self.short_browser()
        for (sort, page), values in list(browser.pages.items()):
            if sort == "main":
                browser.pages["bsr", page] = values
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, _, mail, _ = self.execute(Path(directory), browser)
            self.assertEqual((0, 4, 4, 10), (code, manifest["main"]["tail_confirmation"]["queries"],
                manifest["bsr"]["tail_confirmation"]["queries"], len(browser.calls)))
            mail.assert_called_once()

    def test_bsr_confirmation_failure_preserves_completed_main_and_cached_details(self):
        browser = self.short_browser()
        browser.pages.update({("bsr", 1): block((1, 2), 7, more=True),
                              ("bsr", 2): block((3, 4), 6, more=True),
                              ("bsr", 3): block((5,)), ("bsr", 4): block((5,))})
        manifest, _ = self.assert_failure(browser)
        self.assertEqual(("complete", 5, "failed", 5),
            (manifest["main"]["state"], manifest["main"]["rows"], manifest["bsr"]["state"], manifest["bsr"]["rows"]))
        self.assertEqual(10, len(browser.calls))

    def test_probe_snapshots_do_not_overwrite_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest, _, _, events = self.execute(root, self.short_browser())
            folder = root / "output/intermediate" / manifest["batch_id"]
            self.assertEqual(9, len(list(folder.glob("*.json"))))
            regular = json.loads((folder / "main_0003_attempt_01.json").read_text(encoding="utf-8"))
            self.assertEqual((5, "collecting"), (len(regular["rows"]), regular["progress"]["main"]["state"]))
            reads = [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("main_tail_read_*.json")]
            self.assertEqual(4, len(reads))
            self.assertTrue(all(len(value["rows"]) == 5 and not value["loadable"] for value in reads))
            self.assertTrue(all(tuple(row) == config.COLUMNS for value in reads for row in value["rows"]))
            screen_reads = [e for e in events if e["event"] == "tail_confirmation_read"]
            self.assertEqual([2, 3, 4, 2], [e["page"] for e in screen_reads])
            log_events = [json.loads(line) for line in next((root / "logs").glob("*.log")).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(screen_reads, [e for e in log_events if e["event"] == "tail_confirmation_read"])

    def test_probe_checkpoint_failure_preserves_rows_and_never_loads(self):
        original = checkpoint.write_manifest
        def save(path, value):
            if "_tail_read_02_" in path.name:
                raise OSError("RAW_CHECKPOINT_MUST_NOT_PRINT")
            return original(path, value)
        with patch.object(checkpoint, "write_manifest", side_effect=save):
            manifest, events = self.assert_failure(self.short_browser())
        self.assertEqual("INTERMEDIATE_SAVE_FAILED", manifest["error"])
        self.assertEqual("failed", manifest["main"]["tail_confirmation"]["state"])
        self.assertNotIn("RAW_CHECKPOINT_MUST_NOT_PRINT", json.dumps(events))

    def test_confirmed_collection_does_not_hide_insert_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, _, mail, _ = self.execute(root, self.short_browser(),
                db_error=database.DatabaseError("DB_INSERT_FAILED"))
            self.assertEqual((1, "complete", "complete"),
                (code, manifest["main"]["state"], manifest["bsr"]["state"]))
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertTrue((root / "output/mmkt_dry_output.csv").exists())
            self.assertIn("[CHECK]", mail.call_args.args[0])

    def test_observed_count_scenario_reaches_213_rows_and_426_api_calls(self):
        # Count/control transcription only; all IDs, titles and API values below
        # are synthetic. This does not restore actual remote product fields.
        totals = [290, 290, 289, 290, 290] + [289] * 15 + [288, 289, 288, None]
        names = {str(pid): "Waschtrockner" for pid in range(2, 62)}
        names.update({str(pid): "Waschmaschine" for pid in range(62, 74)})
        names.update({"74": "Trockner Set", "75": "Portabel Trockner"})
        pages = {}
        for page, total in enumerate(totals, 1):
            ids = list(range((page - 1) * 12 + 1, page * 12 + 1))
            if page == 22:
                ids[1] = 1
            pages["main", page] = block(ids, total, more=page < 24, names=names,
                ads=((9001, "Trockner"), (9002, "Trockner")) if page == 1 else ())
        pages["main", 25] = pages["main", 24]
        eligible = [1, *range(76, 254), *range(255, 289)][:100]
        for start in range(0, 100, 12):
            pages["bsr", start // 12 + 1] = page_html([(str(pid), "Trockner") for pid in eligible[start:start+12]],
                shown=100 if start == 96 else 12, total=100, more=start < 96)
        browser = ScriptedBrowser(pages)
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, mail, _ = self.execute(Path(directory), browser)
            self.assertEqual((0, 213, 100, 213, 426),
                (code, manifest["main"]["rows"], manifest["bsr"]["rows"], manifest["output_rows"], manifest["api_calls"]))
            self.assertEqual((288, 4), (manifest["main"]["tail_confirmation"]["confirmed_total"],
                                        manifest["main"]["tail_confirmation"]["queries"]))
            self.assertEqual(213, len(db.call_args.args[1]))
            mail.assert_called_once()

    def test_missing_or_conflicting_fresh_totals_are_not_success(self):
        for fresh in (block((3, 4), more=True),
                      '<h1>Trockner (6 Artikel)</h1>' + block((3, 4), 5, more=True)):
            with self.subTest(case=fresh[:30]):
                browser = self.short_browser()
                browser.pages["main", 2] = [block((3, 4), 6, more=True), fresh]
                manifest, _ = self.assert_failure(browser)
                self.assertEqual("current_total_missing_or_conflicting", manifest["main"]["tail_confirmation"]["reason"])

    def test_probe_http_failure_is_not_hidden_by_valid_regular_pages(self):
        original = run.make_fetcher
        def factory(*args, **kwargs):
            if kwargs.get("phase") == "tail_confirmation":
                return lambda page: ("", 503, 0, None, 0)
            return original(*args, **kwargs)
        with patch.object(run, "make_fetcher", side_effect=factory):
            manifest, events = self.assert_failure(self.short_browser())
        self.assertEqual(2, manifest["main"]["tail_confirmation"]["queries"])
        self.assertTrue(all(e["ready_status"] == 503 for e in events if e["event"] == "tail_confirmation_read"))

    def test_following_snapshot_changes_before_regular_read_is_not_skipped(self):
        browser = ScriptedBrowser({
            ("main", 1): block((1, 2), 7, more=True),
            ("main", 2): [block((3, 4), 6, more=True), block((3, 4), 8, more=True)],
            ("main", 3): block((5, 6)),
            ("main", 4): [block((7, 8)), block((9, 10))],
        })
        manifest, _ = self.assert_failure(browser)
        self.assertEqual((6, "confirmed_following_products_changed"),
                         (manifest["main"]["rows"], manifest["main"]["stop_reason"]))
        self.assertEqual(12, len(browser.calls))

    def test_middle_repeat_and_nonuniform_prefix_are_not_short_category_success(self):
        for second in (block((1, 2), 6, more=True), block((3,), 6, more=True)):
            with self.subTest(second=second[:30]):
                browser = self.short_browser()
                browser.pages["main", 2] = second
                manifest, _ = self.assert_failure(browser)
                self.assertFalse(manifest["success"])

    def test_incomplete_initial_dom_does_not_disable_changed_total_confirmation(self):
        browser = self.short_browser()
        soup = BeautifulSoup(browser.pages["main", 1], "html.parser")
        soup.select_one("li").decompose()
        browser.pages["main", 1] = str(soup)
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, _, _, _ = self.execute(Path(directory), browser)
            self.assertEqual(0, code)
            self.assertTrue(manifest["pages"]["main"][0]["first_page_validation"]["passed"])

    def test_old_csv_and_other_batch_files_survive_confirmation_failure(self):
        browser = self.short_browser()
        browser.pages["main", 2] = block((3, 4), 6, more=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            csv = output / "mmkt_dry_output.csv"
            csv.write_text("OLD_BATCH_CSV", encoding="utf-8")
            old = output / "intermediate/old_batch/keep.json"
            old.parent.mkdir(parents=True)
            old.write_text("OLD_BATCH_SNAPSHOT", encoding="utf-8")
            code, manifest, db, _, _ = self.execute(root, browser)
            self.assertEqual(1, code)
            db.assert_not_called()
            self.assertEqual("OLD_BATCH_CSV", csv.read_text(encoding="utf-8"))
            self.assertEqual("OLD_BATCH_SNAPSHOT", old.read_text(encoding="utf-8"))
            self.assertEqual("not_created", manifest["csv"]["state"])

    def test_final_csv_failure_after_confirmed_tail_does_not_load_or_send_success(self):
        with patch.object(run, "write_output", side_effect=OSError("synthetic")):
            manifest, _ = self.assert_failure_after_csv_error()
        self.assertEqual("FINAL_CSV_SAVE_FAILED", manifest["error"])

    def assert_failure_after_csv_error(self):
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, mail, events = self.execute(Path(directory), self.short_browser())
            self.assertEqual((1, "confirmed", "failed"),
                (code, manifest["main"]["tail_confirmation"]["state"], manifest["csv"]["state"]))
            db.assert_not_called()
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertIn("[CHECK]", mail.call_args.args[0])
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(5, len(saved["rows"]))
            return manifest, events

    def test_verified_csv_reload_keeps_original_batch_and_does_not_resend_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, before, _, _, _ = self.execute(root, self.short_browser())
            browser = ScriptedBrowser({})
            code, after, _, mail, events = self.execute(root, browser, options=("--write-db",))
            self.assertEqual((0, before["batch_id"], "sent"),
                (code, after["batch_id"], after["notification"]["state"]))
            self.assertTrue(any(e["event"] == "mail_complete" and e["state"] == "already_sent" for e in events))
            mail.assert_not_called()
            self.assertEqual([], browser.requests)

    def test_multiple_expansions_cannot_reset_the_shared_eight_read_budget(self):
        browser = ScriptedBrowser({
            ("main", 1): block((1, 2), 7, more=True),
            ("main", 2): [block((3, 4), 6, more=True), block((3, 4), 8, more=True)],
            ("main", 3): [block((5, 6)), block((5, 6)), block((5, 6), 10, more=True)],
            ("main", 4): block((7, 8)), ("main", 5): block((9, 10)),
        })
        manifest, events = self.assert_failure(browser, options=("--page-retries", "20"))
        state = manifest["main"]["tail_confirmation"]
        self.assertEqual((8, 0, "confirmation_budget_exhausted"),
                         (state["queries"], state["remaining"], state["reason"]))
        self.assertEqual((10, 20), (manifest["main"]["rows"], len(browser.calls)))
        self.assertEqual(8, sum(e["event"] == "tail_confirmation_read" for e in events))

    def test_confirmation_result_save_failure_keeps_regular_and_probe_rows(self):
        original = checkpoint.write_manifest
        def save(path, value):
            if "_tail_result_" in path.name:
                raise OSError("synthetic")
            return original(path, value)
        with patch.object(checkpoint, "write_manifest", side_effect=save):
            manifest, _ = self.assert_failure(self.short_browser())
        self.assertEqual(("INTERMEDIATE_SAVE_FAILED", "not_started", 5),
            (manifest["error"], manifest["bsr"]["state"], manifest["main"]["rows"]))

    def test_individual_api_failure_remains_check_after_confirmed_tail_and_insert(self):
        browser = self.short_browser()
        original = browser.gql
        def gql(operation, variables):
            if operation == "GetComparisonTableRecommendations" and variables["ref"] == "5":
                browser.calls.append((operation, variables))
                return {"status": 500, "data": {"errors": []}}
            return original(operation, variables)
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, mail, _ = self.execute(Path(directory), browser)
            self.assertEqual((1, "confirmed", 1, "partial"),
                (code, manifest["main"]["tail_confirmation"]["state"],
                 manifest["product_failure_count"], manifest["collection_status"]))
            self.assertTrue(manifest["database"]["db_written"])
            self.assertEqual({"1", "2", "3", "4"}, {row["item"] for row in db.call_args.args[1]})
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertIn("[CHECK]", mail.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
