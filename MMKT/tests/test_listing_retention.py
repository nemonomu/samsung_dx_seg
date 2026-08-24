from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.listing_retention import cleanup_listing_archives


class ListingRetentionTests(unittest.TestCase):
    NOW = datetime(2026, 8, 18, 12, 0, 0)

    @staticmethod
    def _set_modified(path: Path, when: datetime) -> None:
        timestamp = when.timestamp()
        os.utime(path, (timestamp, timestamp))

    def test_only_old_exact_archive_directories_are_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_main = root / "main_20260816_105959"
            old_bsr = root / "bsr_20260815_120000"
            exact_boundary = root / "main_20260816_120000"
            recent = root / "bsr_20260818_110000"
            unrelated = root / "schema"
            suffix = root / "main_20260815_120000_backup"
            invalid = root / "main_not_a_timestamp"
            matching_file = root / "main_20260815_110000"
            nested_match = root / "other" / "main_20260815_100000"

            for directory in (old_main, old_bsr, exact_boundary, recent, unrelated, suffix, invalid):
                directory.mkdir()
            nested_match.mkdir(parents=True)
            (old_main / "page_01.html").write_text("raw", encoding="utf-8")
            matching_file.write_text("keep", encoding="utf-8")
            for directory in (old_main, old_bsr):
                self._set_modified(directory, self.NOW - timedelta(hours=49))

            result = cleanup_listing_archives(root, now=self.NOW)

            self.assertFalse(old_main.exists())
            self.assertFalse(old_bsr.exists())
            for kept in (exact_boundary, recent, unrelated, suffix, invalid, matching_file, nested_match):
                self.assertTrue(kept.exists(), kept.name)
            self.assertEqual({old_main, old_bsr}, set(result.deleted))
            self.assertEqual((), result.errors)

    def test_old_named_directory_modified_recently_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "main_20260815_120000"
            active.mkdir()
            self._set_modified(active, self.NOW - timedelta(hours=1))

            result = cleanup_listing_archives(root, now=self.NOW)

            self.assertTrue(active.exists())
            self.assertEqual((active,), result.skipped_recently_modified)
            self.assertEqual((), result.deleted)

    def test_missing_listing_root_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "listing"

            result = cleanup_listing_archives(missing, now=self.NOW)

            self.assertEqual((), result.deleted)
            self.assertEqual((), result.skipped_recently_modified)
            self.assertEqual((), result.errors)


if __name__ == "__main__":
    unittest.main()
