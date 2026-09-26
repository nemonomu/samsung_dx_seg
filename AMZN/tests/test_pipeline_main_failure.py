from __future__ import annotations

import argparse
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import email_report, listing, pipeline


class PipelineMainFailureTests(unittest.TestCase):
    def test_main_failure_skips_bsr_detail_full_and_db(self) -> None:
        args = argparse.Namespace(
            only="all",
            limit=0,
            max_detail=None,
            start=1,
            max_pages=30,
            max_rank=300,
            bsr_max_rank=100,
            bsr_retries=2,
            bsr_min_rank=97,
            bsr_page_load_strategies="eager,none,eager",
            db_dry_run=False,
            detail_sleep=1.5,
            headless=False,
            streaming_insert=False,
            no_auto_insert=False,
            email_report=False,
        )
        cfg = SimpleNamespace(PRODUCT="REF", POSTAL_CODE="10117")
        session = Mock()
        fake_browser = types.ModuleType("common.browser")
        fake_browser.AmazonBrowserSession = Mock(return_value=session)

        with (
            patch.dict(sys.modules, {"common.browser": fake_browser}),
            patch.object(pipeline, "category_output_root", return_value=Path(".")),
            patch.object(pipeline, "write_json"),
            patch.object(pipeline, "append_jsonl"),
            patch.object(pipeline, "run_meta", return_value={"batch_id": "a_test"}),
            patch.object(pipeline.siel_log, "setup_run", return_value="run.log"),
            patch.object(pipeline.siel_log, "run_log"),
            patch.object(pipeline.siel_log, "log_record_event"),
            patch.object(
                pipeline.listing,
                "run",
                side_effect=listing.MainListingUnavailableError(
                    "main listing unavailable after 3 attempts: amazon_technical_error"
                ),
            ),
            patch.object(pipeline, "_run_bsr_with_retries") as bsr_run,
            patch.object(pipeline.targets, "run") as targets_run,
            patch.object(pipeline.detail, "run") as detail_run,
            patch.object(pipeline.full_output, "run") as full_run,
            patch.object(pipeline.merge_insert, "insert_jsonl") as db_run,
            patch.object(pipeline.notify, "run", return_value={"severity": "warning", "sent": False, "error": None}),
        ):
            status = pipeline.run(cfg, args)

        self.assertEqual(status, 1)
        bsr_run.assert_not_called()
        targets_run.assert_not_called()
        detail_run.assert_not_called()
        full_run.assert_not_called()
        db_run.assert_not_called()
        session.close.assert_called_once()

    def test_partial_main_runs_downstream_but_returns_failure_and_warns(self) -> None:
        args = argparse.Namespace(
            only="all", limit=0, max_detail=None, start=1, max_pages=30,
            max_rank=300, bsr_max_rank=100, bsr_retries=2, bsr_min_rank=97,
            bsr_page_load_strategies="eager,none,eager", db_dry_run=False,
            detail_sleep=1.5, headless=False, streaming_insert=False,
            no_auto_insert=False, email_report=True,
        )
        cfg = SimpleNamespace(PRODUCT="REF", POSTAL_CODE="10117")
        fake_browser = types.ModuleType("common.browser")
        fake_browser.AmazonBrowserSession = Mock(return_value=Mock())
        records = []

        def partial_main(*_args, emit, **_kwargs):
            emit({
                "stage": "listing_error", "error_stage": "main", "product": "REF",
                "page_no": 4, "source_url": "https://example.invalid/?page=4",
                "_error": "listing page load failed",
                "message": "main listing incomplete: page=4 reason=amazon_technical_error; remaining pages were not collected",
            })
            return {"success": False, "failed_page": 4, "failure_reason": "amazon_technical_error"}

        with (
            patch.dict(sys.modules, {"common.browser": fake_browser}),
            patch.object(pipeline, "category_output_root", return_value=Path(".")),
            patch.object(pipeline, "write_json"),
            patch.object(pipeline, "append_jsonl", side_effect=lambda _path, row: records.append(row)),
            patch.object(pipeline, "run_meta", return_value={"batch_id": "a_test"}),
            patch.object(pipeline.siel_log, "setup_run", return_value="run.log"),
            patch.object(pipeline.siel_log, "run_log"),
            patch.object(pipeline.siel_log, "log_record_event"),
            patch.object(pipeline.listing, "run", side_effect=partial_main),
            patch.object(pipeline, "_run_bsr_with_retries") as bsr_run,
            patch.object(pipeline.targets, "run", return_value={"unique_targets": 117}),
            patch.object(pipeline.detail, "run", return_value={"success": True, "rows": 117, "targets": 117}),
            patch.object(pipeline.full_output, "run", return_value={"output_rows": 117}) as full_run,
            patch.object(pipeline.merge_insert, "insert_jsonl", return_value={"rows_full": 117, "inserted_total": 117, "success": True}) as db_run,
            patch.object(pipeline.notify, "run", return_value={"severity": "warning", "sent": False}) as notify_run,
        ):
            status = pipeline.run(cfg, args)

        self.assertEqual(status, 1)
        bsr_run.assert_called_once()
        full_run.assert_called_once()
        db_run.assert_called_once()
        notify_run.assert_called_once()
        self.assertFalse(any(row.get("_fatal") for row in records))
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(email_report, "read_jsonl", return_value=records),
        ):
            body, severity = email_report.build_email_report_with_severity(cfg, "test.jsonl")
        self.assertEqual(severity, "warning")
        self.assertIn("listing page failures: 1", body)
        self.assertIn("page=4", body)
        self.assertIn("remaining pages were not collected", body)


if __name__ == "__main__":
    unittest.main()
