from __future__ import annotations

import argparse
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import pipeline


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
    def test_db_failure_still_runs_notify_and_returns_failure(self):
        cfg = FakeConfig()
        with patch.object(pipeline.db_save, "run", side_effect=RuntimeError("simulated failure")), \
                patch.object(pipeline.notify, "run") as notify_run, \
                patch("sys.stdout", new=io.StringIO()):
            result = pipeline.run(cfg, args_for("db", "notify"))

        self.assertEqual(1, result)
        notify_run.assert_called_once_with(cfg)

    def test_successful_db_and_notify_return_success(self):
        with patch.object(pipeline.db_save, "run"), \
                patch.object(pipeline.notify, "run"), \
                patch("sys.stdout", new=io.StringIO()):
            result = pipeline.run(FakeConfig(), args_for("db", "notify"))

        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
