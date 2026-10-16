from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import recovery_collect_db as recovery


def records(product="REF"):
    base = dict(product=product, account_name="Amazon", batch_id="a_original")
    return [
        dict(base, stage="main", asin="A", main_rank=1, product_url="https://www.amazon.de/dp/A"),
        dict(base, stage="main", asin="B", main_rank=2, product_url="https://www.amazon.de/dp/B"),
        dict(base, stage="bsr", asin="C", bsr_rank=1, product_url="https://www.amazon.de/dp/C"),
        dict(base, stage="detail", asin="A", crawl_datetime="2026-10-13 02:00:00", sku="original"),
    ]


def write_source(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "original run.jsonl"
        write_source(self.source, records())
        self.original = self.source.read_bytes()

    def state(self):
        return recovery.load_state(self.source, "ref")

    def initialize(self):
        state = self.state()
        with recovery.recovery_lock(state["folder"]):
            recovery.initialize(state)
        return state

    def complete(self, state, product="REF"):
        for row in state["missing"]:
            state["details"][row["asin"]] = dict(
                row, stage="detail", product=product, crawl_datetime="2026-10-13 08:00:00",
            )
        recovery.save_checkpoint(state)

    def test_both_products_and_noncontiguous_missing(self):
        for product in ("REF", "TV"):
            with self.subTest(product=product):
                rows = records(product)
                rows.append(dict(rows[2], stage="detail"))
                state = recovery.inspect_records(rows, product.lower())
                self.assertEqual([r["asin"] for r in state["missing"]], ["B"])
                self.assertEqual(state["batch_id"], "a_original")

    def test_invalid_sources_are_rejected(self):
        variants = []
        rows = records(); rows[0]["product"] = "TV"; variants.append(rows)
        rows = records(); rows.append(dict(rows[-1])); variants.append(rows)
        rows = records(); rows[-1]["batch_id"] = "another"; variants.append(rows)
        rows = records(); rows[-1]["asin"] = "unexpected"; variants.append(rows)
        rows = records(); rows[1]["product_url"] = "https://example.com/dp/B"; variants.append(rows)
        rows = records(); rows[0]["account_name"] = "Other"; variants.append(rows)
        rows = records(); del rows[2]; variants.append(rows)
        rows = records(); rows[-1].pop("batch_id"); variants.append(rows)
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                recovery.inspect_records(rows, "ref")

    def test_fatal_records_are_retried_but_source_is_preserved(self):
        rows = records()
        rows.extend([dict(rows[1], stage="detail_error", _fatal=True), dict(rows[2], stage="detail", _error="failed")])
        write_source(self.source, rows)
        original = self.source.read_bytes()
        state = self.initialize()
        self.assertEqual(len(state["missing"]), 2)
        saved, _ = recovery.read_records(state["checkpoint"])
        self.assertEqual(len(saved), 4)
        self.assertEqual(self.source.read_bytes(), original)

    def test_only_unterminated_broken_tail_can_be_ignored(self):
        self.source.write_bytes(self.original + b'{"stage":"detail","text":"\xec')
        self.assertTrue(self.state()["torn_tail"])
        for suffix in (b'{bad}\n', b'{bad}\n{}\n'):
            self.source.write_bytes(self.original + suffix)
            with self.assertRaises(ValueError):
                self.state()

    def test_checkpoint_does_not_overwrite_original_and_resumes(self):
        state = self.initialize()
        state["details"]["B"] = dict(records()[1], stage="detail")
        recovery.save_checkpoint(state)
        resumed = self.state()
        self.assertEqual([r["asin"] for r in resumed["missing"]], ["C"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(resumed["details"]["A"]["sku"], "original")

    def test_changed_source_or_checkpoint_cannot_be_mixed(self):
        state = self.initialize()
        self.source.write_bytes(self.original + b"\n")
        with self.assertRaises(ValueError):
            self.state()
        self.source.write_bytes(self.original)
        state["details"]["A"]["sku"] = "changed"
        recovery.save_checkpoint(state)
        with self.assertRaises(ValueError):
            self.state()

    def test_atomic_failure_leaves_previous_checkpoint_valid(self):
        state = self.initialize()
        previous = state["checkpoint"].read_bytes()
        state["details"]["B"] = dict(records()[1], stage="detail")
        with patch.object(recovery.os, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                recovery.save_checkpoint(state)
        self.assertEqual(state["checkpoint"].read_bytes(), previous)
        self.assertEqual(len(self.state()["missing"]), 2)

    def test_lock_blocks_second_process_and_releases(self):
        folder = self.state()["folder"]
        with recovery.recovery_lock(folder):
            with self.assertRaises(ValueError):
                with recovery.recovery_lock(folder):
                    self.fail("second lock acquired")
        with recovery.recovery_lock(folder):
            pass

    def test_check_and_initial_cancel_have_no_writes_or_runtime_imports(self):
        for args, answer in ((["--check"], "y"), ([], "n")):
            with patch("builtins.input", return_value=answer), patch.object(recovery, "collect_missing") as collect, patch.object(recovery, "export_and_load") as load, redirect_stdout(io.StringIO()):
                status = recovery.main(["--product", "ref", "--jsonl", str(self.source)] + args)
            self.assertEqual(status, 0)
            self.assertFalse((self.source.parent / "recovery").exists())
            collect.assert_not_called()
            load.assert_not_called()

    def test_changed_source_during_confirmation_is_blocked(self):
        def change(_):
            self.source.write_bytes(self.original + b"\n")
            return "y"
        with patch("builtins.input", side_effect=change), patch.object(recovery, "collect_missing") as collect, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(recovery.main(["--product", "ref", "--jsonl", str(self.source)]), 1)
        collect.assert_not_called()

    def test_collection_uses_only_missing_targets_and_persists_before_crash(self):
        from common.io_util import read_csv
        fake_detail = ModuleType("common.detail")
        state = self.initialize()

        def crash(cfg, **kwargs):
            selected = read_csv(state["folder"] / "amzn_final_targets.csv")
            self.assertEqual([r["asin"] for r in selected], ["B", "C"])
            self.assertEqual(kwargs["batch_id"], "a_original")
            kwargs["emit"](dict(records()[1], stage="detail", crawl_datetime="new time"))
            raise RuntimeError("browser crashed")

        fake_detail.run = crash
        with patch.dict(sys.modules, {"common.detail": fake_detail}), patch.object(recovery.importlib, "import_module", return_value=SimpleNamespace(PRODUCT="REF")), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                recovery.collect_missing(state, "ref")
        self.assertEqual([r["asin"] for r in self.state()["missing"]], ["C"])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_export_requires_full_validation_and_correct_insert_count(self):
        state = self.initialize()
        self.complete(state)
        state = self.state()
        fake = ModuleType("resume_db")
        fake.validate_resume_jsonl = Mock(return_value={"targets": 3, "details": 3})
        fake._load_config = Mock(return_value=SimpleNamespace(PRODUCT="REF"))
        fake.merge_insert = SimpleNamespace(insert_jsonl=Mock(return_value=dict(success=True, rows_full=3, skipped=False, inserted_total=2)))
        with patch.dict(sys.modules, {"resume_db": fake}):
            with self.assertRaises(ValueError):
                recovery.export_and_load(state, "ref", dry_run=False)
            fake.merge_insert.insert_jsonl.return_value["inserted_total"] = 3
            result = recovery.export_and_load(state, "ref", dry_run=False)
        self.assertEqual(result["inserted_total"], 3)
        self.assertEqual(fake.merge_insert.insert_jsonl.call_args.kwargs["expected_rows"], 3)
        self.assertTrue((state["folder"] / "amzn_detail.csv").exists())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_real_merge_exports_all_rows_without_db_connection(self):
        # Supply synthetic settings before importing legacy modules. Never read
        # runtime config, credentials, or contact the retailer/DB in this test.
        config = ModuleType("common.config")
        config.AMAZON_BASE = "https://www.amazon.de"
        config.run_meta = lambda prefix: {"calendar_week": "w42", "batch_id": "unused"}
        with patch.dict(sys.modules, {"common.config": config}):
            import resume_db
            from common.io_util import read_csv

            for product in ("ref", "tv"):
                with self.subTest(product=product):
                    self.source = Path(self.temp.name) / f"integration_{product}.jsonl"
                    write_source(self.source, records(product.upper()))
                    state = recovery.load_state(self.source, product)
                    with recovery.recovery_lock(state["folder"]):
                        recovery.initialize(state)
                        self.complete(state, product.upper())
                    state = recovery.load_state(self.source, product)
                    cfg = SimpleNamespace(PRODUCT=product.upper(), DB_TABLE="dx_seg.test_recovery")
                    with patch.object(resume_db, "_load_config", return_value=cfg), patch.object(resume_db.merge_insert, "_connect", side_effect=AssertionError("DB must not connect")):
                        manifest = recovery.export_and_load(state, product, dry_run=True)
                    merged = read_csv(state["folder"] / "amzn_full_output.csv")
                    self.assertEqual(manifest["rows_full"], 3)
                    self.assertEqual({r["item"] for r in merged}, {"A", "B", "C"})
                    self.assertEqual({r["batch_id"] for r in merged}, {"a_original"})
                    self.assertEqual(merged[0]["crawl_strdatetime"], "2026-10-13 02:00:00")
                    self.assertEqual(merged[1]["crawl_strdatetime"], "2026-10-13 08:00:00")

    def run_cli(self, answers, *, collector=None, exporter=None, product="ref"):
        with patch("builtins.input", side_effect=answers), patch.object(recovery, "collect_missing", side_effect=collector) as collect, patch.object(recovery, "export_and_load", side_effect=exporter) as export, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = recovery.main(["--product", product, "--jsonl", str(self.source)])
        return result, collect, export

    def test_end_to_end_flow_both_categories_auto_loads_after_single_confirmation(self):
        for product in ("ref", "tv"):
            with self.subTest(product=product):
                self.source = Path(self.temp.name) / f"{product}.jsonl"
                write_source(self.source, records(product.upper()))
                def collect(state, selected_product, **kwargs):
                    self.assertEqual(selected_product, product)
                    self.complete(state, product.upper())
                result, crawler, export = self.run_cli(["y"], collector=collect, product=product)
                self.assertEqual(result, 0)
                self.assertEqual(crawler.call_count, 1)
                self.assertEqual([c.kwargs["dry_run"] for c in export.call_args_list], [True, False])

    def test_db_failure_preserves_completed_collection_for_next_run(self):
        def collect(state, product, **kwargs):
            self.complete(state)
        def fail_insert(state, product, *, dry_run):
            if not dry_run:
                raise RuntimeError("DB unavailable")
        result, _, export = self.run_cli(["y"], collector=collect, exporter=fail_insert)
        self.assertEqual(result, 1)
        self.assertEqual([c.kwargs["dry_run"] for c in export.call_args_list], [True, False])
        result, collect, export = self.run_cli(["y"])
        self.assertEqual(result, 0)
        collect.assert_not_called()
        self.assertEqual([c.kwargs["dry_run"] for c in export.call_args_list], [True, False])

    def test_incomplete_collection_or_crash_never_loads_db(self):
        for callback in (None, RuntimeError("browser crash")):
            result, _, export = self.run_cli(["y"], collector=callback)
            self.assertEqual(result, 1)
            export.assert_not_called()

    def test_modified_checkpoint_after_validation_blocks_db(self):
        def collect(state, product, **kwargs):
            self.complete(state)
        def change_checkpoint(state, product, *, dry_run):
            if dry_run:
                checkpoint = state["checkpoint"]
                checkpoint.write_bytes(checkpoint.read_bytes() + b"\n")
        result, _, export = self.run_cli(["y"], collector=collect, exporter=change_checkpoint)
        self.assertEqual(result, 1)
        self.assertEqual([c.kwargs["dry_run"] for c in export.call_args_list], [True])

    def test_failed_validation_never_auto_loads_db(self):
        def collect(state, product, **kwargs):
            self.complete(state)
        result, _, export = self.run_cli(["y"], collector=collect, exporter=ValueError("validation failed"))
        self.assertEqual(result, 1)
        self.assertEqual([c.kwargs["dry_run"] for c in export.call_args_list], [True])

    def test_output_override_is_restored_on_error(self):
        name = "AMZN_REF_OUTPUT_ROOT"
        with patch.dict(os.environ, {name: "previous-folder"}):
            with self.assertRaises(RuntimeError):
                with recovery.output_folder("ref", Path("new-folder")):
                    self.assertEqual(os.environ[name], "new-folder")
                    raise RuntimeError("stop")
            self.assertEqual(os.environ[name], "previous-folder")

    def test_batch_entrypoints_only_call_matching_interactive_recovery(self):
        root = Path(__file__).resolve().parents[1]
        for product in ("ref", "tv"):
            text = (root / f"recovery_{product}_collect_db.bat").read_text(encoding="utf-8")
            self.assertIn(f"recovery_collect_db.py --product {product}", text)
            self.assertIn('"%~1"', text)
            self.assertIn("DisableDelayedExpansion", text)
            self.assertNotIn("--email-report", text)
            self.assertNotIn("run.py --product", text)

    def test_auto_select_uses_filename_time_and_ignores_other_products_and_recovery(self):
        folder = Path(self.temp.name) / "output"
        folder.mkdir()
        for product in ("ref", "tv"):
            older = folder / f"amzn_{product}_run_20261012_090000.jsonl"
            latest = folder / f"amzn_{product}_run_20261013_010000.jsonl"
            write_source(older, records(product.upper()))
            write_source(latest, records(product.upper()))
            os.utime(older, (2000000000, 2000000000))
            nested = folder / "recovery" / product
            nested.mkdir(parents=True)
            write_source(nested / f"amzn_{product}_run_20261014_010000.jsonl", records(product.upper()))
            write_source(folder / f"amzn_{product}_run_20261099_010000.jsonl", records(product.upper()))
            write_source(folder / f"amzn_{product}_run_20261015_010000.recovered.jsonl", records(product.upper()))
            self.assertEqual(recovery.select_source(product, folder), latest)
            self.assertEqual(recovery.select_source(product, older), older)

    def test_data_folder_and_default_output_are_supported_without_path_prompt(self):
        for product in ("ref", "tv"):
            folder = Path(self.temp.name) / product / "data" / "output"
            folder.mkdir(parents=True)
            latest = folder / f"amzn_{product}_run_20261013_010000.jsonl"
            write_source(latest, records(product.upper()))
            self.assertEqual(recovery.select_source(product, folder.parent), latest)
            output = io.StringIO()
            with patch.object(recovery, "default_output_folder", return_value=folder), patch("builtins.input", side_effect=AssertionError("no path prompt")), redirect_stdout(output):
                status = recovery.main(["--product", product, "--check"])
            self.assertEqual(status, 0)
            self.assertIn(latest.name, output.getvalue())
            self.assertIn("전체 3건 / 완료 1건 / 미수집 2건", output.getvalue())
            self.assertFalse((folder / "recovery").exists())

    def test_auto_selected_file_still_requires_confirmation_before_any_writes(self):
        latest = Path(self.temp.name) / "amzn_ref_run_20261013_010000.jsonl"
        write_source(latest, records())
        with patch.object(recovery, "default_output_folder", return_value=latest.parent), patch("builtins.input", return_value="n") as prompt, patch.object(recovery, "collect_missing") as collect, patch.object(recovery, "export_and_load") as load, redirect_stdout(io.StringIO()):
            status = recovery.main(["--product", "ref"])
        self.assertEqual(status, 0)
        prompt.assert_called_once()
        self.assertIn("복구 및 DB 자동 적재", prompt.call_args.args[0])
        collect.assert_not_called()
        load.assert_not_called()
        self.assertFalse((latest.parent / "recovery").exists())

    def test_no_candidate_or_ambiguous_timestamp_stops(self):
        folder = Path(self.temp.name)
        with self.assertRaises(ValueError):
            recovery.select_source("tv", folder)
        name = "amzn_ref_run_20261013_010000.jsonl"
        write_source(folder / name, records())
        (folder / "output").mkdir()
        write_source(folder / "output" / name, records())
        with self.assertRaises(ValueError):
            recovery.select_source("ref", folder)

    def test_invalid_latest_file_does_not_fall_back_to_older_batch(self):
        folder = Path(self.temp.name)
        write_source(folder / "amzn_ref_run_20261012_010000.jsonl", records())
        (folder / "amzn_ref_run_20261013_010000.jsonl").write_text("broken\n", encoding="utf-8")
        with patch.object(recovery, "default_output_folder", return_value=folder), patch("builtins.input", side_effect=AssertionError("must stop before confirmation")), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = recovery.main(["--product", "ref"])
        self.assertEqual(status, 1)
        self.assertFalse((folder / "recovery").exists())


if __name__ == "__main__":
    unittest.main()
