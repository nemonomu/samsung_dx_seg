from __future__ import annotations

import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import full_output  # noqa: E402
from common import notify  # noqa: E402
from common.parsers import parse_review_html  # noqa: E402


PLACEHOLDER_HTML = b"""
<html><body><div class="js_pdp_cr-summary" hidden></div></body></html>
"""

RENDERED_HTML = b"""
<html><body>
  <section class="js_pdp_cr-summary pdp_cr-summary">
    <h3>Das sagen unsere Kunden</h3>
    <div class="pdp_cr-summary__item-content">
      <span>Waschmaschine arbeitet sehr leise beim Schleudern</span>
      <a>Bewertungen ansehen</a>
    </div>
    <div class="pdp_cr-summary__item-content">
      <span>Bedienung ist einfach und selbsterklaerend</span>
      <a>Bewertungen ansehen</a>
    </div>
    <p>Ist diese Zusammenfassung hilfreich?</p>
    <button>Nicht hilfreich</button>
  </section>
</body></html>
"""


class OttoReviewSummaryTests(unittest.TestCase):
    def test_parser_keeps_only_summary_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.html"
            path.write_bytes(RENDERED_HTML)
            parsed = parse_review_html(path)

        self.assertEqual(
            "Waschmaschine arbeitet sehr leise beim Schleudern ||| "
            "Bedienung ist einfach und selbsterklaerend",
            parsed["summarized_review_content"],
        )
        self.assertTrue(parsed["summary_placeholder_present"])
        self.assertTrue(parsed["summary_container_present"])
        self.assertTrue(parsed["summary_rendered"])
        self.assertEqual(2, parsed["summary_item_count"])

    def test_summary_required_retries_placeholder_until_rendered(self) -> None:
        responses = [
            {"status": 200, "body": PLACEHOLDER_HTML, "error": None},
            {"status": 200, "body": RENDERED_HTML, "error": None},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", side_effect=responses) as fetch, \
                patch.object(full_output.raw_html, "save"), \
                patch.object(full_output.time, "sleep"):
            parsed = full_output.collect_review(
                "https://www.otto.de/kundenbewertungen/test/",
                Path(tmpdir),
                "test",
                require_summary=True,
                summary_attempts=6,
            )

        self.assertEqual(2, fetch.call_count)
        self.assertEqual(2, parsed["_summary_attempts"])
        self.assertTrue(parsed["_summary_eligible"])
        self.assertTrue(parsed["_summary_rendered"])
        self.assertIsNone(parsed["_summary_failure_reason"])
        self.assertNotIn("Bewertungen ansehen", parsed["summarized_review_content"])

    def test_no_summary_source_does_not_retry(self) -> None:
        response = {"status": 200, "body": b"<html><body>No AI summary</body></html>", "error": None}
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", return_value=response) as fetch, \
                patch.object(full_output.raw_html, "save"), \
                patch.object(full_output.time, "sleep"):
            parsed = full_output.collect_review(
                "https://www.otto.de/kundenbewertungen/test/",
                Path(tmpdir),
                "test",
                require_summary=True,
                summary_attempts=6,
            )

        self.assertEqual(3, fetch.call_count)
        self.assertEqual(3, parsed["_summary_attempts"])
        self.assertEqual("no_source", parsed["_summary_failure_reason"])
        self.assertFalse(parsed["_summary_eligible"])
        self.assertFalse(parsed["_summary_rendered"])

    def test_rendered_container_without_expected_items_is_selector_mismatch(self) -> None:
        response = {
            "status": 200,
            "body": b'<html><body><section class="js_pdp_cr-summary pdp_cr-summary">changed</section></body></html>',
            "error": None,
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", return_value=response) as fetch, \
                patch.object(full_output.raw_html, "save"), \
                patch.object(full_output.time, "sleep"):
            parsed = full_output.collect_review(
                "https://www.otto.de/kundenbewertungen/test/",
                Path(tmpdir),
                "test",
                require_summary=True,
            )

        self.assertEqual(4, fetch.call_count)
        self.assertEqual("selector_mismatch", parsed["_summary_failure_reason"])
        self.assertTrue(parsed["_summary_container_present"])

    def test_marker_seen_once_keeps_retrying_until_later_summary(self) -> None:
        no_marker = b"<html><body>No AI summary marker</body></html>"
        responses = [
            {"status": 200, "body": no_marker, "error": None},
            {"status": 200, "body": PLACEHOLDER_HTML, "error": None},
            {"status": 200, "body": no_marker, "error": None},
            {"status": 200, "body": no_marker, "error": None},
            {"status": 200, "body": RENDERED_HTML, "error": None},
        ]
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", side_effect=responses) as fetch, \
                patch.object(full_output.raw_html, "save"), \
                patch.object(full_output.time, "sleep"):
            parsed = full_output.collect_review(
                "https://www.otto.de/kundenbewertungen/test/",
                Path(tmpdir),
                "test",
                require_summary=True,
                summary_attempts=6,
            )

        self.assertEqual(5, fetch.call_count)
        self.assertTrue(parsed["_summary_rendered"])
        self.assertEqual(5, parsed["_summary_attempts"])

    def test_http_failure_is_returned_as_diagnostic_metadata(self) -> None:
        response = {"status": 503, "body": b"", "error": "service unavailable"}
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(full_output, "fetch_html", return_value=response), \
                patch.object(full_output.raw_html, "save"):
            parsed = full_output.collect_review(
                "https://www.otto.de/kundenbewertungen/test/",
                Path(tmpdir),
                "test",
                require_summary=True,
            )

        self.assertEqual(503, parsed["_review_status"])
        self.assertEqual("http_status=503", parsed["_summary_failure_reason"])

    def test_email_report_flags_eligible_missing_and_ui_text(self) -> None:
        class FakeConfig:
            PRODUCT = "LDY"
            SPEC_FIELDS: list[str] = []

        rows = [{
            "main_rank": "1",
            "bsr_rank": "",
            "summarized_review_content": "Das sagen unsere Kunden ||| useful summary text",
        }]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "step02_final_targets_manifest.json").write_text(
                json.dumps({"main_target_unique": 1, "bsr_rank_limit": 0}), encoding="utf-8"
            )
            (out / "step09_full_output_manifest.json").write_text(
                json.dumps({
                    "summary_qa": {
                        "checked": 1,
                        "eligible": 1,
                        "rendered": 0,
                        "eligible_missing": 1,
                        "no_source": 0,
                    }
                }),
                encoding="utf-8",
            )
            (out / "step14_db_save_manifest.json").write_text(
                json.dumps({"success": True, "dry_run": False, "inserted": 1}), encoding="utf-8"
            )
            with patch.object(notify, "category_output_root", return_value=out):
                subject, report = notify.build_report(FakeConfig(), rows)

        self.assertTrue(subject.startswith("[CHECK]"))
        self.assertIn("eligible missing - 1", report)
        self.assertIn("UI text contamination - 1", report)


if __name__ == "__main__":
    unittest.main()
