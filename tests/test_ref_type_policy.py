"""Offline output/DB boundary tests for the exact REF type exclusion list."""
from __future__ import annotations

import copy
import csv
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
RETAILERS = ("MMKT", "OTTO", "AMZN")
EXCLUDED = (
    "Built-in Refrigerator", "Built-in refrigerator", "Refrigerator", "Mini fridge",
    "Chest Freezer", "Fleischreifeschrank", "Getr\u00e4nkek\u00fchler",
    "Getr\u00e4nkek\u00fchlschrank", "K\u00fchlbox", "Party-K\u00fchlbox", "Generation 2",
)
RETAINED = (
    "Freezer-on-Top(Top Mount)", "Freezer-on-Bottom(Bottom Mount)",
    "Side-by-Side", "Side by Side", "French Door", "Multi-Door",
    "Fridge-freezer combination", "No freezer compartment",
    "Internal freezer compartment", "Single Door", "Unknown type",
    "Built-in Refrigerator with French Door", "Generation 20", "Mini fridge-freezer",
    "  fReNcH   Door\t", "French&#x20;Door", "", None,
)


def read_csv(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows, fields=None):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


@contextmanager
def retailer_modules(retailer, output_root):
    # Isolate each independent `common` package and stub all deployment settings.
    with patch.dict(sys.modules):
        for name in list(sys.modules):
            if name == "common" or name.startswith("common."):
                del sys.modules[name]
        package = ModuleType("common")
        package.__path__ = [str(ROOT / retailer / "common")]
        sys.modules["common"] = package
        settings = ModuleType("common.config")
        settings.ACCOUNT_NAME = "Retailer"
        settings.COUNTRY = "SEG"
        settings.PAGE_TYPE = "main"
        settings.AMAZON_BASE = "https://example.invalid"
        settings.run_meta = lambda prefix: {
            "batch_id": "test-batch", "calendar_week": "w38", "crawl_strdatetime": "2026-09-15 00:00:00"
        }
        settings.read_csv = read_csv
        settings.write_json = write_json
        settings.ensure_dirs = lambda *paths: None
        settings.db_config = Mock(side_effect=AssertionError("Real DB access forbidden"))
        sys.modules["common.config"] = settings
        util = ModuleType("common.io_util")
        util.ACCOUNT_NAME = "Retailer"
        util.COUNTRY = "SEG"
        util.RETAILER = "Retailer"
        util.AMZN_ROOT = ROOT / "AMZN"
        util.category_output_root = lambda product: output_root
        util.read_csv = read_csv
        util.write_csv = write_csv
        util.write_json = write_json
        util.split_table = lambda table: tuple(table.split("."))
        util.db_config = settings.db_config
        sys.modules["common.io_util"] = util
        for name in ("datasheet", "raw_html", "reco"):
            stub = ModuleType("common." + name)
            if name == "reco":
                stub.fetch_similar_product_names = lambda *args, **kwargs: {}
            sys.modules["common." + name] = stub
        with patch("sys.stdout", new=io.StringIO()):
            yield lambda name: importlib.import_module("common." + name)


def cfg_for(retailer, root):
    return SimpleNamespace(
        PRODUCT="REF", ACCOUNT_NAME="Retailer", COUNTRY="SEG", OUTPUT_ROOT=root,
        SPEC_FIELDS=["ref_refrigerator_type", "ref_capacity"],
        DB_TABLE=("test", "ref") if retailer == "MMKT" else "test.ref",
        USE_DATASHEET=False, PDP_SUPPLEMENT_FIELDS=["ref_refrigerator_type"],
    )


def row_for(value, index=1):
    return {
        "account_name": "Retailer", "product": "REF", "batch_id": "test-batch",
        "item": str(index), "retailer_sku_name": "Example Model", "sku": "MODEL123",
        "main_rank": str(index), "bsr_rank": str(index), "sku_status": "Sponsored",
        "ref_capacity": "300 L", "ref_refrigerator_type": value,
    }


