"""Synthetic listing snapshots through the real DRY CLI; external services mocked.

Only page fields already consumed by the production parser are used. No saved
HTML or missing middle-page product data from AWS is reconstructed here.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup

from test_dry_pipeline import page_html
import test_dry_tail_confirmation as tail_fixture
from test_dry_tail_confirmation import ScriptedBrowser, block
from dry import checkpoint, config, database, listing, run
from dry.detail import CollectionError, CommonAPIError, DetailCollector


class TimelineBrowser(ScriptedBrowser):
    def __init__(self, pages, detail_types=None):
        super().__init__(pages)
        self.detail_types = detail_types or {}
        self.timeline = []

    def navigate(self, url, **kwargs):
        result = super().navigate(url, **kwargs)
        self.timeline.append(("listing", *self.requests[-1]))
        return result

    def gql(self, operation, variables):
        item = variables.get("ref") or variables.get("id")
        self.timeline.append(("api", item, operation))
        return super().gql(operation, variables)


class ListingPhaseTests(unittest.TestCase):
    def execute(self, root, browser, options=()):
        return tail_fixture.TailPipelineTests().execute(root, browser, options=options)

    def browser(self):
        tail = block((3,))
        return TimelineBrowser({("main", 1): block((1, 2), 3, more=True),
                                ("main", 2): tail, ("main", 3): tail,
                                ("bsr", 1): block((3, 2, 1), 3)})

    def test_real_phase_order_listing_then_detail_then_bsr_with_original_group_logs(self):
        browser = self.browser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, mail, events = self.execute(root, browser)
            self.assertEqual(0, code)
            self.assertEqual([("listing", "main", 1), ("listing", "main", 2), ("listing", "main", 3)],
                             browser.timeline[:3])
            bsr = browser.timeline.index(("listing", "bsr", 1))
            self.assertEqual(6, len([step for step in browser.timeline[:bsr] if step[0] == "api"]))
            self.assertFalse(any(step[0] == "api" for step in browser.timeline[bsr:]))
            starts = [e for e in events if e["event"] == "detail_start"]
            self.assertEqual([("main", "1", 1), ("main", "2", 1), ("main", "3", 2),
                              ("bsr", "3", 1), ("bsr", "2", 1), ("bsr", "1", 1)],
                             [(e["sort"], e["item"], e["page"]) for e in starts])
            for sort in ("main", "bsr"):
                end = next(i for i, e in enumerate(events) if e["event"] == "listing_complete" and e["sort"] == sort)
                begin = next(i for i, e in enumerate(events) if e["event"] == "detail_start" and e["sort"] == sort)
                self.assertLess(end, begin)
                self.assertEqual(("complete", "complete"),
                    (manifest[sort]["listing_state"], manifest[sort]["detail_state"]))
            self.assertTrue(manifest["notification"]["success_report"])
            mail.assert_called_once()
            self.assertEqual(3, len(db.call_args.args[1]))

    def test_all_excluded_middle_block_does_not_end_listing_and_api_stops_at_target(self):
        tail = block((5, 6))
        browser = TimelineBrowser({("main", 1): block((1, 2), 6, more=True),
            ("main", 2): block((3, 4), 6, more=True,
                               names={"3": "Waschmaschine", "4": "Portabel Trockner"}),
            ("main", 3): tail, ("main", 4): tail, ("bsr", 1): block((2, 1), 2)})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser, ("--target", "3"))
            self.assertEqual(0, code)
            self.assertEqual((6, 4, 3), (manifest["main"]["category_slots_read"],
                manifest["main"]["candidate_count"], manifest["main"]["rows"]))
            self.assertEqual(["1", "2", "5"], [row["item"] for row in db.call_args.args[1]])
            self.assertNotIn("6", [call[1] for call in browser.timeline if call[0] == "api"])
            self.assertIn(("main", 4), browser.requests)

    def test_second_stage_exclusion_compresses_ranks_and_keeps_bsr_independent(self):
        browser = TimelineBrowser({("main", 1): block((1, 2, 3, 4, 5), 5,
            names={"3": "Portabel Trockner"}), ("bsr", 1): block((4, 2), 2)},
            detail_types={"1": "Waschtrockner"})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser, ("--target", "2", "--bsr-target", "2"))
            self.assertEqual(0, code)
            self.assertEqual([("2", "1", "2"), ("4", "2", "1")],
                [(row["item"], row["main_rank"], row["bsr_rank"]) for row in db.call_args.args[1]])
            self.assertEqual(5, manifest["api_calls"])
            self.assertEqual({"washer_dryer": 1}, manifest["main"]["detail_excluded"])
            self.assertEqual(1, manifest["main"]["early_excluded"]["portabel_title"])
            self.assertEqual(4, manifest["main"]["candidate_count"])

    def test_aws_observed_286_to_288_tail_and_incomplete_first_dom_with_synthetic_middle(self):
        # Only the observed counts/control states are transcribed from AWS.
        # Every ID, title and middle page is synthetic, not recovered HTML.
        pages = {}
        for page in range(1, 25):
            ids = tuple(range((page - 1) * 12 + 1, page * 12 + 1))
            pages["main", page] = block(ids, 286 if page == 1 else 288 if page < 24 else None,
                                         more=page < 24, ads=(("9000", "Banner"), ("9001", "Banner")) if page == 1 else ())
        soup = BeautifulSoup(pages["main", 1], "html.parser")
        for card in soup.select('li:not([data-test])')[3:]:
            card.decompose()
        pages["main", 1] = str(soup)
        pages["main", 25] = pages["main", 24]
        pages["bsr", 1] = block((1,), 1)
        browser = TimelineBrowser(pages)
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, mail, events = self.execute(Path(directory), browser,
                                                           ("--target", "1", "--bsr-target", "1"))
            self.assertEqual(0, code)
            self.assertEqual((288, 1, 2), (manifest["main"]["candidate_count"],
                                         manifest["main"]["rows"], manifest["api_calls"]))
            self.assertEqual((4, 288), (manifest["main"]["tail_confirmation"]["queries"],
                                        manifest["main"]["tail_confirmation"]["confirmed_total"]))
            first = manifest["pages"]["main"][0]
            # Three rendered normal cards plus two banner rows; state fills 12.
            self.assertEqual((286, 12, 5, True), (first["total"], first["state_slots"],
                                                 first["rendered_listing_rows"], first["state_coherent"]))
            view = listing.page_view(pages["main", 1], listing.parse_listing_html(pages["main", 1], page=1))
            self.assertFalse(listing.complete_dom(pages["main", 1], view))
            validations = [e for e in events if e["event"] == "first_page_validation"]
            self.assertTrue(all(e["passed"] and not e["details_allowed"] for e in validations))
            self.assertEqual(1, len(db.call_args.args[1]))
            mail.assert_called_once()

    def test_all_duplicate_candidates_in_a_different_middle_sequence_continue(self):
        tail = block((3, 4))
        browser = TimelineBrowser({("main", 1): block((1, 2), 6, more=True),
            ("main", 2): block((2, 1), 6, more=True), ("main", 3): tail,
            ("main", 4): tail, ("bsr", 1): block((1,), 1)})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser, ("--target", "3"))
            self.assertEqual(0, code)
            self.assertEqual((6, 4, 2), (manifest["main"]["category_slots_read"],
                manifest["main"]["candidate_count"], manifest["main"]["duplicates"]))
            self.assertEqual(["1", "2", "3"], [row["item"] for row in db.call_args.args[1]])

    def test_same_banner_and_standard_id_remains_an_included_sponsored_candidate(self):
        browser = TimelineBrowser({("main", 1): block((1, 2), 2,
            ads=(("1", "Trockner"), ("9", "Trockner"))), ("bsr", 1): block((1,), 1)})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser)
            self.assertEqual(0, code)
            self.assertEqual(["1", "2"], [row["item"] for row in db.call_args.args[1]])
            saved = json.loads(Path(manifest["main"]["candidate_path"]).read_text(encoding="utf-8"))
            self.assertTrue(all(row["listing_card_type"] == "standard" and row["sku_status"] == "Sponsored"
                                for row in saved["candidates"]))
            self.assertNotIn("9", [entry[1] for entry in browser.timeline if entry[0] == "api"])

    def test_bsr_new_product_waits_for_its_entire_listing_and_keeps_independent_rank(self):
        tail = block((4,))
        browser = TimelineBrowser({("main", 1): block((1, 2), 2),
            ("bsr", 1): block((3, 1), 3, more=True), ("bsr", 2): tail, ("bsr", 3): tail})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser,
                                                    ("--target", "2", "--bsr-target", "2"))
            self.assertEqual(0, code)
            last_listing = browser.timeline.index(("listing", "bsr", 3))
            first_new_api = browser.timeline.index(("api", "3", "GetComparisonTableRecommendations"))
            self.assertLess(last_listing, first_new_api)
            self.assertEqual(6, manifest["api_calls"])
            values = {row["item"]: row for row in db.call_args.args[1]}
            self.assertEqual(("", "1"), (values["3"]["main_rank"], values["3"]["bsr_rank"]))
            self.assertNotIn("4", values)

    def test_verified_zero_category_is_distinct_from_a_failed_empty_screen(self):
        browser = TimelineBrowser({(sort, 1): page_html((), shown=0, total=0)
                                   for sort in ("main", "bsr")})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, _ = self.execute(Path(directory), browser)
            self.assertEqual((0, 0, "complete"),
                (code, manifest["output_rows"], manifest["collection_status"]))
            self.assertTrue(all(manifest[sort]["listing_state"] == "complete" for sort in ("main", "bsr")))
            db.assert_not_called()
            self.assertEqual([], browser.calls)

    def test_more_than_300_candidates_are_all_saved_but_only_300_details_are_loaded(self):
        pages = {}
        for page in range(1, 28):
            ids = tuple(range((page - 1) * 12 + 1, page * 12 + 1))
            pages["main", page] = block(ids, 324 if page < 27 else None, more=page < 27)
        pages["main", 28] = pages["main", 27]
        pages["bsr", 1] = block((1,), 1)
        browser = TimelineBrowser(pages)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, _, _ = self.execute(root, browser, ("--bsr-target", "1"))
            self.assertEqual(0, code)
            self.assertEqual((324, 300, 600), (manifest["main"]["candidate_count"],
                                            manifest["main"]["rows"], manifest["api_calls"]))
            saved = json.loads(Path(manifest["main"]["candidate_path"]).read_text(encoding="utf-8"))
            self.assertEqual("324", saved["candidates"][-1]["sku_id"])
            self.assertEqual(300, len(db.call_args.args[1]))
            self.assertNotIn("301", [entry[1] for entry in browser.timeline if entry[0] == "api"])
            self.assertEqual("last_page", manifest["main"]["listing_stop_reason"])
            self.assertEqual("target_reached", manifest["main"]["stop_reason"])

    def test_normal_price_gaps_and_other_listing_fields_survive_candidate_storage(self):
        real_parse = listing.parse_listing_html
        def parse(*args, **kwargs):
            rows = real_parse(*args, **kwargs)
            for row in rows:
                row.update(final_sku_price=None, original_sku_price=None, savings=None,
                           manufacturer="PUBLIC_BRAND", discount_type="PUBLIC_PROMOTION",
                           count_of_reviews=17, star_rating=4.2)
            return rows
        with tempfile.TemporaryDirectory() as directory, patch.object(listing, "parse_listing_html", side_effect=parse):
            code, manifest, db, _, _ = self.execute(Path(directory), self.browser())
            self.assertEqual(0, code)
            saved = json.loads(Path(manifest["main"]["candidate_path"]).read_text(encoding="utf-8"))
            for row in saved["candidates"]:
                self.assertIsNone(row["final_sku_price"])
                self.assertEqual(("PUBLIC_BRAND", "PUBLIC_PROMOTION", 17, 4.2),
                    (row["manufacturer"], row["discount_type"], row["count_of_reviews"], row["star_rating"]))
                self.assertTrue(row["observed_at"])
                self.assertEqual("main", row["listing_sort"])
            self.assertEqual(3, len(db.call_args.args[1]))

    def test_saved_candidate_corruption_blocks_all_detail_and_db_calls(self):
        browser = self.browser()
        original = checkpoint.write_manifest
        def corrupt(path, value):
            original(path, value)
            if value.get("phase") == "candidates":
                saved = json.loads(path.read_text(encoding="utf-8"))
                saved["candidates"][0]["sku_id"] = "999"
                path.write_text(json.dumps(saved), encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory, patch.object(checkpoint, "write_manifest", side_effect=corrupt):
            root = Path(directory)
            code, manifest, db, _, _ = self.execute(root, browser)
            self.assertEqual((1, "INTERMEDIATE_SAVE_FAILED"), (code, manifest["error"]))
            self.assertEqual([], browser.calls)
            self.assertEqual("not_started", manifest["main"]["detail_state"])
            db.assert_not_called()
            self.assertFalse((root / "output/mmkt_dry_output.csv").exists())

    def test_detail_checkpoint_failure_preserves_prior_detail_and_candidate_files(self):
        original = checkpoint.IntermediateJournal._write_line
        def fail(journal, line):
            value = json.loads(line)
            if value['kind']=='detail' and value['result']['item']=='2':
                raise OSError("SYNTHETIC_SAVE_FAILURE")
            original(journal, line)
        with tempfile.TemporaryDirectory() as directory, patch.object(checkpoint.IntermediateJournal, "_write_line", fail):
            code, manifest, db, _, _ = self.execute(Path(directory), self.browser())
            self.assertEqual((1, "INTERMEDIATE_SAVE_FAILED", 2),
                             (code, manifest["error"], manifest["main"]["rows"]))
            saved = checkpoint.read_journal(manifest['intermediate_journals']['main']['path'])['records']
            self.assertEqual(["1"], [r['result']['item'] for r in saved if r['kind']=='detail'])
            self.assertTrue(Path(manifest["main"]["candidate_path"]).exists())
            self.assertEqual("not_started", manifest["bsr"]["state"])
            db.assert_not_called()

    def test_final_csv_save_failure_keeps_completed_phase_counts_and_blocks_loading(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(run, "write_output", side_effect=OSError("SYNTHETIC")):
            root = Path(directory)
            code, manifest, db, _, _ = self.execute(root, self.browser())
            self.assertEqual((1, "FINAL_CSV_SAVE_FAILED", "failed"),
                             (code, manifest["error"], manifest["csv"]["state"]))
            self.assertEqual((3, 3, "complete", "complete"),
                (manifest["main"]["rows"], manifest["bsr"]["rows"],
                 manifest["main"]["state"], manifest["bsr"]["state"]))
            db.assert_not_called()
            self.assertFalse(manifest["notification"]["success_report"])
            with self.assertRaises(CollectionError):
                database.load_validated_output(root / "output/mmkt_dry_output.csv")

    def test_confirmed_all_excluded_category_is_empty_without_any_db_connection(self):
        browser = TimelineBrowser({(sort, 1): block((1,), 1, names={"1": "Waschmaschine"})
                                   for sort in ("main", "bsr")})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, mail, events = self.execute(root, browser)
            self.assertEqual(0, code)
            self.assertEqual((0, "complete", "skipped_empty", False),
                (manifest["output_rows"], manifest["collection_status"],
                 manifest["database"]["state"], manifest["database"]["db_written"]))
            self.assertEqual([], browser.calls)
            db.assert_not_called()
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertFalse(events[-1]["db_written"])
            self.assertEqual([], database.load_validated_output(root / "output/mmkt_dry_output.csv"))
            mail.assert_called_once()
            self.assertIn("적격 상품 0건", mail.call_args.args[0])

    def test_isolated_missing_products_can_all_fail_without_hiding_failure_as_empty(self):
        browser = self.browser()
        browser.gql = Mock(return_value={"status": 404})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, db, _, events = self.execute(root, browser, ("--page-retries", "0"))
            self.assertEqual((1, "partial", 6),
                (code, manifest["collection_status"], manifest["product_failure_count"]))
            self.assertEqual("NO_VALID_PRODUCTS", events[-1]["error"])
            self.assertEqual([], database.load_validated_output(root / "output/mmkt_dry_output.csv"))
            db.assert_not_called()

    def test_explicit_mid_listing_error_saves_candidates_without_any_api_work(self):
        browser = TimelineBrowser({("main", 1): block((1, 2), 4, more=True),
                                   ("main", 2): "<html>access denied</html>"})
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, events = self.execute(Path(directory), browser, ("--page-retries", "2"))
            self.assertEqual(1, code)
            self.assertEqual((2, 0, "not_started"), (manifest["main"]["candidate_count"],
                manifest["main"]["rows"], manifest["main"]["detail_state"]))
            self.assertEqual(3, browser.counts["main", 2])
            self.assertEqual([], browser.calls)
            db.assert_not_called()
            self.assertTrue(any(e["event"] == "page_summary" for e in events))

    def test_candidate_artifact_cannot_be_loaded_as_final_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, _, _, _ = self.execute(Path(directory), self.browser())
            self.assertEqual(0, code)
            path = Path(manifest["main"]["candidate_path"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(saved["success"])
            self.assertFalse(saved["loadable"])
            self.assertEqual("intermediate", saved["artifact_kind"])
            with self.assertRaises(CollectionError):
                database.load_validated_output(path)


class CommonFailureTests(unittest.TestCase):
    def collector(self, response, retries=1):
        session = Mock()
        session.gql.return_value = response
        return DetailCollector(session, retries=retries, sleep_s=0), session

    def test_same_exhausted_block_on_two_products_stops_operation(self):
        for status in (401, 403, 429, 503, None):
            with self.subTest(status=status), contextlib.redirect_stdout(io.StringIO()):
                collector, session = self.collector({"status": status})
                with self.assertRaisesRegex(CollectionError, "^DETAIL_API_FAILED$"):
                    collector.query("GetComparisonTableRecommendations", {"ref": "1"})
                with self.assertRaisesRegex(CommonAPIError, "^DETAIL_API_COMMON_FAILURE$"):
                    collector.query("GetComparisonTableRecommendations", {"ref": "2"})
                self.assertEqual(4, session.gql.call_count)
                self.assertEqual({}, collector.cache)

    def test_same_product_retries_are_not_miscounted_as_multiple_products(self):
        collector, session = self.collector({"status": 403}, retries=2)
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                with self.assertRaisesRegex(CollectionError, "^DETAIL_API_FAILED$"):
                    collector.query("GetProductReviews", {"id": "1"})
        self.assertEqual(6, session.gql.call_count)

    def test_successful_operation_resets_its_failed_product_streak(self):
        collector, session = self.collector({"status": 403}, retries=0)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(CollectionError, "^DETAIL_API_FAILED$"):
                collector.query("GetProductReviews", {"id": "1"})
            session.gql.return_value = {"status": 200, "data": {"data": {}}}
            collector.query("GetProductReviews", {"id": "2"})
            session.gql.return_value = {"status": 403}
            with self.assertRaisesRegex(CollectionError, "^DETAIL_API_FAILED$"):
                collector.query("GetProductReviews", {"id": "3"})

    def test_explicit_query_validation_stops_after_bounded_retries_without_raw_message(self):
        collector, session = self.collector({"status": 500, "data": {"errors": [
            {"message": "RAW_MESSAGE_MUST_NOT_PRINT", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}})
        with contextlib.redirect_stdout(io.StringIO()) as screen, self.assertRaises(CommonAPIError):
            collector.query("GetComparisonTableRecommendations", {"ref": "1"})
        self.assertEqual(2, session.gql.call_count)
        self.assertNotIn("RAW_MESSAGE", screen.getvalue())


if __name__ == "__main__":
    unittest.main()
