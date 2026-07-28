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
    def _run(self, session: FakeSession, raw_root: Path) -> dict:
        cfg = SimpleNamespace(PRODUCT="REF", ACCOUNT_NAME="Amazon.de", MAIN_URL="https://www.amazon.de/s?k=test")
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
            patch.object(listing, "save_text"),
        ):
            return listing.run(
                cfg,
                sort="main",
                target=1,
                max_pages=1,
                batch_id="a_test",
                session=session,
            )

    def test_same_session_retry_recovers_technical_error(self) -> None:
        session = FakeSession([
            _response(error="amazon_technical_error", rows=[], size=2294),
            _response(error=None, rows=[{"item": "B0TEST"}], size=1_500_000),
        ])
        with patch.object(listing.time, "sleep"):
            manifest = self._run(session, Path("test-output"))

        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(session.refetch_calls, 1)
        self.assertEqual(session.restart_calls, 0)
        self.assertEqual(manifest["pages"][0]["retry_attempts"][-1]["mode"], "same_session")

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


if __name__ == "__main__":
    unittest.main()
