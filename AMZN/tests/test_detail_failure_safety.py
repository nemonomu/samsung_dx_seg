from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run as run_module
from common import email_report, merge_insert, pipeline


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        only="detail,full,db,notify",
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
        email_report=True,
    )


class DetailFailureSafetyTests(unittest.TestCase):
    def test_zero_detail_targets_fail_the_completeness_gate(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "targets=0"):
            pipeline._require_complete_detail({"success": True, "rows": 0, "targets": 0})

    def test_incomplete_detail_manifest_skips_full_and_db_and_notifies(self) -> None:
        cfg = SimpleNamespace(PRODUCT="REF", POSTAL_CODE="10117")
        session = Mock()
        fake_browser = types.ModuleType("common.browser")
        fake_browser.AmazonBrowserSession = Mock(return_value=session)
        append = Mock()

        with (
            patch.dict(sys.modules, {"common.browser": fake_browser}),
            patch.object(pipeline, "category_output_root", return_value=Path(".")),
            patch.object(pipeline, "write_json"),
            patch.object(pipeline, "append_jsonl", append),
            patch.object(pipeline, "run_meta", return_value={"batch_id": "a_test"}),
            patch.object(pipeline.siel_log, "setup_run", return_value="run.log"),
            patch.object(pipeline.siel_log, "run_log"),
            patch.object(pipeline.siel_log, "log_record_event"),
            patch.object(
                pipeline.detail,
                "run",
                return_value={"success": False, "rows": 87, "targets": 327},
            ),
            patch.object(pipeline.full_output, "run") as full_run,
            patch.object(pipeline.merge_insert, "insert_jsonl") as db_run,
            patch.object(
                pipeline.notify,
                "run",
                return_value={"severity": "sos", "sent": True, "error": None},
            ) as notify_run,
        ):
            status = pipeline.run(cfg, _args())

        self.assertEqual(status, 1)
        full_run.assert_not_called()
        db_run.assert_not_called()
        notify_run.assert_called_once_with(cfg)
        emitted = [call.args[1] for call in append.call_args_list]
        self.assertTrue(any(rec.get("stage") == "run_error" and rec.get("_fatal") for rec in emitted))
        session.close.assert_called_once()

    def test_fatal_run_error_makes_email_sos_without_db_summary(self) -> None:
        records = [
            {
                "stage": "detail",
                "product": "REF",
                "asin": "B0OK",
                "item": "B0OK",
                "product_url": "https://www.amazon.de/dp/B0OK",
                "sku": "RF-OK",
            },
            {
                "stage": "detail_error",
                "product": "REF",
                "asin": "B0FAIL",
                "_error": "detail browser recovery exhausted",
                "_fatal": True,
                "error_stage": "detail",
                "message": "new Chrome failed twice",
            },
        ]

        with (
            patch.object(email_report.Path, "exists", return_value=True),
            patch.object(email_report, "read_jsonl", return_value=records),
        ):
            body, severity = email_report.build_email_report_with_severity("REF", "run.jsonl")

        self.assertEqual(severity, "sos")
        self.assertIn("fatal run errors (DB insert skipped): 1", body)
        self.assertNotIn("db insert rows = 0", body)

    def test_429_and_timeout_are_warning_email_items_not_sos(self) -> None:
        records = [
            {
                "stage": "detail",
                "product": "TV",
                "asin": "B0BLOCKED",
                "item": "B0BLOCKED",
                "product_url": "https://www.amazon.de/dp/B0BLOCKED",
                "sku": "TV-BLOCKED",
                "_transport_warning": "amazon_429",
            },
            {
                "stage": "detail",
                "product": "TV",
                "asin": "B0TIMEOUT",
                "item": "B0TIMEOUT",
                "product_url": "https://www.amazon.de/dp/B0TIMEOUT",
                "sku": "TV-TIMEOUT",
                "_transport_warning": "timeout",
            },
        ]

        with (
            patch.object(email_report.Path, "exists", return_value=True),
            patch.object(email_report, "read_jsonl", return_value=records),
        ):
            body, severity = email_report.build_email_report_with_severity("TV", "run.jsonl")

        self.assertEqual(severity, "warning")
        self.assertIn("Amazon 429 차단: 1", body)
        self.assertIn("ASIN=B0BLOCKED", body)
        self.assertIn("페이지 타임아웃(재시도 후에도 실패): 1", body)
        self.assertIn("ASIN=B0TIMEOUT", body)
        self.assertNotIn("DB insert skipped", body)

    def test_transport_warning_detail_row_still_merges_for_db(self) -> None:
        cfg = SimpleNamespace(PRODUCT="TV")
        records = [
            {
                "stage": "main",
                "asin": "B0TIMEOUT",
                "item": "B0TIMEOUT",
                "product_url": "https://www.amazon.de/dp/B0TIMEOUT",
                "main_rank": 1,
            },
            {
                "stage": "detail",
                "asin": "B0TIMEOUT",
                "item": "B0TIMEOUT",
                "product_url": "https://www.amazon.de/dp/B0TIMEOUT",
                "_transport_warning": "timeout",
            },
        ]

        with patch.object(merge_insert, "read_jsonl", return_value=records):
            rows = merge_insert.merge_jsonl(cfg, "run.jsonl")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["item"], "B0TIMEOUT")
        self.assertNotIn("_transport_warning", rows[0])

    def test_daily_batch_files_default_to_batch_insert(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in ("run_tv.bat", "run_ref.bat", "run_tv_ref.bat"):
            with self.subTest(name=name):
                text = (root / name).read_text(encoding="utf-8")
                self.assertNotIn("--streaming-insert", text)

    def test_streaming_flag_is_ignored_and_success_uses_batch_insert(self) -> None:
        args = _args()
        args.only = "detail,db"
        args.streaming_insert = True
        args.email_report = False
        cfg = SimpleNamespace(PRODUCT="TV", POSTAL_CODE="10117")
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
                pipeline.detail,
                "run",
                return_value={"success": True, "rows": 1, "targets": 1},
            ),
            patch.object(pipeline.merge_insert, "StreamingRetailInserter") as streaming,
            patch.object(
                pipeline.merge_insert,
                "insert_jsonl",
                return_value={"success": True, "inserted_total": 1},
            ) as batch_insert,
        ):
            status = pipeline.run(cfg, args)

        self.assertEqual(status, 0)
        streaming.assert_not_called()
        batch_insert.assert_called_once()
        self.assertEqual(batch_insert.call_args.kwargs["expected_rows"], 1)

    def test_jsonl_row_mismatch_stops_before_insert_rows(self) -> None:
        cfg = SimpleNamespace(PRODUCT="REF")
        with (
            patch.object(merge_insert, "merge_jsonl", return_value=[{"item": "B0ONE"}]),
            patch.object(merge_insert, "insert_rows") as insert_rows,
        ):
            with self.assertRaisesRegex(RuntimeError, "merged_rows=1 expected_rows=2"):
                merge_insert.insert_jsonl(cfg, "run.jsonl", expected_rows=2)

        insert_rows.assert_not_called()

    def test_redirect_listing_only_marker_is_excluded_from_batch_preview(self) -> None:
        for product in ("TV", "REF"):
            with self.subTest(product=product):
                cfg = SimpleNamespace(PRODUCT=product, DB_TABLE="dx_seg.test_table")
                main = {
                    "stage": "main",
                    "asin": "B0LISTING1",
                    "item": "B0LISTING1",
                    "product_url": "https://www.amazon.de/dp/B0LISTING1",
                    "retailer_sku_name": f"Listing {product}",
                    "main_rank": 1,
                }
                detail = {
                    "stage": "detail",
                    "asin": "B0LISTING1",
                    "item": "B0REDIRECT",
                    "product_url": "https://www.amazon.de/dp/B0LISTING1",
                    "redirect": True,
                    "_detail_skip": "url_mismatch_name_mismatch",
                }
                row = merge_insert.make_row(cfg, main, None, detail)
                self.assertIsNotNone(row)
                self.assertTrue(row.get("_redirect_listing_only"))

                with tempfile.TemporaryDirectory() as tmp_dir:
                    output_root = Path(tmp_dir)
                    with patch.object(
                        merge_insert, "category_output_root", return_value=output_root
                    ):
                        manifest = merge_insert.insert_rows(cfg, [row], dry_run=True)

                    with (output_root / "amzn_full_output.csv").open(
                        newline="", encoding="utf-8-sig"
                    ) as fh:
                        reader = csv.DictReader(fh)
                        preview_rows = list(reader)
                        fieldnames = reader.fieldnames or []

                self.assertTrue(manifest["success"])
                self.assertNotIn("_redirect_listing_only", fieldnames)
                self.assertEqual(len(preview_rows), 1)
                self.assertEqual(preview_rows[0]["item"], "B0LISTING1")

    def test_combined_runner_continues_after_unexpected_tv_error(self) -> None:
        tv_cfg = SimpleNamespace(PRODUCT="TV")
        ref_cfg = SimpleNamespace(PRODUCT="REF")
        with (
            patch.object(run_module, "_load_config", side_effect=[tv_cfg, ref_cfg]),
            patch.object(
                run_module.pipeline,
                "run",
                side_effect=[RuntimeError("unexpected TV failure"), 0],
            ) as pipeline_run,
        ):
            status = run_module.main(["--product", "tv", "ref", "--no-auto-insert"])

        self.assertEqual(status, 1)
        self.assertEqual(pipeline_run.call_count, 2)
        self.assertIs(pipeline_run.call_args_list[1].args[0], ref_cfg)


if __name__ == "__main__":
    unittest.main()
