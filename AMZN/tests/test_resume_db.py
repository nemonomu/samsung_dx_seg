from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import resume_db


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _complete_records(product: str = "TV") -> list[dict]:
    return [
        {"stage": "main", "product": product, "asin": "B0MAIN", "item": "B0MAIN"},
        {"stage": "bsr", "product": product, "asin": "B0BSR", "item": "B0BSR"},
        {
            "stage": "detail",
            "product": product,
            "asin": "B0MAIN",
            "item": "B0MAIN",
            "batch_id": "a_batch",
        },
        {
            "stage": "detail",
            "product": product,
            "asin": "B0BSR",
            "item": "B0BSR",
            "batch_id": "a_batch",
        },
    ]


class ResumeDbTests(unittest.TestCase):
    def test_validation_accepts_one_detail_per_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            _write_jsonl(path, _complete_records())

            result = resume_db.validate_resume_jsonl(path, "tv")

        self.assertEqual(result["targets"], 2)
        self.assertEqual(result["details"], 2)
        self.assertEqual(result["batch_ids"], ["a_batch"])

    def test_validation_rejects_incomplete_detail_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            records = _complete_records()
            _write_jsonl(path, records[:-1])

            with self.assertRaisesRegex(ValueError, "completeness check failed"):
                resume_db.validate_resume_jsonl(path, "tv")

    def test_validation_rejects_wrong_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            _write_jsonl(path, _complete_records("REF"))

            with self.assertRaisesRegex(ValueError, "product mismatch"):
                resume_db.validate_resume_jsonl(path, "tv")

    def test_validation_rejects_multiple_detail_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            records = _complete_records()
            records[-1]["batch_id"] = "a_other_batch"
            _write_jsonl(path, records)

            with self.assertRaisesRegex(ValueError, "exactly one detail batch_id required"):
                resume_db.validate_resume_jsonl(path, "tv")

    def test_dry_run_calls_only_existing_jsonl_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            _write_jsonl(path, _complete_records())
            cfg = SimpleNamespace(PRODUCT="TV")
            manifest = {
                "success": True,
                "rows_full": 2,
                "inserted_total": 0,
                "skipped": True,
                "preview_csv": str(Path(tmp_dir) / "amzn_full_output.csv"),
            }
            with (
                patch.object(resume_db, "_load_config", return_value=cfg),
                patch.object(
                    resume_db.merge_insert,
                    "insert_jsonl",
                    return_value=manifest,
                ) as insert_jsonl,
            ):
                status = resume_db.main(
                    ["--product", "tv", "--jsonl", str(path), "--dry-run"]
                )

        self.assertEqual(status, 0)
        insert_jsonl.assert_called_once_with(
            cfg,
            path.resolve(),
            dry_run=True,
            expected_rows=2,
        )

    def test_interactive_cancel_stops_after_dry_run(self) -> None:
        validation = {"targets": 2, "details": 2}
        dry_manifest = {
            "success": True,
            "rows_full": 2,
            "inserted_total": 0,
            "skipped": True,
            "preview_csv": "amzn_full_output.csv",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            with (
                patch.object(
                    resume_db,
                    "_execute_resume",
                    return_value=(validation, dry_manifest),
                ) as execute,
                patch.object(resume_db, "_file_digest", return_value="same"),
                patch("builtins.input", return_value="n"),
            ):
                status = resume_db.main(
                    ["--product", "tv", "--jsonl", str(path), "--interactive"]
                )

        self.assertEqual(status, 0)
        execute.assert_called_once_with("tv", path.resolve(), dry_run=True)

    def test_interactive_prompt_accepts_quoted_dragged_path(self) -> None:
        validation = {"targets": 1, "details": 1}
        dry_manifest = {
            "success": True,
            "rows_full": 1,
            "inserted_total": 0,
            "skipped": True,
            "preview_csv": "amzn_full_output.csv",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run with spaces.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            with (
                patch.object(
                    resume_db,
                    "_execute_resume",
                    return_value=(validation, dry_manifest),
                ) as execute,
                patch.object(resume_db, "_file_digest", return_value="same"),
                patch("builtins.input", side_effect=[f'"{path}"', "n"]),
            ):
                status = resume_db.main(["--product", "tv", "--interactive"])

        self.assertEqual(status, 0)
        execute.assert_called_once_with("tv", path.resolve(), dry_run=True)

    def test_interactive_yes_runs_dry_run_then_insert(self) -> None:
        validation = {"targets": 2, "details": 2}
        dry_manifest = {
            "success": True,
            "rows_full": 2,
            "inserted_total": 0,
            "skipped": True,
            "preview_csv": "amzn_full_output.csv",
        }
        insert_manifest = {
            "success": True,
            "rows_full": 2,
            "inserted_total": 2,
            "skipped": False,
            "preview_csv": "amzn_full_output.csv",
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "run.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            with (
                patch.object(
                    resume_db,
                    "_execute_resume",
                    side_effect=[
                        (validation, dry_manifest),
                        (validation, insert_manifest),
                    ],
                ) as execute,
                patch.object(resume_db, "_file_digest", return_value="same"),
                patch("builtins.input", return_value="y"),
            ):
                status = resume_db.main(
                    ["--product", "ref", "--jsonl", str(path), "--interactive"]
                )

        self.assertEqual(status, 0)
        self.assertEqual(
            execute.call_args_list,
            [
                call("ref", path.resolve(), dry_run=True),
                call("ref", path.resolve(), dry_run=False),
            ],
        )

    def test_product_batch_files_use_interactive_resume_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for product in ("tv", "ref"):
            with self.subTest(product=product):
                text = (root / f"resume_{product}_db.bat").read_text(encoding="utf-8")
                self.assertIn("resume_db.py", text)
                self.assertIn(f"--product {product}", text)
                self.assertIn("--interactive", text)
                self.assertIn('"%~1"', text)
                self.assertNotIn("run.py --product", text)
                self.assertNotIn("--email-report", text)


if __name__ == "__main__":
    unittest.main()
