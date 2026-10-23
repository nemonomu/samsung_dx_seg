"""SKU boundaries only: all browser, API and database dependencies are fake."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_dry_pipeline import Browser, ratings, specs
from test_dry_updates import db_row, fake_db, schema
from dry import config, database
from dry.detail import DetailCollector, parse_specs
from dry.fields import title_sku, valid_sku
from dry.output import merge_rows, run_metadata, write_manifest, write_output
from dry.policy import exclusion_reason


CASES = (
    ("TWC640WP EcoSpeed&8kg", "TWC640WP"),
    ("TWB640WP Speed&8kg", "TWB640WP"),
    ("TEL785WP Steam&9kg", "TEL785WP"),
    ("TQ 1000 WP Nova Edition", "TQ 1000 WP"),
    ("TEL685WP 125 Gala Edition", "TEL685WP"),
)


class SkuDescriptionTests(unittest.TestCase):
    def parse(self, model, *, hint=None, title=None):
        return parse_specs(specs("1", name=title or "BRAND " + model + " Trockner",
                                 extra={"Modelkennung": hint}), "1")

    def test_five_descriptions_with_and_without_api_confirmation(self):
        for original, expected in CASES:
            for hint in (None, original):
                with self.subTest(original=original, hint=hint):
                    result = self.parse(original, hint=hint)
                    self.assertEqual(expected, result["sku"])
                    self.assertEqual("title", result["_sources"]["sku"]["source"])

    def test_spec_fallback_uses_the_same_description_boundary(self):
        for original, expected in CASES:
            with self.subTest(original=original):
                result = self.parse(original, hint=original, title="BRAND Trockner")
                self.assertEqual(expected, result["sku"])
                self.assertEqual("spec", result["_sources"]["sku"]["source"])
                self.assertEqual(original, result["_sources"]["sku"]["sku_before"])
                self.assertEqual(expected, result["_sources"]["sku"]["sku_after"])

    def test_additional_saved_gala_edition_uses_the_same_rule(self):
        result = self.parse("TWL680WP 125 Gala Edition", hint="TWL680WP 125 Gala Edition")
        self.assertEqual("TWL680WP", result["sku"])
        self.assertEqual("125 Gala Edition", result["_sources"]["sku"]["removed_description"])

    def test_wp_and_spaced_models_survive_without_an_api_hint(self):
        for model in ("TK100C WP", "TK300C WP", "TK740H WP", "TQ 1000 WP",
                      "WPT 7154", "CSOE H8A2DE-S", "HHPD-HMY80IVFC+++",
                      "DV90DG52A0AEEG", "NDE H8A2TCEXS-S"):
            with self.subTest(model=model):
                self.assertEqual(model, self.parse(model)["sku"])

    def test_model_punctuation_and_regional_suffix_survive_cleanup(self):
        for model in ("HHPD-HMY80IVFC+++", "HHPD-M8G3PDD++", "DV90DG52A0AEEG",
                      "CSOE H8A2DE-S", "TK100C WP", "WPT 7154"):
            for hint in (None, model + " Nova Edition"):
                with self.subTest(model=model, hint=hint):
                    self.assertEqual(model, self.parse(model + " Nova Edition", hint=hint)["sku"])

    def test_case_whitespace_and_decimal_capacity_variants(self):
        for original, expected in (("MODEL123 eCoSpEeD & 8 KG", "MODEL123"),
                                   ("MODEL123 ECO SPEED & 8,5 kg", "MODEL123"),
                                   ("MODEL123 steam & 3000 g", "MODEL123"),
                                   ("TQ 1000 WP\tNOVA   EDITION", "TQ 1000 WP"),
                                   ("MODEL123  125  gAlA  eDiTiOn", "MODEL123")):
            with self.subTest(original=original):
                self.assertEqual(expected, self.parse(original, hint=original)["sku"])

    def test_title_model_wins_over_a_different_api_model(self):
        result = self.parse("RIGHT123 Nova Edition", hint="WRONG456 Steam&9kg")
        self.assertEqual("RIGHT123", result["sku"])
        self.assertTrue(result["_sources"]["sku"]["conflict"])

    def test_clean_and_long_api_hints_cannot_reexpand_a_title_model(self):
        for hint in ("TWC640WP", "TWC640WP EcoSpeed&8kg"):
            with self.subTest(hint=hint):
                self.assertEqual("TWC640WP", self.parse("TWC640WP EcoSpeed&8kg", hint=hint)["sku"])
        self.assertEqual("TWC640WP", self.parse("TWC640WP", hint="TWC640WP EcoSpeed&8kg")["sku"])

    def test_internal_strings_and_unknown_editions_are_not_removed(self):
        for model in ("MODEL123NovaEdition", "MODEL123-EcoSpeed", "MODEL123-Nova Edition",
                      "MODEL123 Nova EditionX", "MODEL123 SPEEDPRO", "MODEL123 125",
                      "MODEL123 Special Edition", "TQ 1000 WP EU"):
            with self.subTest(model=model):
                self.assertEqual(model, self.parse(model, hint=model)["sku"])

    def test_description_before_a_model_does_not_cut_away_that_model(self):
        self.assertEqual("MODEL123", title_sku("BRAND Nova Edition MODEL123 Trockner"))
        self.assertEqual("MODEL123", title_sku("BRAND EcoSpeed&8kg MODEL123 Trockner"))

    def test_no_confirmed_boundary_is_preserved_and_explained(self):
        original = "12345678 Nova Edition"
        result = self.parse(original, hint=original, title="BRAND Trockner")
        self.assertEqual(original, result["sku"])
        self.assertEqual("model_boundary_unconfirmed", result["_sources"]["sku"]["cleanup_skipped"])

    def test_description_without_a_model_does_not_invent_one(self):
        for value in ("800W", "400W", "3-in-1", "800W/400W", "8kg", "10 Programme"):
            with self.subTest(value=value):
                self.assertIsNone(self.parse(value + " Nova Edition")["sku"])
                self.assertIsNone(valid_sku(value))

    def test_log_keeps_item_source_before_after_and_semantic_reason(self):
        original = "TWC640WP EcoSpeed&8kg"
        browser = Browser({})
        browser.gql = Mock(side_effect=[
            {"status": 200, "data": specs("1", name="BRAND " + original + " Trockner",
                                          extra={"Modelkennung": original})},
            {"status": 200, "data": ratings()}])
        with contextlib.redirect_stdout(io.StringIO()) as screen:
            collector = DetailCollector(browser, sleep_s=0)
            first = collector.get("1")
            self.assertEqual(first, collector.get("1"))
        events = [json.loads(line) for line in screen.getvalue().splitlines() if line.startswith("{")]
        event = next(e for e in events if e.get("event") == "field_selected" and e.get("field") == "sku")
        self.assertEqual(("1", "title", original, "TWC640WP"),
                         (event["item"], event["source"], event["sku_before"], event["sku_after"]))
        self.assertEqual("trailing_feature_capacity", event["cleanup_reason"])
        self.assertEqual("EcoSpeed&8kg", event["removed_description"])
        self.assertEqual(2, browser.gql.call_count)

    def test_non_sku_fields_and_filter_dryer_eligibility_are_unchanged(self):
        long = self.parse("TWC640WP EcoSpeed&8kg", hint="TWC640WP EcoSpeed&8kg")
        short = self.parse("TWC640WP", hint="TWC640WP")
        for field in ("capacity", "loading_type"):
            self.assertEqual(short[field], long[field])
        self.assertIsNone(exclusion_reason({"retailer_sku_name":
            "KITTECH 4 Kg Mit 5 Programme Bis Zu 60℃,800W/400W Filter für Wäschetrockner, G)"},
            features={"Produkttyp": "Wäschetrockner"}))


class SkuStorageCompatibilityTests(unittest.TestCase):
    def test_existing_validated_csv_reloads_without_normalizing_history(self):
        # New extraction rules must not silently rewrite a saved, validated batch.
        row = db_row(sku="TWC640WP EcoSpeed&8kg")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mmkt_dry_output.csv"
            write_output(path, [row])
            write_manifest(path.parent / "mmkt_dry_manifest.json", {
                "success": True, "output_rows": 1, "output_digest": database.file_digest(path),
                "batch_id": row["batch_id"]})
            loaded = database.load_validated_output(path)
        self.assertEqual(row["sku"], loaded[0]["sku"])
        self.assertEqual(row["sku"], valid_sku(row["sku"]))

    def test_new_sku_wins_and_null_history_backfill_is_not_normalized(self):
        rows = [db_row("1", sku="TWC640WP"), db_row("2")]
        original = copy.deepcopy(rows)
        connection, cursor = fake_db()
        cursor.fetchmany.side_effect = [[
            ("SEG", "Mediamarkt", "1", "TWC640WP EcoSpeed&8kg", None, None),
            ("SEG", "Mediamarkt", "2", "TWL680WP 125 Gala Edition", "Front load", "8kg")], []]
        with patch.object(database, "connect", return_value=connection), \
                patch.object(database, "table_metadata", return_value=schema()), \
                contextlib.redirect_stdout(io.StringIO()):
            database.inspect_or_insert(config.DB_TABLE, rows)
        inserted = [dict(zip(config.COLUMNS[1:], values)) for values in cursor.executemany.call_args.args[1]]
        self.assertEqual(["TWC640WP", "TWL680WP 125 Gala Edition"], [r["sku"] for r in inserted])
        self.assertEqual(original, rows)

    def test_sku_cleanup_does_not_change_identity_ranks_or_twenty_columns(self):
        metadata = run_metadata()
        main = [dict(sku_id=item, product_url=f"https://www.mediamarkt.de/de/product/_model-{item}.html")
                for item in ("1", "2")]
        bsr = list(reversed(main))
        details = {"1": {"sku": "TWC640WP"}, "2": {"sku": "TK100C WP"}}
        rows = merge_rows(main, bsr, details, metadata, log_defaults=False)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(tuple(row) == config.COLUMNS for row in rows))
        self.assertEqual([("1", 1, 2), ("2", 2, 1)],
                         [(r["item"], r["main_rank"], r["bsr_rank"]) for r in rows])


if __name__ == "__main__":
    unittest.main()
