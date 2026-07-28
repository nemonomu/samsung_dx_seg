from __future__ import annotations

import argparse
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import listing, pipeline


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


if __name__ == "__main__":
    unittest.main()
