"""Offline sold-product selection regressions; no live listing/DB required."""
import unittest

import test_dry as base
from dry import fields, pipeline


class SoldProductTests(base.Offline):
    def test_accessory_care_and_included_items_are_not_sold_parts(self):
        descriptions = (
            "Filter leicht zu reinigen", "leicht zu reinigendes Flusensieb",
            "Trocknerbälle inklusive", "Flusensieb einfach zu reinigen",
            "Trocknerbälle im Lieferumfang enthalten",
        )
        for appliance in ("Wäschetrockner DR900", "Wäschetrockner", "Trockner Classic"):
            for separator in (", ", "; ", " "):
                for description in descriptions:
                    name = appliance + separator + description
                    with self.subTest(name=name):
                        accepted, reason = fields.classify(name)
                        self.assertTrue(accepted)
                        self.assertTrue(reason.startswith("ambiguous"))
                        self.assertTrue(fields.classify(name)[0])
                        self.assertTrue(fields.classify("Wäschetrockner", selection_name=name)[0])
                        self.assertEqual(fields.classification_evidence(name)[0]["raw"], name)

    def test_cleanable_or_bundled_parts_still_are_sold_parts(self):
        for name in (
            "Filter für Trockner", "Ersatzfilter für Wäschetrockner",
            "Trocknerbälle für Wäschetrockner",
            "Wäschetrockner-Kondensator 48 x Trocknerbälle",
            "Filter für Trockner, leicht zu reinigen", "Flusensieb für Trockner, leicht zu reinigen",
            "Filter leicht zu reinigen für Wäschetrockner",
            "Ersatzfilter für Wäschetrockner, Trocknerbälle inklusive",
            "Trockner-Filter leicht zu reinigen", "Wäschetrockner-Flusensieb leicht zu reinigen",
            "Wäschetrockner Ersatzfilter leicht zu reinigen", "Wäschetrockner Ersatzteil inklusive",
            "Wäschetrockner-Kondensator, Trocknerbälle inklusive",
            "Wäschetrockner, Ersatzfilter für das Gerät",
            "Wäschetrockner, Filter", "Wäschetrockner, Filter für Waschmaschine",
        ):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])
        # Cleaning/delivery claims elsewhere must not rescue a sold filter.
        self.assertFalse(fields.classify("Trockner, Filter, Trommel leicht zu reinigen")[0])
        self.assertFalse(fields.classify("Trockner, Filter, Anleitung inklusive")[0])
        self.assertFalse(fields.classify("Waschtrockner, Filter leicht zu reinigen")[0])
        self.assertFalse(fields.classify("Portabler Wäschetrockner, Filter leicht zu reinigen")[0])
        self.assertFalse(fields.classify("Wäschetrockner + Waschmaschine Set, Filter leicht zu reinigen")[0])

    def test_mini_is_not_portability(self):
        for name in (
            "Mini-Trockner DR900", "Mini Wäschetrockner DR900",
            "Fyrove Elektrischer Wäschetrockner Mini Wäschetrockner 4 kg, 5 Programme, 800W/400W",
            "FELIXLEO Heizkörper-Wäschetrockner Mini Wäschetrockner 4 kg 5 Programme 800W 400W Freistehend Schwarz",
        ):
            with self.subTest(name=name):
                self.assertTrue(fields.classify(name)[0])
        self.assertFalse(fields.classify("Mini Waschtrockner DR900")[0])
        self.assertFalse(fields.classify("Mini Wäschetrockner Ersatzfilter")[0])

    def test_portable_compound_appliance_not_controls(self):
        for name in (
            "SSY Heizkörper-Wäschetrockner Tragbarer Wärmepumpen-Wäschetrockner",
            "Portabler Wärme-Pumpen-Wäschetrockner DR900 mit mobiler App",
            "Tragbarer Wärmepumpen-Wäschetrockner DR900 mit Fernbedienung",
            "Mini Portabler Wäschetrockner DR900", "Reisetrockner DR900", "Campingtrockner DR900",
        ):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])
        for suffix in ("mobile Fernsteuerung", "tragbare Fernbedienung", "mit mobiler App",
                       "portable App-Steuerung", "mit tragbarem Lüfter", "kompatibel mit mobilen Geräten"):
            self.assertTrue(fields.classify("Wärmepumpen-Wäschetrockner DR900, " + suffix)[0])

    def test_same_title_conflict_is_not_discarded(self):
        name = "FELIXLEO Elektrischer Wäscheständer Ablufttrockner 2 5 kg mit Smart Modus 800 W Kompakttrockner mit"
        for chars in ({}, {"Produkttyp": "Wäscheständer"}, {"Produkttyp": "Ablufttrockner"}):
            accepted, reason = fields.classify(name)
            self.assertTrue(accepted)
            self.assertTrue(reason.startswith("ambiguous"))
        evidence = fields.classification_evidence(name)
        self.assertEqual(evidence[0]["decision"], "ambiguous")
        self.assertEqual(evidence[0]["raw"], name)

    def test_sold_parts_and_unrelated_products_override_generic_type(self):
        for name in (
            "easyPART Heizstab wie Electrolux 1257533164 Heizregister Trockner",
            "easyPART Sensor wie Beko 2982600100 Feuchtigkeitssensor mit",
            "Sensor für Wäschetrockner", "Griff wie BLUPARTS 10102303",
            "Thermodetektor wie SAMSUNG DC90-10128G Temperaturfühler",
            "Xavax Stellfuß", "Xavax Vibrationsdämpfer Schwingungsdämpfer für Waschmaschine/Spülmaschine/Trockner",
            "MOPUEA Wäschespinne-Schutzhülle Schützhülle für Waschmaschine und Trockner mit Reißverschlus",
            "Trade-Shop Wäschetrockner-Kondensator universal für viele Abluft-Wäschetrockner",
            "relaxdays Wäschetrockner-Kondensator 48 x Trocknerbälle",
            "Basera Wäschetrockner-Kondensator Wäscheständer ausziehbar aus rostfreiem Edelstahl",
            "Apex Kompressor Schraubenkompressor 950/10/500 7,5kW 950L 10bar mit Trockner",
            "Fruit of the Loom Unterziehshirt Unterhemd (10er Pack) Weiß Rundhals",
            "Traumreiter Bastelfilz Füllwatte mit Extra Power I 95° waschbar + Trockner",
            "Ruco Standregal", "HOROW Dusch-WC Spülrandloses Hänge WC mit Bidet Funktion",
            "Sibel Haargel Sibel Quick UV Trockner mit Ventilator",
            "Heizstab wie Brand Wärmepumpentrockner DR900",
        ):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])
                self.assertFalse(fields.classify("Wäschetrockner", selection_name=name)[0])

    def test_non_clothing_purpose_survives_fuer(self):
        for name in (
            "Creality 3D 3D-Drucker Creality SpacePi X4 Filatment Trockner 4 Filamente",
            "Filament-Trockner DR900", "Trockner für Filamente",
            "Tourtecs Sitzbank Universeller Motorrad Ozon Sterilisator Trockner für Helm Stiefel und",
            "Ozon Trockner für Helm und Stiefel", "Trockner für den Helm",
            "XPOWER Hundeschermaschine Professioneller kommerzieller Hundepflege-Trockner für Haustiere",
            "Vacmaster Elektro-Gebläse Air Mover Gebläse mit 3 Geschwindigkeitsstufen, Teppich-Trockner",
            "Teppich-Trockner Air Mover",
        ):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])

    def test_features_accessories_and_compatibility_are_not_sold_parts(self):
        for suffix in (
            "mit Sensor", "mit Heizstab und Temperaturfühler", "mit Kompressor",
            "mit Lüfter, Pumpe und Schlauch", "mit Stellfuß", "inklusive Trocknerbälle",
            "mit Trockner für Helm und Stiefel", "mit Programm für Teppiche und Schuhe",
            "kompatibel mit Schutzhülle", "für Wäscheständer geeignet",
            "für Waschmaschine und Untergestell", "mit selbstreinigendem Kondensator",
            "ohne Ersatzfilter", "mit portable App-Steuerung",
        ):
            for dryer in ("Wärmepumpentrockner DR900", "Wäschetrockner DR900"):
                with self.subTest(dryer=dryer, suffix=suffix):
                    self.assertTrue(fields.classify(dryer + " " + suffix)[0])
        # Unintroduced feature nouns remain uncertain, not a reason to lose a dryer.
        for suffix in ("Sensor", "Kompressor", "Temperaturfühler", "Heizstab"):
            for dryer in ("Wärmepumpentrockner DR900", "Wäschetrockner DR900"):
                accepted, reason = fields.classify(dryer + ", " + suffix)
                self.assertTrue(accepted)
        self.assertEqual(fields.classify("Brand unbekannt"), (True, "ambiguous_included_product_type"))

    def test_title_conflicts_and_primary_title_precedence(self):
        accepted,reason=fields.classify("Wäschetrockner DR900, Heizstab")
        self.assertTrue(accepted)
        self.assertTrue(reason.startswith("ambiguous"))
        self.assertFalse(fields.classify("Wärmepumpentrockner DR900",selection_name="Heizstab wie Brand DR900")[0])
        self.assertFalse(fields.classify("Wäscheständer Wäschetrockner")[0])
        self.assertFalse(fields.classify("Lüfter für Wäscheständer")[0])