class RefTypePolicyTests(unittest.TestCase):
    def test_exact_list_and_comparison_variants_only_clear_one_field(self):
        for retailer in RETAILERS:
            with retailer_modules(retailer, ROOT) as load:
                policy = load("ref_type_policy")
                for value in EXCLUDED:
                    variants = (value, value.swapcase(), " \t" + value + "\t\r\n",
                                value.replace(" ", "  \t ") + "&#x9;",
                                value.replace("\u00e4", "a\u0308").replace("\u00fc", "u\u0308"),
                                value.replace("\u00e4", "&auml;").replace("\u00fc", "&uuml;"))
                    for variant in variants:
                        with self.subTest(retailer=retailer, value=variant):
                            row = row_for(variant)
                            expected = dict(row, ref_refrigerator_type=None)
                            self.assertTrue(policy.apply_ref_type_policy(row))
                            self.assertEqual(expected, row)
                            self.assertFalse(policy.apply_ref_type_policy(row))

    def test_unlisted_values_and_missing_key_are_unchanged(self):
        for retailer in RETAILERS:
            with retailer_modules(retailer, ROOT) as load:
                policy = load("ref_type_policy")
                for value in RETAINED:
                    with self.subTest(retailer=retailer, value=value):
                        row = row_for(value)
                        before = copy.deepcopy(row)
                        self.assertFalse(policy.apply_ref_type_policy(row))
                        self.assertEqual(before, row)
                row = {"sku": "Refrigerator", "ref_capacity": "300 L"}
                before = dict(row)
                self.assertFalse(policy.apply_ref_type_policy(row))
                self.assertEqual(before, row)

    def test_history_skips_every_excluded_value_and_uses_older_valid_value(self):
        for retailer in RETAILERS:
            with retailer_modules(retailer, ROOT) as load:
                module = load("last_known_db")
                for retained in (None, "French Door", "Unlisted Historical Type"):
                    with self.subTest(retailer=retailer, retained=retained):
                        cursor = Mock()
                        history = [("1", "Example Model", value + "&#x9;", "300 L", "2026-09-14", "old")
                                   for value in EXCLUDED]
                        if retained:
                            history.append(("1", "Example Model", retained, "299 L", "2026-09-01", "older"))
                        cursor.fetchall.return_value = history
                        row = row_for(None)
                        row["ref_capacity"] = None
                        stats = module.backfill_from_retail_history(
                            cursor, schema="test", table="ref", rows=[row], account_names=("Retailer",),
                            product="REF", fields=("ref_refrigerator_type", "ref_capacity"),
                        )
                        self.assertEqual(retained, row["ref_refrigerator_type"])
                        self.assertEqual("300 L", row["ref_capacity"])
                        self.assertEqual(int(bool(retained)), stats["recovered_fields"].get("ref_refrigerator_type", 0))
                        self.assertEqual("Sponsored", row["sku_status"])

    def test_history_never_replaces_an_existing_unlisted_type(self):
        for retailer in RETAILERS:
            with retailer_modules(retailer, ROOT) as load:
                module = load("last_known_db")
                cursor = Mock()
                cursor.fetchall.return_value = [("1", "Example Model", "Refrigerator", "299 L", "old", "old")]
                row = row_for("  Side-by-Side\t")
                row["ref_capacity"] = None
                module.backfill_from_retail_history(
                    cursor, schema="test", table="ref", rows=[row], account_names=("Retailer",),
                    product="REF", fields=("ref_refrigerator_type", "ref_capacity"),
                )
                self.assertEqual("  Side-by-Side\t", row["ref_refrigerator_type"])
                self.assertEqual("299 L", row["ref_capacity"])

    def test_db_csv_entry_points_clear_values_without_dropping_rows(self):
        for retailer in RETAILERS:
            with tempfile.TemporaryDirectory() as directory, retailer_modules(retailer, Path(directory)) as load:
                root = Path(directory)
                module = load("db_save")
                cfg = cfg_for(retailer, root)
                values = (*EXCLUDED, "French Door", "Unknown type")
                rows = [row_for(value, i) for i, value in enumerate(values, 1)]
                original = copy.deepcopy(rows)
                path = root / f"{retailer.lower()}_full_output.csv"
                path.touch()
                with patch.object(module, "read_csv", return_value=rows):
                    if retailer == "MMKT":
                        args = SimpleNamespace(product="ref", input=str(path), dry_run=True)
                        with patch.object(module, "load_cfg", return_value=cfg), patch.object(module, "parse_args", return_value=args):
                            self.assertEqual(0, module.main())
                    else:
                        self.assertTrue(module.run(cfg, dry_run=True)["success"])
                self.assertEqual(len(values), len(rows))
                for i, row in enumerate(rows):
                    expected = dict(original[i])
                    if i < len(EXCLUDED):
                        expected["ref_refrigerator_type"] = None
                    self.assertEqual(expected, row)

    def test_mmkt_full_output_filters_reused_details_and_keeps_ranks(self):
        with tempfile.TemporaryDirectory() as directory, retailer_modules("MMKT", Path(directory)) as load:
            root = Path(directory)
            module = load("full_output")
            cfg = cfg_for("MMKT", root)
            values = (*EXCLUDED, "French Door", "Unknown type")
            listings = [dict(row_for(value, i), sku_id=str(i), rank=str(i), position=str(i))
                        for i, value in enumerate(values, 1)]
            for name in ("mmkt_listing_main.csv", "mmkt_listing_bsr.csv", "mmkt_pdp_detail.csv"):
                write_csv(root / name, listings)
            args = SimpleNamespace(product="ref", listing="", bsr="", detail="", output="")
            with patch.object(module, "parse_args", return_value=args), patch.object(module, "load_cfg", return_value=cfg):
                self.assertEqual(0, module.main())
            rows = read_csv(root / "mmkt_full_output.csv")
            self.assertEqual([""] * len(EXCLUDED) + ["French Door", "Unknown type"], [r["ref_refrigerator_type"] for r in rows])
            self.assertEqual([str(i) for i in range(1, len(values) + 1)], [r["main_rank"] for r in rows])
            self.assertTrue(all(r["ref_capacity"] == "300 L" and r["sku_status"] == "Sponsored" for r in rows))
            manifest = json.loads((root / "step09_full_output_manifest.json").read_text())
            self.assertEqual(0, manifest["rows_missing_primary_spec"])

    def test_otto_full_output_filters_specs_without_pdp_for_policy_nulls(self):
        with tempfile.TemporaryDirectory() as directory, retailer_modules("OTTO", Path(directory)) as load:
            root = Path(directory)
            module = load("full_output")
            cfg = cfg_for("OTTO", root)
            values = (*EXCLUDED, "French Door", "Unknown type")
            targets = [dict(row_for(value, i), product_id=str(i)) for i, value in enumerate(values, 1)]
            write_csv(root / "otto_final_targets.csv", targets)
            cfg.extract_spec = lambda target, *args, **kwargs: {field: target[field] for field in cfg.SPEC_FIELDS}
            with patch.object(module, "collect_review", return_value={}):
                manifest = module.run(cfg, pdp_supplement="zenrows", detail_sleep=0)
            rows = read_csv(root / "otto_full_output.csv")
            self.assertEqual([""] * len(EXCLUDED) + ["French Door", "Unknown type"], [r["ref_refrigerator_type"] for r in rows])
            self.assertEqual(len(values), manifest["output_rows"])
            for attempt in manifest["attempts"][:len(EXCLUDED)]:
                self.assertEqual("policy_null_only", attempt["pdp_supplement_skipped_reason"])
            self.assertTrue(all(r["ref_capacity"] == "300 L" and r["sku_status"] == "Sponsored" for r in rows))

    def test_amazon_translation_does_not_refill_an_excluded_value_from_title(self):
        with retailer_modules("AMZN", ROOT) as load:
            module = load("translations")
            for value in EXCLUDED:
                row = row_for(value)
                row["retailer_sku_name"] = "Brand French Door Refrigerator"
                module.translate_record_fields(row)
                self.assertIsNone(row["ref_refrigerator_type"])
                self.assertEqual("300 L", row["ref_capacity"])
            row = row_for("French Door")
            module.translate_record_fields(row)
            self.assertEqual("French Door", row["ref_refrigerator_type"])

    def test_amazon_csv_and_jsonl_full_output_exclude_listed_types(self):
        with tempfile.TemporaryDirectory() as directory, retailer_modules("AMZN", Path(directory)) as load:
            root = Path(directory)
            module = load("full_output")
            merger = load("merge_insert")
            cfg = cfg_for("AMZN", root)
            values = (*EXCLUDED, "French Door")
            targets = [dict(row_for(value, i), asin=str(i)) for i, value in enumerate(values, 1)]
            write_csv(root / "amzn_final_targets.csv", targets)
            write_csv(root / "amzn_detail.csv", targets)
            module.run(cfg)
            rows = read_csv(root / "amzn_full_output.csv")
            self.assertEqual([""] * len(EXCLUDED) + ["French Door"], [r["ref_refrigerator_type"] for r in rows])
            for i, target in enumerate(targets):
                row = merger.make_row(cfg, target, None, target)
                self.assertEqual(None if i < len(EXCLUDED) else "French Door", row["ref_refrigerator_type"])
                self.assertEqual(target["item"], row["item"])
                self.assertEqual(target["main_rank"], row["main_rank"])
                self.assertEqual("300 L", row["ref_capacity"])

    def test_amazon_batch_and_streaming_insert_boundaries(self):
        with tempfile.TemporaryDirectory() as directory, retailer_modules("AMZN", Path(directory)) as load:
            root = Path(directory)
            module = load("merge_insert")
            cfg = cfg_for("AMZN", root)
            rows = [row_for(value, i) for i, value in enumerate((*EXCLUDED, "Unknown type"), 1)]
            original = copy.deepcopy(rows)
            manifest = module.insert_rows(cfg, rows, dry_run=True)
            self.assertEqual(len(rows), manifest["rows_full"])
            self.assertEqual([""] * len(EXCLUDED) + ["Unknown type"],
                             [r["ref_refrigerator_type"] for r in read_csv(root / "amzn_full_output.csv")])
            streamer = module.StreamingRetailInserter(cfg, batch_id="test-batch", dry_run=True)
            for row, expected in zip(original, rows):
                streamer.insert_row(row)
                self.assertEqual(expected, row)
            self.assertEqual(len(rows), streamer.inserted)


if __name__ == "__main__":
    unittest.main()
