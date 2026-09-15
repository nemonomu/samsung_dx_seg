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
    def test_detail_failure_preserves_existing_database_backfill_flow(self):
        argv = ["run.py", "--product", "ldy", "--steps", "detail,full,db,notify"]
        with patch.object(sys, "argv", argv), \
                patch.object(mmkt_run, "run_step", side_effect=(1, 0, 0, 0)) as mocked, \
                patch("sys.stdout", new=io.StringIO()):
            result = mmkt_run.main()
        self.assertEqual(1, result)
        self.assertEqual(["common.pdp_detail", "common.full_output", "common.db_save", "common.notify"],
                         [call.args[0] for call in mocked.call_args_list])

    def test_failed_listing_skips_database_insert_but_still_reports(self):
        argv = ["run.py", "--product", "ldy", "--steps", "listing,db,notify"]
        with patch.object(sys, "argv", argv), \
                patch.object(mmkt_run, "run_step", side_effect=(1, 0)) as mocked, \
                patch("sys.stdout", new=io.StringIO()):
            result = mmkt_run.main()
        self.assertEqual(1, result)
        self.assertEqual(["common.listing", "common.notify"], [call.args[0] for call in mocked.call_args_list])

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
