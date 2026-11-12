from __future__ import annotations

import sys
import csv
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import PRIMARY_SPEC_EXPECTED_NULL
from common.last_known_db import backfill_from_retail_history
from common.ref_type_policy import normalize_ref_type
from common import db_save, full_output, pdp_detail
from ref import config as ref_config


class RefTypeNormalizationTests(unittest.TestCase):
    def test_gefrierbox_is_intentionally_empty_only_for_the_product_type(self):
        result = ref_config.extract_pdp_spec(
            {"Produkttyp": " Gefrierbox ", "Rauminhalt der K\u00fchlf\u00e4cher": "249"},
            "Brand French Door",
        )
        self.assertIsNone(result["ref_refrigerator_type"])
        self.assertTrue(result[PRIMARY_SPEC_EXPECTED_NULL])
        self.assertTrue(result["_ref_type_gefrierbox"])
        self.assertEqual("249L", result["ref_capacity"])
        self.assertEqual("French Door", self._type("French Door", "Kuehlschrank mit Gefrierbox French Door"))
        self.assertIsNone(normalize_ref_type("  GEFRIERBOX  "))

    def test_gefrierbox_does_not_restore_type_from_history(self):
        rows = [
            {"account_name": "MMKT", "product": "REF", "item": item,
             "retailer_sku_name": name, "ref_refrigerator_type": None, "ref_capacity": None}
            for item, name in (("1", "Box"), ("2", "Ordinary"))
        ]
        cursor = Mock()
        cursor.fetchall.return_value = [
            ("1", "Box", "French Door", "120L", "2026-01-01", "old"),
            ("2", "Ordinary", "French Door", "200L", "2026-01-01", "old"),
        ]
        backfill_from_retail_history(
            cursor, schema="test", table="ref", rows=rows, account_names=("MMKT",),
            product="REF", fields=("ref_refrigerator_type", "ref_capacity"),
            skip_ref_type_items={"1"},
        )
        self.assertIsNone(rows[0]["ref_refrigerator_type"])
        self.assertEqual("120L", rows[0]["ref_capacity"])
        self.assertEqual("French Door", rows[1]["ref_refrigerator_type"])

    def test_gefrierbox_marker_survives_detail_fallback_and_save_input(self):
        row = {"ref_refrigerator_type": "French Door", "ref_capacity": "249L"}
        parsed = {"ref_refrigerator_type": None, PRIMARY_SPEC_EXPECTED_NULL: True,
                  "_ref_type_gefrierbox": True}
        with patch.object(pdp_detail, "parse_pdp_html", return_value=parsed):
            pdp_detail.backfill_missing_pdp_fields(row, "", "1", ref_config)
        self.assertIsNone(row["ref_refrigerator_type"])
        self.assertTrue(row["_ref_type_gefrierbox"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mmkt_pdp_detail.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("sku_id", "_ref_type_gefrierbox"))
                writer.writeheader()
                writer.writerow({"sku_id": "1", "_ref_type_gefrierbox": row["_ref_type_gefrierbox"]})
            self.assertEqual({"1"}, db_save._gefrierbox_ids(SimpleNamespace(OUTPUT_ROOT=Path(directory))))

    def test_full_output_keeps_gefrierbox_empty_on_reused_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = [{"sku_id": "1", "rank": "1", "retailer_sku_name": "French Door"}]
            detail = [{"sku_id": "1", "ref_refrigerator_type": "French Door",
                       "ref_capacity": "249L", "_ref_type_gefrierbox": "True"}]
            def read(path):
                return detail if path.name == "mmkt_pdp_detail.csv" else listing
            (root / "mmkt_listing_main.csv").touch()
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="REF",
                                  SPEC_FIELDS=["ref_refrigerator_type", "ref_capacity"])
            args = SimpleNamespace(product="ref", listing="", bsr="", detail="", output="")
            with patch.object(full_output, "load_cfg", return_value=cfg), \
                 patch.object(full_output, "parse_args", return_value=args), \
                 patch.object(full_output, "read_csv", side_effect=read), \
                 patch.object(sys, "stdout", new=io.StringIO()):
                for original in (dict(detail[0]), {"sku_id": "1", "ref_refrigerator_type": "Gefrierbox", "ref_capacity": "249L"}):
                    detail[0] = original
                    self.assertEqual(0, full_output.main())
                    with (root / "mmkt_full_output.csv").open(encoding="utf-8-sig", newline="") as handle:
                        row = next(csv.DictReader(handle))
                    self.assertEqual("", row["ref_refrigerator_type"])
                    self.assertEqual("249L", row["ref_capacity"])
                    self.assertEqual("True", row["_ref_type_gefrierbox"])

    def test_save_passes_gefrierbox_to_history_skip_and_inserts_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mmkt_full_output.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("item", "main_rank", "ref_refrigerator_type", "ref_capacity", "_ref_type_gefrierbox"))
                writer.writeheader()
                writer.writerow({"item": "1", "main_rank": "1", "ref_refrigerator_type": "French Door", "_ref_type_gefrierbox": "True"})
            with (root / "mmkt_pdp_detail.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("sku_id", "_ref_type_gefrierbox", PRIMARY_SPEC_EXPECTED_NULL))
                writer.writeheader()
                writer.writerow({"sku_id": "1", "_ref_type_gefrierbox": "True", PRIMARY_SPEC_EXPECTED_NULL: "True"})
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="REF", DB_TABLE=("test", "ref"),
                                  SPEC_FIELDS=["ref_refrigerator_type", "ref_capacity"])
            args = SimpleNamespace(product="ref", input=str(source), dry_run=False)
            cursor = Mock()
            connection = Mock()
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=False)
            cursor.__enter__ = Mock(return_value=cursor)
            cursor.__exit__ = Mock(return_value=False)
            connection.cursor.return_value = cursor
            driver = SimpleNamespace(connect=Mock(return_value=connection))
            def history(_connection, **kwargs):
                self.assertEqual({"1"}, kwargs["skip_ref_type_items"])
                self.assertIsNone(kwargs["rows"][0]["ref_refrigerator_type"])
                kwargs["rows"][0]["ref_capacity"] = "249L"
                return {"recovered_rows": 1, "recovered_fields": {"ref_capacity": 1}, "error": None}
            with patch.object(db_save, "load_cfg", return_value=cfg), \
                 patch.object(db_save, "parse_args", return_value=args), \
                 patch.object(db_save, "db_config", return_value={"host": "unused"}), \
                 patch.object(db_save, "table_columns", return_value=["item", "ref_refrigerator_type", "ref_capacity"]), \
                 patch.object(db_save, "safe_backfill_from_retail_history", side_effect=history), \
                 patch.dict(sys.modules, {"psycopg2": driver}):
                self.assertEqual(0, db_save.main())
            self.assertEqual([("1", None, "249L")], cursor.executemany.call_args.args[1])

    def _type(self, raw_type: str | None, title: str | None = None) -> str | None:
        return ref_config.extract_pdp_spec(
            {"Produkttyp": raw_type},
            title,
        )["ref_refrigerator_type"]

    def test_fridge_freezer_punctuation_variants_share_one_value(self):
        variants = (
            "Kuehlgefrierkombination",
            "Kuehl-Gefrierkombination",
            "Kuehl-Gefrier-Kombination",
            "Kuehl- Gefrierkombination",
            "Kuehl-/Gefrierkombination",
            "Kuehl- und Gefrierkombination",
            "Kuehl- und Gefrierkombinationen",
            "Fridge-freezer combination",
        )
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual("Fridge-freezer combination", self._type(value))

    def test_specific_door_form_outranks_fridge_freezer_words(self):
        cases = {
            "French-Door": "French Door",
            "French Door Kuehlgefrierkombination": "French Door",
            "Side by Side Kuehlgefrierkombination": "Side-by-Side",
            "Side by Side Kuehl- und Gefrierkombination": "Side-by-Side",
            "Side-by-Side Kuehlkombination": "Side-by-Side",
            "Side-by-Side Kuehlkombinationen": "Side-by-Side",
            "Multi-Door": "Multi-Door",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, self._type(value))

    def test_product_category_and_installation_values_are_policy_null(self):
        values = (
            "compact", "Mini Refrigerator", "without water dispenser",
            "Mini Kuehlschrank",
            "Mini K\u00fchlschrank",
            "Stehender Vorratsschrank",
            "Kuehlschrank mit Gefrierfach",
            "Kuehlschrank mit Kaltlagerfach",
            "Einbaukuehlschrank",
            "Weinkuehlschrank",
            "Refrigerator",
            "Refrigerator with freezer compartment",
            "Refrigerator with chill compartment",
            "Built-in refrigerator",
            "Wine fridge",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(self._type(value))

    def test_full_space_translation_from_type_and_title(self):
        for raw in ("Vollraumk\u00fchlschrank", "Vollraumkuehlschrank", "Full-space Refrigerator"):
            for title in (None, "Brand " + raw + " Model"):
                with self.subTest(raw=raw, title=title):
                    result = ref_config.extract_pdp_spec(
                        {"Produkttyp": raw, "Rauminhalt der K\u00fchlf\u00e4cher": "249"}, title,
                    )
                    self.assertEqual("Full-space Refrigerator", result["ref_refrigerator_type"])
                    self.assertEqual("249L", result["ref_capacity"])
                    self.assertFalse(result.get(PRIMARY_SPEC_EXPECTED_NULL))

    def test_compact_layouts_keep_their_freezer_position(self):
        for raw, expected in (
            ("compact freezer-on-top", "Freezer-on-top"),
            ("compact internal freezer compartment", "Internal freezer compartment"),
            ("Freezer-on-top", "Freezer-on-top"),
            ("Internal freezer compartment", "Internal freezer compartment"),
        ):
            with self.subTest(raw=raw):
                result = ref_config.extract_pdp_spec({"Produkttyp": raw})
                self.assertEqual(expected, result["ref_refrigerator_type"])
                self.assertFalse(result.get(PRIMARY_SPEC_EXPECTED_NULL))

    def test_non_refrigerator_product_types_are_excluded(self):
        excluded = (
            "Gefrierschrank",
            "Gefriertruhe",
            "Freezer",
            "Chest freezer",
            "Getraenkekuehlschrank",
            "Getraenkekuehler",
            "Beverage cooler",
            "Fleischreifeschrank",
            "Meat aging cabinet",
            "Kuehlvitrine",
            "Display refrigerator",
            "Kuehlbox",
            "Party-Kuehlbox",
            "Cooler box",
        )
        for value in excluded:
            with self.subTest(value=value):
                self.assertIsNone(self._type(value))

    def test_excluded_title_overrides_generic_product_type(self):
        self.assertIsNone(
            self._type(
                "Refrigerator",
                "HENDI Aufsatz Kuehlvitrine 78 Liter Kuehlschrank",
            )
        )

    def test_branded_freezer_titles_override_generic_product_type(self):
        for title in (
            "ACME Freezer 300L",
            "ACME Chest Freezer 300L",
            "ACME Upright Freezer 300L",
        ):
            with self.subTest(title=title):
                self.assertIsNone(self._type("Refrigerator", title))

    def test_refrigerator_with_freezer_compartment_is_not_layout(self):
        self.assertIsNone(self._type("Refrigerator with freezer compartment"))
        self.assertIsNone(self._type("Kuehlschrank mit Gefrierfach"))

    def test_unknown_type_does_not_leak_raw_source_text(self):
        result = ref_config.extract_pdp_spec(
            {"Produkttyp": "Unbekannter Spezialschrank"}
        )
        self.assertIsNone(result["ref_refrigerator_type"])
        self.assertTrue(result[PRIMARY_SPEC_EXPECTED_NULL])

    def test_type_normalization_does_not_change_capacity_extraction(self):
        result = ref_config.extract_pdp_spec(
            {
                "Produkttyp": "Kuehl-Gefrier-Kombination",
                "Rauminhalt der K\u00fchlf\u00e4cher": "249",
            },
            "Beispiel Kuehl-Gefrier-Kombination",
        )
        self.assertEqual("Fridge-freezer combination", result["ref_refrigerator_type"])
        self.assertEqual("249L", result["ref_capacity"])


if __name__ == "__main__":
    unittest.main()