class SelectionFlowTests(base.Offline):
    def test_accessory_description_reaches_comparison_and_preserves_collection(self):
        titles = (
            "Wäschetrockner DR900, Filter leicht zu reinigen",
            "Wäschetrockner DR900, leicht zu reinigendes Flusensieb",
            "Wäschetrockner DR900, Trocknerbälle inklusive",
        )
        for comparison in ({"Produkttyp": "Wärmepumpentrockner"}, {}, {"_error": "comparison_timeout"}):
            with self.subTest(comparison=comparison):
                # Distinct URIs let this check prove no excluded product reaches PDF.
                items = [base.target(i + 1, name) for i, name in enumerate(titles)]
                early = base.target(900, "Filter für Trockner")
                late = base.target(901, "Unbekanntes Produkt")
                for item in items + [early, late]:
                    item["pdf_uri"] = "https://example.test/" + item["variation_id"] + ".pdf"
                source = base.Source([[early, *items, late],
                                      [base.target(4, titles[0])] + [base.target(i) for i in range(5, 110)]],
                                     {**{item["variation_id"]: comparison for item in items},
                                      "v901": {"_name": "Ersatzfilter für Wäschetrockner"}})
                state = dict(target=100, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-24 12:00:00")
                log = base.Log()
                rows = pipeline.collect(source, log, state, lambda rows: None)
                self.assertEqual([row["item"] for row in rows[:3]], ["1", "2", "3"])
                self.assertEqual(len(rows), 100)
                self.assertEqual([r["main_rank"] for r in rows], list(range(1, 101)))
                self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101)))
                self.assertEqual(state["counts"]["excluded"], 1)
                self.assertEqual(state["counts"]["duplicates"], 1)
                compared = [v for call in source.calls if call[0] == "comparison" for v in call[1]]
                self.assertNotIn("v900", compared)
                self.assertEqual(len(compared), len(set(compared)))
                for vid in ("v1", "v2", "v3", "v901"):
                    self.assertEqual(compared.count(vid), 1)
                self.assertEqual([c[1] for c in source.calls if c[0] == "rating"], [r["item"] for r in rows])
                self.assertEqual([c[1] for c in source.calls if c[0] == "pdf"], [item["pdf_uri"] for item in items + [late]])
                self.assertEqual({c[0] for c in source.calls}, {"page", "tiles", "comparison", "pdf", "rating"})
                for product_id in ("1", "2", "3"):
                    decision = next(v for e, v in log.records if e == "classification" and v.get("item") == product_id)
                    self.assertTrue(decision["needs_review"])
                    self.assertEqual(decision["evidence"][0]["source"], "search_title")
                    self.assertEqual(decision["evidence"][0]["raw"], titles[int(product_id)-1])

    def test_title_only_filtering_requests_ranks_and_refill(self):
        conflict = "Elektrischer Wäscheständer Ablufttrockner 2 5 kg mit Smart Modus"
        source = base.Source([
            [base.target(900, "Heizstab wie Brand 12345"), base.target(1, conflict),
             base.target(2, "Unbekanntes Produkt"), base.target(3, "Mini Wäschetrockner DR900"),
             base.target(4, conflict)],
            [base.target(i) for i in range(5, 110)],
        ], {"v1": {}, "v2": {"_name": "Kompressor mit Trockner"}, "v3": base.chars(3)})
        state = dict(target=100, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-24 12:00:00")
        log = base.Log()
        rows = pipeline.collect(source, log, state, lambda rows: None)
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["item"], "1")
        self.assertEqual(rows[1]["item"], "2")
        self.assertEqual([r["main_rank"] for r in rows], list(range(1, 101)))
        self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101)))
        self.assertEqual(state["counts"]["excluded"], 1)
        self.assertEqual(state["counts"]["duplicates"], 1)
        compared = [v for call in source.calls if call[0] == "comparison" for v in call[1]]
        self.assertNotIn("v900", compared)
        self.assertEqual(compared.count("v1"), 1)
        self.assertEqual(compared.count("v2"), 1)
        self.assertEqual(len(compared), len(set(compared)))
        rated = [call[1] for call in source.calls if call[0] == "rating"]
        self.assertIn("2", rated)  # Detailed type cannot exclude a selected title.
        self.assertNotIn("900", rated)
        self.assertEqual(len(rated), 100)
        # Both uncertain selected items need PDF; no excluded title does.
        self.assertEqual(sum(call[0] == "pdf" for call in source.calls), 2)
        decisions = [v for event, v in log.records if event == "classification" and v.get("item") == "1"]
        self.assertTrue(decisions[0]["needs_review"])
        self.assertEqual(decisions[0]["evidence"][0]["raw"], conflict)
        included = next(v for event, v in log.records if event == "classification" and v.get("item") == "3")
        self.assertTrue(included["accepted"])
        self.assertEqual(included["evidence"][0]["source"], "search_title")
        self.assertEqual(included["evidence"][0]["raw"], "Mini Wäschetrockner DR900")


if __name__ == "__main__":
    unittest.main()
