from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import listing


def _response(*, error: str | None, rows: list[dict], size: int) -> tuple[dict, list[dict]]:
    return ({
        "url": "https://www.amazon.de/s?k=test",
        "status": 503 if error == "amazon_technical_error" else 200,
        "text": "technical error" if error else "normal listing",
        "bytes": size,
        "error": error,
    }, rows)


class FakeSession:
    def __init__(self, attempts: list[tuple[dict, list[dict]]]) -> None:
        self.attempts = list(attempts)
        self.driver = SimpleNamespace(rows=[])
        self.refetch_calls = 0
        self.restart_calls = 0
        self.warmup_calls = 0

    def warm_up(self, _url: str) -> dict:
        self.warmup_calls += 1
        return {
            "url": "https://www.amazon.de/",
            "status": 200,
            "text": "homepage",
            "bytes": 1_500_000,
            "error": None,
        }

    def _next(self) -> dict:
        response, rows = self.attempts.pop(0)
        self.driver.rows = rows
        return response

    def fetch(self, *_args, **_kwargs) -> dict:
        return self._next()

    def refetch(self, *_args, **_kwargs) -> dict:
        self.refetch_calls += 1
        return self._next()

    def restart(self, _reason: str) -> None:
        self.restart_calls += 1


class MainListingRetryTests(unittest.TestCase):
    def _run(self, session: FakeSession, raw_root: Path, *, product: str = "REF",
             target: int = 1, max_pages: int = 1, emit=None, save=None) -> dict:
        cfg = SimpleNamespace(PRODUCT=product, ACCOUNT_NAME="Amazon.de", MAIN_URL="https://www.amazon.de/s?k=test")
        logger = Mock()
        with (
            patch.object(listing, "ensure_dirs"),
            patch.object(listing, "category_output_root", return_value=raw_root),
            patch.object(listing, "category_reference_root", return_value=raw_root),
            patch.object(listing.selector_api, "load_selectors", return_value={}),
            patch.object(listing.selector_api, "extract_cards", side_effect=lambda driver, *_args, **_kwargs: driver.rows),
            patch.object(listing.siel_log, "setup", return_value=(logger, None)),
            patch.object(listing.siel_log, "log_selectors"),
            patch.object(listing.siel_log, "warn_price_logic"),
            patch.object(listing.siel_log, "log_record_summary"),
            patch.object(listing, "write_csv"),
            patch.object(listing, "write_json"),
            patch.object(listing, "save_text", save or Mock()),
        ):
            return listing.run(
                cfg,
                sort="main",
                target=target,
                max_pages=max_pages,
                batch_id="a_test",
                session=session,
                emit=emit,
            )

    def test_same_session_retry_recovers_technical_error(self) -> None:
        session = FakeSession([
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error=None, rows=[{"item": "B0TEST"}], size=1_500_000),
        ])
        with patch.object(listing.time, "sleep"):
            manifest = self._run(session, Path("test-output"))

        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(session.warmup_calls, 1)
        self.assertEqual(manifest["warmup"]["status"], 200)
        self.assertEqual(session.refetch_calls, 1)
        self.assertEqual(session.restart_calls, 0)
        self.assertEqual(manifest["pages"][0]["retry_attempts"][-1]["mode"], "same_session")

    def test_tv_main_uses_the_same_homepage_warmup(self) -> None:
        session = FakeSession([
            _response(error=None, rows=[{"item": "B0TVTEST"}], size=1_500_000),
        ])
        with patch.object(listing.time, "sleep"):
            manifest = self._run(session, Path("test-output"), product="TV")

        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(session.warmup_calls, 1)
        self.assertEqual(manifest["warmup"]["status"], 200)
        self.assertEqual(session.refetch_calls, 0)
        self.assertEqual(session.restart_calls, 0)

    def test_new_session_retry_runs_after_same_session_failure(self) -> None:
        session = FakeSession([
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error=None, rows=[{"item": "B0TEST"}], size=1_500_000),
        ])
        with patch.object(listing.time, "sleep"):
            manifest = self._run(session, Path("test-output"))

        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(session.refetch_calls, 1)
        self.assertEqual(session.restart_calls, 1)
        self.assertEqual(session.warmup_calls, 2)
        self.assertEqual(manifest["pages"][0]["retry_attempts"][-1]["mode"], "new_session")

    def test_empty_normal_page_is_also_retried(self) -> None:
        session = FakeSession([
            _response(error=None, rows=[], size=1_000_000),
            _response(error=None, rows=[{"item": "B0TEST"}], size=1_500_000),
        ])
        with patch.object(listing.time, "sleep"):
            manifest = self._run(session, Path("test-output"))

        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(manifest["pages"][0]["retry_attempts"][0]["reason"], "listing_cards_empty")

    def test_all_retries_fail_closed_before_downstream_stages(self) -> None:
        session = FakeSession([
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error="amazon_technical_error", rows=[], size=2294),
        ])

        with patch.object(listing.time, "sleep"):
            with self.assertRaisesRegex(
                listing.MainListingUnavailableError,
                "main listing unavailable after 3 attempts: amazon_technical_error",
            ):
                self._run(session, Path("test-output"))

        self.assertEqual(session.refetch_calls, 1)
        self.assertEqual(session.restart_calls, 1)
        self.assertEqual(session.warmup_calls, 2)

    def test_later_page_recovers_and_continues_without_duplicate_rows(self) -> None:
        session = FakeSession([
            _response(error=None, rows=[{"item": "FIRST"}], size=1000),
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error=None, rows=[{"item": "SECOND"}], size=1000),
            _response(error=None, rows=[{"item": "THIRD"}], size=1000),
        ])
        saved = Mock()
        manifest = self._run(session, Path("test-output"), target=3, max_pages=4, save=saved)
        self.assertTrue(manifest["success"])
        self.assertEqual(manifest["stop_reason"], "target_reached")
        self.assertEqual([row["item"] for row in manifest["rows_data"]], ["FIRST", "SECOND", "THIRD"])
        self.assertEqual([row["page_no"] for row in manifest["rows_data"]], [1, 2, 3])
        self.assertEqual(session.refetch_calls, 1)
        self.assertEqual(saved.call_args.args[0].name, "page_02_attempt_1_error.html")

    def test_later_page_can_recover_in_new_session(self) -> None:
        session = FakeSession([
            _response(error=None, rows=[{"item": "FIRST"}], size=1000),
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error=None, rows=[{"item": "SECOND"}], size=1000),
        ])
        manifest = self._run(session, Path("test-output"), target=2, max_pages=3)
        self.assertTrue(manifest["success"])
        self.assertEqual(session.restart_calls, 1)
        self.assertEqual(manifest["pages"][1]["retry_attempts"][-1]["mode"], "new_session")

    def test_later_page_exhaustion_preserves_rows_and_reports_incomplete(self) -> None:
        session = FakeSession([
            _response(error=None, rows=[{"item": "FIRST"}], size=1000),
            *[_response(error="amazon_technical_error", rows=[{"item": "UNSAFE"}], size=2294)
              for _ in range(3)],
        ])
        emitted, saved = [], Mock()
        manifest = self._run(session, Path("test-output"), target=3, max_pages=4,
                             emit=emitted.append, save=saved)
        self.assertFalse(manifest["success"])
        self.assertEqual(manifest["failed_page"], 2)
        self.assertEqual(manifest["failure_reason"], "amazon_technical_error")
        self.assertEqual([row["item"] for row in manifest["rows_data"]], ["FIRST"])
        self.assertEqual(manifest["pages"][1]["parsed_rows"], 0)
        self.assertEqual(saved.call_count, 3)
        self.assertEqual(saved.call_args.args[0].name, "page_02_attempt_3_error.html")
        issue = next(row for row in emitted if row.get("_error"))
        self.assertEqual(issue["error_stage"], "main")
        self.assertEqual(issue["page_no"], 2)
        self.assertIn("remaining pages were not collected", issue["message"])

    def test_normal_last_page_ends_without_retry(self) -> None:
        response, rows = _response(error=None, rows=[{"item": "LAST"}], size=1000)
        response["text"] = '<span class="s-pagination-next s-pagination-disabled">Next</span>'
        session = FakeSession([(response, rows)])
        manifest = self._run(session, Path("test-output"), target=300, max_pages=30)
        self.assertTrue(manifest["success"])
        self.assertEqual(manifest["stop_reason"], "last_page")
        self.assertEqual(session.refetch_calls, 0)

    def test_empty_page_is_not_accepted_as_last_page(self) -> None:
        response, rows = _response(error=None, rows=[], size=1000)
        response["text"] = '<span class="s-pagination-next s-pagination-disabled">Next</span>'
        session = FakeSession([
            _response(error=None, rows=[{"item": "FIRST"}], size=1000),
            (response, rows), (response, rows), (response, rows),
        ])
        manifest = self._run(session, Path("test-output"), target=300, max_pages=30)
        self.assertFalse(manifest["success"])
        self.assertEqual(manifest["failure_reason"], "listing_cards_empty")
        self.assertEqual(session.refetch_calls, 1)

    def test_page_limit_without_last_page_is_incomplete(self) -> None:
        session = FakeSession([_response(error=None, rows=[{"item": "FIRST"}], size=1000)])
        manifest = self._run(session, Path("test-output"), target=300, max_pages=1)
        self.assertFalse(manifest["success"])
        self.assertEqual(manifest["failure_reason"], "page_limit_reached")

    def test_enabled_next_control_prevents_last_page_detection(self) -> None:
        response, rows = _response(error=None, rows=[{"item": "FIRST"}], size=1000)
        response["text"] = ('<span class="s-pagination-next s-pagination-disabled">Next</span>'
                            '<a class="s-pagination-next" href="?page=2">Next</a>')
        self.assertFalse(listing._main_last_page(response, rows))


if __name__ == "__main__":
    unittest.main()
