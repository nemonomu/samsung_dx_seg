from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import db_save  # noqa: E402


class FakeTvConfig:
    PRODUCT = "TV"
    ACCOUNT_NAME = "OTTO"
    DB_TABLE = "dx_seg.dx_seg_tv_retail_com"
    SPEC_FIELDS = ["screen_size", "estimated_annual_electricity_use"]


def write_full_output(path: Path) -> None:
    rows = [
        {
            "account_name": "OTTO",
            "product": "TV",
            "batch_id": "test_batch",
            "main_rank": "1",
            "item": "item1",
            "screen_size": "55",
            "estimated_annual_electricity_use": "",
        }
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


class OttoDbSavePolicyTests(unittest.TestCase):
    def test_missing_specs_warn_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_full_output(out / "otto_full_output.csv")
            with patch.object(db_save, "category_output_root", return_value=out), \
                    patch.object(db_save, "db_config", return_value=None), \
                    patch.dict(os.environ, {"OTTO_DB_DRY_RUN": "0", "OTTO_ALLOW_NULL_SPEC_DB": "0"}, clear=False):
                os.environ.pop("OTTO_BLOCK_NULL_SPEC_DB", None)
                os.environ.pop("OTTO_ALLOW_NULL_SPEC_DB", None)
                with self.assertRaisesRegex(RuntimeError, "DB_CONFIG"):
                    db_save.run(FakeTvConfig())

    def test_missing_specs_are_not_blocked_by_legacy_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_full_output(out / "otto_full_output.csv")
            with patch.object(db_save, "category_output_root", return_value=out), \
                    patch.object(db_save, "db_config", return_value=None), \
                    patch.dict(os.environ, {
                        "OTTO_BLOCK_NULL_SPEC_DB": "1",
                        "OTTO_ALLOW_NULL_SPEC_DB": "0",
                        "OTTO_DB_DRY_RUN": "0",
                    }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "DB_CONFIG"):
                    db_save.run(FakeTvConfig())


if __name__ == "__main__":
    unittest.main()
