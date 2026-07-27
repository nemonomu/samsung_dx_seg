from __future__ import annotations

import unittest

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
            "Kühlgefrierkombination",
            "Kühl-Gefrierkombination",
            "Kühl-Gefrier-Kombination",
            "Kühl- Gefrierkombination",
            "Kühl-/Gefrierkombination",
            "Kühl- und Gefrierkombination",
            "Kühl- und Gefrierkombinationen",
            "Fridge-freezer combination",
        )
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual("Fridge-freezer combination", self._type(value))

    def test_specific_door_form_outranks_fridge_freezer_words(self):
        cases = {
            "French-Door": "French Door",
            "French Door Kühlgefrierkombination": "French Door",
            "Side by Side Kühlgefrierkombination": "Side-by-Side",
            "Side by Side Kühl- und Gefrierkombination": "Side-by-Side",
            "Side-by-Side Kühlkombination": "Side-by-Side",
            "Side-by-Side Kühlkombinationen": "Side-by-Side",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, self._type(value))

    def test_refrigerator_variants_are_english_and_semantic(self):
        cases = {
            "Mini Kühlschrank": "Mini fridge",
            "Vollraumkühlschrank": "Refrigerator",
            "Stehender Vorratsschrank": "Refrigerator",
            "Kühlschrank mit Gefrierfach": "Refrigerator with freezer compartment",
            "Kühlschrank mit Kaltlagerfach": "Refrigerator with chill compartment",
            "Einbaukühlschrank": "Built-in refrigerator",
            "Weinkühlschrank": "Wine fridge",
            "Refrigerator": "Refrigerator",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, self._type(value))

    def test_non_refrigerator_product_types_are_excluded(self):
        excluded = (
            "Gefrierschrank",
            "Gefriertruhe",
            "Freezer",
            "Chest freezer",
            "Getränkekühlschrank",
            "Getränkekühler",
            "Beverage cooler",
            "Fleischreifeschrank",
            "Meat aging cabinet",
            "Kühlvitrine",
            "Display refrigerator",
            "Kühlbox",
            "Party-Kühlbox",
            "Cooler box",
        )
        for value in excluded:
            with self.subTest(value=value):
                self.assertIsNone(self._type(value))

    def test_excluded_title_overrides_generic_product_type(self):
        self.assertIsNone(
            self._type(
                "Refrigerator",
                "HENDI Aufsatz Kühlvitrine 78 Liter Kühlschrank",
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

    def test_valid_freezer_compounds_are_not_excluded(self):
        self.assertEqual(
            "Fridge-freezer combination",
            self._type("Fridge-freezer combination"),
        )
        self.assertEqual(
            "Refrigerator with freezer compartment",
            self._type("Refrigerator with freezer compartment"),
        )

    def test_unknown_type_does_not_leak_raw_source_text(self):
        result = ref_config.extract_pdp_spec(
            {"Produkttyp": "Unbekannter Spezialschrank"}
        )
        self.assertIsNone(result["ref_refrigerator_type"])
        self.assertTrue(result[PRIMARY_SPEC_EXPECTED_NULL])

    def test_type_normalization_does_not_change_capacity_extraction(self):
        result = ref_config.extract_pdp_spec(
            {
                "Produkttyp": "Kühl-Gefrier-Kombination",
                "Rauminhalt der Kühlfächer": "249",
            },
            "Beispiel Kühl-Gefrier-Kombination",
        )
        self.assertEqual("Fridge-freezer combination", result["ref_refrigerator_type"])
        self.assertEqual("249L", result["ref_capacity"])


if __name__ == "__main__":
    unittest.main()
