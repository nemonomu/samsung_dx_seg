from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import db_save  # noqa: E402
from common.io_util import read_csv, write_csv  # noqa: E402


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
    def test_starting_price_marker_is_not_inserted_into_database(self) -> None:
        row = dict(account_name="OTTO", product="TV", batch_id="test", item="1", is_starting_price="true",
                   star_rating="4", count_of_star_ratings="2", count_of_reviews="2")
        driver = MagicMock()
        cursor = driver.connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(field, "YES") for field in row]
        cursor.fetchone.return_value = None
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_csv(out / "otto_full_output.csv", [row])
            with patch.object(db_save, "category_output_root", return_value=out), \
                    patch.object(db_save, "db_config", return_value={"host": "unused.invalid"}), \
                    patch.object(db_save, "safe_backfill_from_retail_history", return_value={
                        "recovered_rows": 0, "recovered_fields": {}, "error": ""}), \
                    patch.dict(sys.modules, {"psycopg2": driver}):
                manifest = db_save.run(FakeTvConfig(), dry_run=False)
        self.assertNotIn("is_starting_price", manifest["inserted_columns"])
        self.assertNotIn('"is_starting_price"', cursor.executemany.call_args.args[0])

    def test_missing_table_reports_intentional_skip(self) -> None:
        driver = MagicMock()
        cursor = driver.connect.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            write_full_output(out / "otto_full_output.csv")
            with patch.object(db_save, "category_output_root", return_value=out), \
                    patch.object(db_save, "db_config", return_value={"host": "unused.invalid"}), \
                    patch.dict(sys.modules, {"psycopg2": driver}):
                manifest = db_save.run(FakeTvConfig(), dry_run=False)
        self.assertTrue(manifest["success"])
        self.assertTrue(manifest["dry_run"])
        self.assertTrue(manifest["skipped"])
        self.assertIn("not found", manifest["reason"])
        cursor.executemany.assert_not_called()

    def test_tv_hdr_and_other_missing_fields_use_history(self) -> None:
        power = "estimated_annual_electricity_use"
        rows = [
            {
                "account_name": "OTTO", "product": "TV", "batch_id": "current",
                "item": f"item{index}", "retailer_sku_name": f"TV {index}",
                "product_url": f"https://www.otto.de/p/tv/?variationId={index}",
                "sku": "", "screen_size": "", power: value,
                "star_rating": "", "count_of_star_ratings": "", "count_of_reviews": "",
            }
            for index, value in enumerate(("", "10 W", "28 W"), 1)
        ]
        old = dict(rows[0], sku="MODEL-55", screen_size="55", batch_id="older",
                   crawl_strdatetime="2026-11-09 12:00:00", **{power: "5.3 W"})
        driver = MagicMock()
        conn = driver.connect.return_value
        history_cursor = conn.cursor.return_value
        insert_cursor = history_cursor.__enter__.return_value
        insert_cursor.fetchall.return_value = [(column, "YES") for column in rows[0]]
        insert_cursor.fetchone.return_value = None

        def fetch_history():
            select = history_cursor.execute.call_args.args[0].split(" FROM ", 1)[0]
            return [tuple(old[column] for column in re.findall(r'"([^"]+)"', select))]

        history_cursor.fetchall.side_effect = fetch_history
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            path = out / "otto_full_output.csv"
            write_csv(path, rows)
            with patch.object(db_save, "category_output_root", return_value=out), \
                    patch.object(db_save, "db_config", return_value={"host": "unused.invalid"}), \
                    patch.dict(sys.modules, {"psycopg2": driver}):
                manifest = db_save.run(FakeTvConfig(), dry_run=False)
            saved_rows = read_csv(path)

        insert_cursor.executemany.assert_called_once()
        inserted = [dict(zip(manifest["inserted_columns"], values))
                    for values in insert_cursor.executemany.call_args.args[1]]
        self.assertEqual([row[power] for row in inserted], ["5.3 W", "10 W", "28 W"])
        self.assertEqual([row[power] for row in saved_rows], ["5.3 W", "10 W", "28 W"])
        self.assertEqual((inserted[0]["sku"], inserted[0]["screen_size"]), ("MODEL-55", "55"))
        self.assertEqual(manifest["last_known_db_backfill"]["recovered_fields"],
                         {"sku": 1, "screen_size": 1, power: 1})

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
