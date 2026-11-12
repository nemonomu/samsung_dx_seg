from __future__ import annotations

import argparse
import io
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import pipeline
from common.io_util import write_csv


class FakeConfig:
    PRODUCT = "REF"


def args_for(*steps: str) -> argparse.Namespace:
    return argparse.Namespace(
        only=",".join(steps),
        limit=0,
        start=1,
        pdp_supplement="none",
        detail_sleep=0.0,
        save_html=False,
        db_dry_run=False,
    )


class OttoPipelineFailureStatusTests(unittest.TestCase):
    def test_missing_table_skip_still_notifies(self):
        cfg = FakeConfig()
        cfg.DB_TABLE = "dx_seg.example"
        cfg.ACCOUNT_NAME = "OTTO"
        driver = MagicMock()
        cursor = driver.connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_csv(out / "otto_full_output.csv", [{"batch_id": "test", "account_name": "OTTO", "product": "REF"}])
            with patch.object(pipeline.db_save, "category_output_root", return_value=out), \
                    patch.object(pipeline.db_save, "db_config", return_value={"host": "unused.invalid"}), \
                    patch.dict(sys.modules, {"psycopg2": driver}), \
                    patch.object(pipeline.notify, "run") as notify, \
                    patch("sys.stdout", new=io.StringIO()):
                result = pipeline.run(cfg, args_for("db", "notify"))
        self.assertEqual(result, 0)
        cursor.executemany.assert_not_called()
        notify.assert_called_once_with(cfg)

    def test_db_failure_still_runs_notify_and_returns_failure(self):
        cfg = FakeConfig()
        with patch.object(pipeline.db_save, "run", side_effect=RuntimeError("simulated failure")), \
                patch.object(pipeline.notify, "run") as notify_run, \
                patch("sys.stdout", new=io.StringIO()):
            result = pipeline.run(cfg, args_for("db", "notify"))

        self.assertEqual(1, result)
        notify_run.assert_called_once_with(cfg)

    def test_successful_db_and_notify_return_success(self):
        with patch.object(pipeline.db_save, "run", return_value={"success": True}), \
                patch.object(pipeline.notify, "run"), \
                patch("sys.stdout", new=io.StringIO()):
            result = pipeline.run(FakeConfig(), args_for("db", "notify"))

        self.assertEqual(0, result)

    def test_empty_input_propagates_failure_and_still_notifies(self):
        cfg = FakeConfig()
        cfg.DB_TABLE = "dx_seg.example"
        cfg.ACCOUNT_NAME = "OTTO"
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(pipeline.db_save, "category_output_root", return_value=Path(tmpdir)), \
                patch.object(pipeline.notify, "run") as notify_run, \
                patch("sys.stdout", new=io.StringIO()):
            result = pipeline.run(cfg, args_for("db", "notify"))
        self.assertEqual(1, result)
        notify_run.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()
