from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATHS = (
    ROOT / "AMZN" / "common" / "last_known_db.py",
    ROOT / "MMKT" / "common" / "last_known_db.py",
    ROOT / "OTTO" / "common" / "last_known_db.py",
)


def load_module(path: Path):
    name = f"test_{path.parents[1].name.lower()}_last_known_db"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, history=(), *, fail_select=False):
        self.history = list(history)
        self.fail_select = fail_select
        self.calls = []
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_select and sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("history unavailable")

    def fetchall(self):
        return list(self.history)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class LastKnownDbBackfillTests(unittest.TestCase):
    def test_all_retailer_modules_follow_exact_identity_and_fieldwise_latest(self):
        fields = (
            "sku",
            "screen_size",
            "model_year",
            "estimated_annual_electricity_use",
        )
        history = [
            (
                "ITEM-1",
                "  SAME   PRODUCT  ",
                "",
                "",
                "2025",
                None,
                "2026-09-03 12:00:00",
                "old-3",
            ),
            (
                "item-1",
                "same product",
                "SKU-65",
                "65 inches",
                "2024",
                "100 kWh",
                "2026-09-02 12:00:00",
                "old-2",
            ),
            (
                "item-1",
                "different product",
                "WRONG-SKU",
                "99 inches",
                "2099",
                "999 kWh",
                "2026-09-04 12:00:00",
                "wrong-name",
            ),
            (
                "other-item",
                "same product",
                "OTHER-SKU",
                "88 inches",
                "2088",
                "888 kWh",
                "2026-09-04 12:00:00",
                "wrong-item",
            ),
        ]
        for path in MODULE_PATHS:
            with self.subTest(module=str(path)):
                module = load_module(path)
                cursor = FakeCursor(history)
                row = {
                    "account_name": "Retailer",
                    "product": "TV",
                    "item": "item-1",
                    "retailer_sku_name": "Same Product",
                    "batch_id": "current",
                    "sku": "",
                    "screen_size": "55 inches",
                    "model_year": "",
                    "estimated_annual_electricity_use": "",
                }
                stats = module.backfill_from_retail_history(
                    cursor,
                    schema="dx_seg",
                    table="dx_seg_tv_retail_com",
                    rows=[row],
                    account_names=("Retailer",),
                    product="TV",
                    fields=fields,
                    excluded_batch_ids=("current",),
                )

                self.assertEqual(row["screen_size"], "55 inches")
                self.assertEqual(row["model_year"], "2025")
                self.assertEqual(row["sku"], "SKU-65")
                self.assertEqual(
                    row["estimated_annual_electricity_use"],
                    "100 kWh",
                )
                self.assertEqual(stats["recovered_rows"], 1)
                self.assertEqual(
                    stats["recovered_fields"],
                    {
                        "estimated_annual_electricity_use": 1,
                        "model_year": 1,
                        "sku": 1,
                    },
                )
                select_sql, params = cursor.calls[0]
                self.assertTrue(select_sql.lstrip().upper().startswith("SELECT"))
                self.assertNotRegex(
                    select_sql.upper(),
                    r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP)\b",
                )
                self.assertEqual(params[1], "TV")

    def test_missing_item_or_product_name_is_not_eligible(self):
        for path in MODULE_PATHS:
            with self.subTest(module=str(path)):
                module = load_module(path)
                cursor = FakeCursor()
                rows = [
                    {
                        "account_name": "Retailer",
                        "product": "TV",
                        "item": "",
                        "retailer_sku_name": "Name",
                        "sku": "",
                    },
                    {
                        "account_name": "Retailer",
                        "product": "TV",
                        "item": "item",
                        "retailer_sku_name": "",
                        "sku": "",
                    },
                ]
                stats = module.backfill_from_retail_history(
                    cursor,
                    schema="dx_seg",
                    table="table",
                    rows=rows,
                    account_names=("Retailer",),
                    product="TV",
                    fields=("sku",),
                )
                self.assertEqual(stats["eligible_rows"], 0)
                self.assertEqual(cursor.calls, [])

    def test_current_batch_is_excluded_before_older_values_are_used(self):
        for path in MODULE_PATHS:
            with self.subTest(module=str(path)):
                module = load_module(path)
                cursor = FakeCursor(
                    [
                        ("item", "Product", "CURRENT", "2026-09-04", "batch-now"),
                        ("item", "Product", "HISTORICAL", "2026-09-03", "batch-old"),
                    ]
                )
                row = {
                    "account_name": "Retailer",
                    "product": "TV",
                    "item": "item",
                    "retailer_sku_name": "Product",
                    "sku": "",
                }
                module.backfill_from_retail_history(
                    cursor,
                    schema="dx_seg",
                    table="table",
                    rows=[row],
                    account_names=("Retailer",),
                    product="TV",
                    fields=("sku",),
                    excluded_batch_ids=("batch-now",),
                )
                self.assertEqual(row["sku"], "HISTORICAL")

    def test_query_failure_rolls_back_savepoint_and_fails_open(self):
        for path in MODULE_PATHS:
            with self.subTest(module=str(path)):
                module = load_module(path)
                cursor = FakeCursor(fail_select=True)
                row = {
                    "account_name": "Retailer",
                    "product": "TV",
                    "item": "item",
                    "retailer_sku_name": "Product",
                    "sku": "",
                }
                stats = module.safe_backfill_from_retail_history(
                    FakeConnection(cursor),
                    schema="dx_seg",
                    table="table",
                    rows=[row],
                    account_names=("Retailer",),
                    product="TV",
                    fields=("sku",),
                )
                self.assertEqual(row["sku"], "")
                self.assertEqual(stats["error"], "RuntimeError")
                self.assertEqual(stats["eligible_rows"], 1)
                self.assertEqual(stats["queried_items"], 1)
                self.assertTrue(
                    any(
                        sql.startswith("ROLLBACK TO SAVEPOINT")
                        for sql, _ in cursor.calls
                    )
                )


if __name__ == "__main__":
    unittest.main()
