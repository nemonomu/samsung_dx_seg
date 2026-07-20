from __future__ import annotations

import unittest

from common.parsers import parse_product_detail_html
from common.selectors import extract_detail


def _pdp(title: str, *rows: tuple[str, str]) -> str:
    body = "".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
    return (
        "<html><body>"
        f"<span id='productTitle'>{title}</span>"
        f"<table class='prodDetTable'><tbody>{body}</tbody></table>"
        "</body></html>"
    )


class RefFieldSelectionTests(unittest.TestCase):
    def test_title_type_and_capacity_have_priority(self) -> None:
        parsed = parse_product_detail_html(
            _pdp(
                "Bosch Einbau-Kühlschrank, 199 L",
                ("Konfiguration", "204.0"),
                ("Aufbautyp", "Eingebaut"),
                ("Aufbau", "Zähler Tie"),
                ("Fassungsvermögen", "110,0 kg"),
            ),
            product="REF",
        )

        self.assertEqual(parsed["ref_refrigerator_type"], "Built-in Refrigerator")
        self.assertEqual(parsed["ref_capacity"], "199 L")

    def test_exact_aufbautyp_then_aufbau_fallback(self) -> None:
        built_in = parse_product_detail_html(
            _pdp("Bosch KIR41VFE0", ("Aufbautyp", "Eingebaut"), ("Aufbau", "Zähler Tie")),
            product="REF",
        )
        counter_depth = parse_product_detail_html(
            _pdp("Brand Model", ("Aufbautyp", "unbekannt"), ("Aufbau", "Zähler Tie")),
            product="REF",
        )

        self.assertEqual(built_in["ref_refrigerator_type"], "Built-in Refrigerator")
        self.assertEqual(counter_depth["ref_refrigerator_type"], "Counter Depth")

    def test_konfiguration_is_never_a_type_or_capacity_source(self) -> None:
        parsed = parse_product_detail_html(
            _pdp("Brand Model", ("Konfiguration", "304l")),
            product="REF",
        )

        self.assertIsNone(parsed["ref_refrigerator_type"])
        self.assertIsNone(parsed["ref_capacity"])

    def test_requested_excluded_product_categories_are_not_types(self) -> None:
        titles = (
            "Gefrierschrank",
            "Getränkekühlschrank",
            "Fleischreifeschrank",
            "Gewerbliche Mini-Kühlvitrine",
            "Kühlbox",
        )
        for title in titles:
            with self.subTest(title=title):
                parsed = parse_product_detail_html(
                    _pdp(title, ("Aufbautyp", "Eingebaut")),
                    product="REF",
                )
                self.assertIsNone(parsed["ref_refrigerator_type"])

    def test_structured_capacity_accepts_kg(self) -> None:
        parsed = parse_product_detail_html(
            _pdp("Brand Refrigerator", ("Fassungsvermögen", "110,0 kg")),
            product="REF",
        )

        self.assertEqual(parsed["ref_capacity"], "110,0 kg")

    def test_multiple_title_capacities_choose_refrigerator_capacity(self) -> None:
        parsed = parse_product_detail_html(
            _pdp(
                "Bosch Refrigerator, 270 L Gesamtvolumen, 199 L Kühlteil, 71 L Gefrierteil",
                ("Fassungsvermögen", "270 Liter"),
            ),
            product="REF",
        )

        self.assertEqual(parsed["ref_capacity"], "199 L")

    def test_db_konfiguration_selector_cannot_override_html_semantics(self) -> None:
        html = _pdp(
            "Bosch Einbau-Kühlschrank, 204 L",
            ("Konfiguration", "204.0"),
            ("Aufbautyp", "Eingebaut"),
            ("Fassungsvermögen", "204 Liter"),
        )

        class Element:
            def __init__(self, text: str) -> None:
                self.text = text

            @staticmethod
            def get_attribute(_name: str) -> None:
                return None

        class Driver:
            page_source = html

            @staticmethod
            def find_elements(_by: str, xpath: str) -> list[Element]:
                values = {"db-type": "204.0", "db-capacity": "999 Liter"}
                return [Element(values[xpath])] if xpath in values else []

        result = extract_detail(
            Driver(),
            {
                "ref_refrigerator_type": {"xpath": "db-type", "fallback": None},
                "ref_capacity": {"xpath": "db-capacity", "fallback": None},
            },
            product="REF",
        )

        self.assertEqual(result["ref_refrigerator_type"], "Built-in Refrigerator")
        self.assertEqual(result["ref_capacity"], "204 L")


if __name__ == "__main__":
    unittest.main()
