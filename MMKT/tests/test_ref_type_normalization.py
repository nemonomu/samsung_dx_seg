from __future__ import annotations

import sys
import unittest
from pathlib import Path

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import PRIMARY_SPEC_EXPECTED_NULL
from ref import config as ref_config


class RefTypeNormalizationTests(unittest.TestCase):
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
            "Mini Kuehlschrank",
            "Vollraumkuehlschrank",
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
