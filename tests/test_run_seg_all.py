from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_seg_all as seg


class SegLogTests(unittest.TestCase):
    NOW = datetime(2026, 8, 18, 12, 0, 0)

    @staticmethod
    def _set_modified(path: Path, when: datetime) -> None:
        timestamp = when.timestamp()
        os.utime(path, (timestamp, timestamp))

    def test_cleanup_deletes_only_exact_old_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_log = root / "seg_20260808_115959_000000.log"
            boundary = root / "seg_20260808_120000_000000.log"
            recent = root / "seg_20260818_110000_000000.log"
            unrelated = root / "manual.log"
            matching_directory = root / "seg_20260801_120000_000000.log"
            for path in (old_log, boundary, recent, unrelated):
                path.write_text("test", encoding="utf-8")
            matching_directory.mkdir()
            self._set_modified(old_log, self.NOW - timedelta(days=11))
            self._set_modified(boundary, self.NOW - timedelta(days=11))
            self._set_modified(recent, self.NOW - timedelta(days=11))

            deleted, errors = seg.cleanup_old_logs(root, now=self.NOW)

            self.assertFalse(old_log.exists())
            for kept in (boundary, recent, unrelated, matching_directory):
                self.assertTrue(kept.exists(), kept.name)
            self.assertEqual((old_log,), deleted)
            self.assertEqual((), errors)

    def test_default_steps_preserve_original_order_and_commands(self):
        steps = seg.default_steps(ROOT, python_executable="PYTHON")

        self.assertEqual(
            ["MMKT TV", "OTTO TV", "MMKT REF", "OTTO REF", "MMKT LDY", "OTTO LDY"],
            [step.name for step in steps],
        )
        self.assertEqual(("PYTHON", "run.py", "--product", "tv", "--concurrency", "1"), steps[0].command)
        self.assertEqual(("PYTHON", "tv\\run.py"), steps[1].command)
        self.assertEqual(("PYTHON", "run.py", "--product", "ref", "--concurrency", "1"), steps[2].command)
        self.assertEqual(("PYTHON", "ref\\run.py"), steps[3].command)
        self.assertEqual(("PYTHON", "run.py", "--product", "ldy", "--concurrency", "1"), steps[4].command)
        self.assertEqual(("PYTHON", "ldy\\run.py"), steps[5].command)

    def test_run_step_streams_output_and_returns_child_code(self):
        step = seg.Step(
            "TEST",
            ROOT,
            (sys.executable, "-c", "import sys; print('hello'); print('problem', file=sys.stderr); sys.exit(3)"),
        )
        log = io.StringIO()

        with patch("sys.stdout", new=io.StringIO()):
            return_code = seg.run_step(step, log)

        self.assertEqual(3, return_code)
        self.assertIn("hello", log.getvalue())
        self.assertIn("problem", log.getvalue())
        self.assertIn("status=FAILED exit_code=3", log.getvalue())

    def test_main_creates_log_and_removes_old_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "logs"
            log_dir.mkdir()
            old_log = log_dir / "seg_20200101_000000_000000.log"
            old_log.write_text("old", encoding="utf-8")
            self._set_modified(old_log, datetime(2020, 1, 1))
            step = seg.Step("TEST", root, (sys.executable, "-c", "print('collected')"))

            with patch.object(seg, "__file__", str(root / "run_seg_all.py")), \
                    patch.object(seg, "default_steps", return_value=(step,)), \
                    patch("sys.stdout", new=io.StringIO()):
                return_code = seg.main()

            self.assertEqual(0, return_code)
            self.assertFalse(old_log.exists())
            logs = list(log_dir.glob("seg_*.log"))
            self.assertEqual(1, len(logs))
            content = logs[0].read_text(encoding="utf-8-sig")
            self.assertIn("SEG RUN START", content)
            self.assertIn("[TEST] collected", content)
            self.assertIn("SEG RUN SUCCESS", content)

    def test_run_all_continues_after_failure_and_returns_failure(self):
        steps = (
            seg.Step("FIRST", ROOT, (sys.executable, "-c", "pass")),
            seg.Step("SECOND", ROOT, (sys.executable, "-c", "pass")),
        )
        log = io.StringIO()

        with patch.object(seg, "run_step", side_effect=(4, 0)) as mocked, \
                patch("sys.stdout", new=io.StringIO()):
            return_code = seg.run_all(steps, log)

        self.assertEqual(1, return_code)
        self.assertEqual(2, mocked.call_count)
        self.assertIn("SEG RUN FAILED: FIRST(4)", log.getvalue())

    def test_common_secret_forms_are_redacted(self):
        value = "apikey=test-value Authorization: Bearer test-bearer https://user:test-pass@example.com"
        redacted = seg.redact_sensitive(value)

        self.assertNotIn("test-value", redacted)
        self.assertNotIn("test-bearer", redacted)
        self.assertNotIn("test-pass", redacted)
        self.assertEqual(3, redacted.count("[REDACTED]"))

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_batch_returns_python_runner_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_python = Path(tmp) / "python.cmd"
            fake_python.write_text("@echo off\r\nexit /b 7\r\n", encoding="ascii")
            env = os.environ.copy()
            env["PATH"] = tmp

            result = subprocess.run(
                [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", str(ROOT / "run_seg_all.bat")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(7, result.returncode)


if __name__ == "__main__":
    unittest.main()
