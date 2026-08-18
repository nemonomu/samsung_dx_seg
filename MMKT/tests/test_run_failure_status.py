from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

import run as mmkt_run


class MmktRunFailureStatusTests(unittest.TestCase):
    def test_failure_is_returned_after_remaining_selected_steps_run(self):
        argv = ["run.py", "--product", "tv", "--steps", "listing,notify"]
        with patch.object(sys, "argv", argv), \
                patch.object(mmkt_run, "run_step", side_effect=(5, 0)) as mocked, \
                patch("sys.stdout", new=io.StringIO()):
            result = mmkt_run.main()

        self.assertEqual(1, result)
        self.assertEqual(2, mocked.call_count)

    def test_successful_selected_steps_return_success(self):
        argv = ["run.py", "--product", "ref", "--steps", "listing"]
        with patch.object(sys, "argv", argv), \
                patch.object(mmkt_run, "run_step", return_value=0), \
                patch("sys.stdout", new=io.StringIO()):
            result = mmkt_run.main()

        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
