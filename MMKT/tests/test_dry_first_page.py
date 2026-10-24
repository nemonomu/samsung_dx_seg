"""First-block admission and retry safety using synthetic, offline documents."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from test_dry_pipeline import Browser, page_html
from dry import config, database
from dry.listing import collect_pages, complete_dom, page_view
from common.listing_policy import is_banner_ad
from common.parsers import extract_preloaded_state, parse_listing_html


def block(ids=("1", "2"), *, total=5, shown=2, more=True):
    return page_html([(str(pid), "Trockner") for pid in ids],
                     shown=shown, total=total, more=more)


def inconsistent(html):
    # Synthetic ordinary card absent from the internal category, without an
    # explicit Sponsored label. The original failed HTML was not retained.
    extra = '<li><a href="/de/product/_test-990.html">Trockner</a></li>'
    return html.replace('</ul>', extra + '</ul>')


class FirstPageTests(unittest.TestCase):
    def collect(self, pages, *, target=300, retries=2, eligibility=None, on_page=None):
        queues = {page: deque(value if isinstance(value, list) else [value])
                  for page, value in pages.items()}
        requests = []
        def fetch(page):
            requests.append(page)
            queue = queues[page]
            value = queue.popleft() if len(queue) > 1 else queue[0]
            return value if isinstance(value, tuple) else (value, 200, 0, None, 0)
        with contextlib.redirect_stdout(io.StringIO()) as screen:
            rows, logs, reason = collect_pages(fetch, target=target, retries=retries,
                eligibility=eligibility or (lambda row: None), on_page=on_page)
        return rows, logs, reason, requests, screen.getvalue()

    def test_persistent_mismatch_never_calls_details_or_commits_rows(self):
        detail = Mock(return_value=None)
        snapshots = []
        rows, logs, reason, requests, _ = self.collect(
            {1: inconsistent(block())}, eligibility=detail,
            on_page=lambda rows, logs, stop: snapshots.append((len(rows), stop)))
        self.assertEqual(([], "first_page_inconsistent", [1, 1, 1]), (rows, reason, requests))
        detail.assert_not_called()
        self.assertEqual([(0, None), (0, None), (0, "first_page_inconsistent")], snapshots)
        self.assertTrue(all(entry["new_unique"] == 0 and not entry["excluded"] for entry in logs))

    def test_fresh_retry_is_the_only_committed_state_and_can_prove_short_end(self):
        details = []
        tail = block(("5",), total=None, shown=None, more=False)
        rows, logs, reason, requests, _ = self.collect({
            1: [inconsistent(block(total=99)), block()],
            2: block(("3", "4")), 3: tail, 4: tail},
            eligibility=lambda row: details.append(row["sku_id"]))
        self.assertEqual(("last_page", [1, 1, 2, 3, 4]), (reason, requests))
        self.assertEqual(["1", "2", "3", "4", "5"], details)
        self.assertEqual(details, [row["sku_id"] for row in rows])
        self.assertEqual((0, 2), (logs[0]["accepted"], logs[1]["accepted"]))
        self.assertEqual(([5], 5, True), (logs[-1]["observed_totals"],
                                         logs[-1]["category_slots_read"], logs[1]["uniform_prefix"]))
        self.assertEqual("stable_total_slots_and_verified_tail_repeat", logs[-1]["end_basis"])

    def test_coherent_state_can_admit_incomplete_first_dom(self):
        html = block()
        soup = BeautifulSoup(html, "html.parser")
        soup.select_one("li").decompose()
        html = str(soup)
        view = page_view(html, parse_listing_html(html, page=1))
        self.assertTrue(view["state_coherent"])
        self.assertFalse(complete_dom(html, view))
        rows, _, reason, requests, _ = self.collect({1: html}, target=2)
        self.assertEqual((2, "target_reached", [1]), (len(rows), reason, requests))

    def test_gate_pass_precedes_any_detail_work(self):
        events = []
        import dry.listing as listing
        with patch.object(listing, "emit", side_effect=lambda event, **fields: events.append((event, fields))):
            self.collect({1: block()}, target=1,
                         eligibility=lambda row: events.append(("detail", row)))
        self.assertEqual("first_page_validation", events[0][0])
        self.assertTrue(events[0][1]["passed"])
        self.assertEqual("detail", events[1][0])

    def test_configured_attempt_limit_is_not_multiplied(self):
        for retries in (0, 1, 2):
            detail = Mock()
            _, logs, reason, requests, _ = self.collect(
                {1: inconsistent(block())}, retries=retries, eligibility=detail)
            self.assertEqual([1] * (retries + 1), requests)
            self.assertEqual("first_page_inconsistent", reason)
            self.assertEqual("stop_listing", logs[-1]["first_page_validation"]["action"])
            detail.assert_not_called()

    def test_third_attempt_can_pass_without_bad_attempt_duplicates(self):
        detail = Mock(return_value=None)
        rows, logs, reason, requests, _ = self.collect(
            {1: [inconsistent(block()), inconsistent(block()), block()]},
            target=2, eligibility=detail)
        self.assertEqual(("target_reached", [1, 1, 1], 2), (reason, requests, len(rows)))
        self.assertEqual(2, detail.call_count)
        self.assertEqual([False, False, True], [e["first_page_validation"]["passed"] for e in logs])

    def test_fetch_and_parse_failures_retry_before_details(self):
        for bad, reason in ((("", 403, 0, None, 0), "fetch_failed"),
                            ("<h1>unrelated page</h1>", "parse_failed")):
            detail = Mock(return_value=None)
            _, logs, stop, requests, _ = self.collect({1: [bad, block()]}, target=1, eligibility=detail)
            self.assertEqual(([1, 1], "target_reached", reason), (requests, stop, logs[0]["error"]))
            self.assertEqual(1, detail.call_count)
            self.assertFalse(logs[0]["first_page_validation"]["details_allowed"])

    def test_banner_only_state_product_is_reported_and_rejected(self):
        html = block()
        soup = BeautifulSoup(html, "html.parser")
        state = extract_preloaded_state(html)
        del state["apolloState"]["1"]
        soup.select_one("li")["data-test"] = "mms-sba-product-tile"
        soup.find("script").string = 'window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';'
        detail = Mock()
        _, logs, reason, _, _ = self.collect({1: str(soup)}, eligibility=detail)
        evidence = logs[-1]["first_page_validation"]
        self.assertEqual("first_page_inconsistent", reason)
        self.assertEqual((["1"], ["1"], 2, 1), (evidence["missing_from_normal"],
            evidence["banner_only_state_ids"], evidence["state_slots"], evidence["normal_unique"]))
        detail.assert_not_called()

    def test_duplicate_internal_ids_fail_before_details(self):
        html = block()
        soup = BeautifulSoup(html, "html.parser")
        state = extract_preloaded_state(html)
        state["apolloState"]["page"]["products"].append({"productId": "1"})
        soup.find("script").string = 'window.__PRELOADED_STATE__ = ' + json.dumps(state) + ';'
        detail = Mock()
        _, logs, reason, _, _ = self.collect({1: str(soup)}, eligibility=detail)
        self.assertEqual("first_page_inconsistent", reason)
        self.assertEqual(["1"], logs[-1]["first_page_validation"]["duplicate_state_ids"])
        detail.assert_not_called()

    def test_shared_banner_id_keeps_ordinary_card_and_sponsored_extra(self):
        html = page_html([("1", "Trockner"), ("2", "Trockner")],
                         ads=[("1", "Trockner"), ("900", "Trockner")], shown=2, total=10, more=True)
        html = html.replace('</ul>', '<li><span>Gesponsert</span>'
                            '<a href="/de/product/_test-990.html">Trockner</a></li></ul>')
        rows, logs, reason, _, _ = self.collect({1: html}, target=3,
            eligibility=lambda row: "advertisement" if is_banner_ad(row) else None)
        self.assertEqual(("target_reached", {"1", "2", "990"}), (reason, {row["sku_id"] for row in rows}))
        evidence = logs[0]["first_page_validation"]
        self.assertTrue(evidence["passed"])
        self.assertEqual(["1"], evidence["banner_and_normal_ids"])
        self.assertEqual({"standard", "sponsored-ad"},
            {row["listing_card_type"] for row in evidence["card_evidence"] if row["item"] == "1"})
        self.assertEqual({"advertisement": 2}, logs[0]["excluded"])

    def test_coherence_does_not_override_existing_pagination_error(self):
        detail = Mock()
        html = block(total=2, shown=2, more=False).rsplit('</div>', 1)[0]
        html += '<button disabled>weitere Produkte anzeigen</button><button>weitere Produkte anzeigen</button></div>'
        _, logs, reason, requests, _ = self.collect(
            {1: html}, eligibility=detail)
        self.assertEqual(("counter_next_control_conflict", [1, 1, 1]), (reason, requests))
        self.assertTrue(logs[0]["state_coherent"])
        self.assertFalse(logs[0]["first_page_validation"]["passed"])
        detail.assert_not_called()


class FirstPagePipelineTests(unittest.TestCase):
    def execute(self, root, browser, options=()):
        # Reuse the existing mock-only CLI harness, not another crawler path.
        import test_dry_listing_end as existing
        return existing.ProgressPreservationTests().execute(root, browser, options=options)

    def test_main_gate_exhaustion_preserves_old_files_and_never_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            old_csv = output / "mmkt_dry_output.csv"
            old_csv.write_text("OLD_CSV_MUST_NOT_BE_LOADED", encoding="utf-8")
            prior = output / "intermediate/prior_batch/keep.json"
            prior.parent.mkdir(parents=True)
            prior.write_text("PRIOR_SNAPSHOT", encoding="utf-8")
            browser = Browser({("main", 1): inconsistent(block())})
            browser.navigate = Mock(wraps=browser.navigate)
            code, db, _, manifest, screen = self.execute(root, browser)
            self.assertEqual((1, 3, [], 0), (code, browser.navigate.call_count, browser.calls, manifest["main"]["rows"]))
            self.assertEqual(("failed", "not_started", "not_started"),
                (manifest["main"]["state"], manifest["bsr"]["state"], manifest["database"]["state"]))
            db.assert_not_called()
            self.assertFalse(manifest["notification"]["success_report"])
            self.assertEqual("OLD_CSV_MUST_NOT_BE_LOADED", old_csv.read_text(encoding="utf-8"))
            self.assertEqual("PRIOR_SNAPSHOT", prior.read_text(encoding="utf-8"))
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual([], saved["rows"])
            self.assertFalse(saved["loadable"])
            self.assertIn('"action": "stop_listing"', screen)
            report = (output / "mmkt_dry_email_report.txt").read_text(encoding="utf-8")
            self.assertIn("첫 상품 묶음 불일치 (재조회 소진)", report)
            self.assertIn("DB 적재: 미실행", report)

    def test_bsr_gate_failure_preserves_completed_main_cache_and_snapshots(self):
        browser = Browser({("main", 1): block(total=2, shown=2, more=False),
                           ("bsr", 1): inconsistent(block())})
        browser.navigate = Mock(wraps=browser.navigate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, manifest, _ = self.execute(root, browser)
            self.assertEqual((1, 2, 0, 2, 4), (code, manifest["main"]["rows"], manifest["bsr"]["rows"],
                                              manifest["unique_detail_products"], manifest["api_calls"]))
            self.assertEqual(("complete", "failed"), (manifest["main"]["state"], manifest["bsr"]["state"]))
            self.assertEqual(4, browser.navigate.call_count)
            self.assertEqual(4, len(browser.calls))
            db.assert_not_called()
            saved = json.loads(Path(manifest["intermediate"]["last_path"]).read_text(encoding="utf-8"))
            self.assertEqual(["1", "2"], [row["item"] for row in saved["rows"]])
            self.assertTrue(all(row["bsr_rank"] is None and tuple(row) == config.COLUMNS for row in saved["rows"]))
            self.assertFalse(saved["loadable"])
            self.assertTrue(Path(manifest["intermediate"]["last_path"]).with_name("main_0001_attempt_01.json").exists())
            self.assertFalse((root / "output/mmkt_dry_output.csv").exists())

    def test_both_gates_recover_then_short_main_merges_loads_and_reports_success(self):
        first = block(total=3)
        tail = block(("3",), total=None, shown=None, more=False)
        bsr = block(("2", "1"), total=3)
        browser = Browser({("main", 1): first, ("main", 2): tail, ("main", 3): tail,
                           ("bsr", 1): bsr})
        navigate = browser.navigate
        attempts = {}
        def transient(url, **kwargs):
            response = navigate(url, **kwargs)
            sort = "bsr" if "sort=" in url else "main"
            if response["html"] in (first, bsr):
                attempts[sort] = attempts.get(sort, 0) + 1
                if attempts[sort] == 1:
                    # A different transient total must not enter the end ledger.
                    browser.driver.page_source = inconsistent(response["html"].replace('von 3', 'von 99'))
                    response["html"] = browser.driver.page_source
            return response
        browser.navigate = Mock(side_effect=transient)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, db, _, manifest, screen = self.execute(root, browser, ("--bsr-target", "2"))
            self.assertEqual((0, "last_page", "target_reached"),
                (code, manifest["main"]["stop_reason"], manifest["bsr"]["stop_reason"]))
            self.assertEqual((3, 2, 3, 6), (manifest["main"]["rows"], manifest["bsr"]["rows"],
                                            manifest["output_rows"], len(browser.calls)))
            self.assertEqual(6, browser.navigate.call_count)
            self.assertEqual(config.PRODUCTION_TABLE, db.call_args.args[0])
            rows = database.load_validated_output(root / "output/mmkt_dry_output.csv")
            self.assertEqual([(1, 2), (2, 1), (3, None)],
                [(int(row["main_rank"]), int(row["bsr_rank"]) if row["bsr_rank"] else None) for row in rows])
            self.assertTrue(manifest["notification"]["success_report"])
            self.assertTrue(all(entry["category_slots_read"] == 3 for entry in manifest["pages"]["main"][-2:]))
            events = [json.loads(line) for line in screen.splitlines() if line.startswith('{')]
            validations = [entry for entry in events if entry["event"] == "first_page_validation"]
            self.assertEqual([("main", False), ("main", True), ("bsr", False), ("bsr", True)],
                             [(e["sort"], e["passed"]) for e in validations])
            for sort in ("main", "bsr"):
                passed = next(i for i, e in enumerate(events)
                              if e["event"] == "first_page_validation" and e["sort"] == sort and e["passed"])
                detail = next(i for i, e in enumerate(events) if e["event"] == "detail_start" and e["sort"] == sort)
                self.assertLess(passed, detail)
            file_events = [json.loads(line) for line in next((root / "logs").glob('*.log')).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(validations, [e for e in file_events if e["event"] == "first_page_validation"])


if __name__ == "__main__":
    unittest.main()
