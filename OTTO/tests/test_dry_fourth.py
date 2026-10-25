"""Fourth-run regressions: real candidate paths, synthetic PDF text and fake DB only."""
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_dry as base
from dry import client, config, events, fields, pipeline, storage

FEATURE_TITLE = "MOPUEA Elektrischer Wäschetrockner Elektrischer Wäschetrockner 360-Grad-Zyklustrocknung MINI trockner"
CAPACITY_TITLE = "FELIXLEO Elektrischer Wäscheständer Ablufttrockner 2 5 kg mit Smart Modus 800 W Kompakttrockner mit"
RACK_TITLE = "Melchioni Elektrischer Wäschetrockner BAHAMAS Heiztrockner mit Flügeln 230W Leistung für schnelles Trocknen"
AIMAX_TITLE = "AIMAX Wandwäschetrockner Elektrischer Wäschetrockner 600 W Mini Trockner mit Timer, Tragbarer Klein Wärmepumpentrockner, für Apartment,Zuhause, Reisen"


def parse_pages(texts, **needed):
    pages = []
    for value in texts:
        page = MagicMock()
        if isinstance(value, Exception):
            page.extract_text.side_effect = value
        else:
            page.extract_text.return_value = value
        pages.append(page)
    document = MagicMock()
    document.__enter__.return_value.pages = pages
    log = base.Log()
    with patch.dict("sys.modules", {"pdfplumber": types.SimpleNamespace(open=MagicMock(return_value=document))}):
        result = client.parse_pdf(b"%PDF synthetic", progress=log.emit, **needed)
    return result, pages, log


class FourthFieldTests(base.Offline):
    def test_feature_sku_is_not_a_model_in_real_choose(self):
        target = base.target(1, FEATURE_TITLE)
        log = base.Log()
        self.assertIsNone(fields.choose(target, {}, events=log)["sku"])
        event = next(v for e, v in log.records if v["field"] == "sku")
        candidate = next(c for c in event["candidates"] if c["source"] == "listing_title")
        self.assertEqual(candidate["raw"], FEATURE_TITLE)
        self.assertEqual(candidate["reason"], "sku_feature_description")

    def test_feature_shapes_and_valid_models_are_separate(self):
        for feature in ("360-Grad-Zyklustrocknung", "180-Grad-Luftzirkulation", "3D-Luftstrom", "60-Minuten-Trocknung",
                        "800W-Heizleistung", "600W", "42dB", "5-Programme", "5Programme", "5 Programme"):
            with self.subTest(feature=feature):
                self.assertIsNone(fields.choose(base.target(1, "Wäschetrockner " + feature), {})["sku"])
                self.assertEqual(fields.choose(base.target(1, "Wäschetrockner DR900 " + feature), {})["sku"], "DR900")
        for title, expected in (("Wäschetrockner DR360", "DR360"), ("Wäschetrockner WQ360D00", "WQ360D00"),
                                ("Wäschetrockner BT 97A WB", "BT 97A WB"), ("Wäschetrockner WAM 914 A", "WAM 914 A")):
            self.assertEqual(fields.choose(base.target(1, title), {})["sku"], expected)
        for sku in ("TSA523WP 8kg Active", "MODEL360-X", "TR9DW80690 916 099 280"):
            self.assertEqual(fields.choose(base.target(1, FEATURE_TITLE), {"Modellbezeichnung": sku}, {"sku": "PDF900"})["sku"], sku)
            self.assertEqual(fields.choose(base.target(1, FEATURE_TITLE), {}, {"sku": sku})["sku"], sku)

    def test_ambiguous_title_capacity_falls_through_or_stays_null(self):
        target = base.target(1, CAPACITY_TITLE)
        log = base.Log()
        self.assertIsNone(fields.choose(target, {}, events=log)["capacity"])
        capacity_event = next(v for e, v in log.records if v["field"] == "capacity")
        self.assertEqual(capacity_event["candidates"][0]["reason"], "ambiguous_numeric_capacity")
        target["top_infos"] = {"Kapazität Trocknen": "3 kg"}
        self.assertEqual(fields.choose(target, {"_name": CAPACITY_TITLE}, {"capacity": "2.5 kg"})["capacity"], "3 kg")
        target["top_infos"] = {}
        self.assertEqual(fields.choose(target, {"Nennkapazität": "4 kg"}, {"capacity": "2.5 kg"})["capacity"], "4 kg")
        self.assertEqual(fields.choose(target, {}, {"capacity": "2.5 kg"})["capacity"], "2.5 kg")

    def test_normal_capacity_model_numbers_and_existing_priority(self):
        for raw, expected in (("2.5 kg", "2.5 kg"), ("2,5 kg", "2.5 kg"), ("9 kg", "9 kg"),
                              ("Wärmepumpentrockner Serie 6 9 kg", "9 kg"),
                              ("Trockner DR360 9 kg", "9 kg"), ("Trockner PT 840 9 kg", "9 kg"),
                              ("Trockner Modell 1200 9 kg", "9 kg"),
                              ("BOMANN Wärmepumpentrockner WPT 7155, 8kg Wärmepumpentrockner", "8 kg"),
                              ("BOMANN Wärmepumpentrockner WPT 7156, 10kg Wärmepumpentrockner", "10 kg"),
                              ("5 Programme, 9 kg", "9 kg")):
            with self.subTest(raw=raw):
                self.assertEqual(fields.capacity(raw), expected)
        for raw in ("2 5 kg", "2 5", "2, 5 kg", "Produktgewicht 50 kg", "Waschkapazität 8 kg", "8 kg / 9 kg"):
            with self.subTest(raw=raw):
                self.assertIsNone(fields.capacity(raw, labelled=True))
        target = base.target(1, "Trockner DR900, 8 kg")
        target["selection_name"] = "Trockner DR900, 7 kg"
        self.assertEqual(fields.choose(target, {"_name": "Trockner DR900, 9 kg"})["capacity"], "9 kg")


class FourthPdfTests(base.Offline):
    def test_pdf_ambiguous_capacity_does_not_truncate(self):
        result, _, log = parse_pages(["Rated capacity 2 5 kg"], need_sku=False, need_capacity=True)
        self.assertIsNone(result.get("capacity"))
        self.assertNotIn("capacity_raw", result)
        self.assertEqual(result["_missing"], ["capacity"])
        event = next(v for e, v in log.records if e == "pdf_capacity_candidate")
        self.assertIn("2 5", event["raw"])
        self.assertEqual(event["reason"], "ambiguous_numeric_capacity")

    def test_pdf_rejected_candidate_continues_same_line_page_and_next_page(self):
        for texts in (["Rated capacity 2 5 kg\nNennkapazität (kg) 2,5"],
                      ["Rated capacity 2 5 kg; Rated capacity 2.5 kg"],
                      ["Rated capacity 2 5 kg", "Rated capacity 2.5 kg"]):
            with self.subTest(texts=texts):
                result, pages, _ = parse_pages(list(texts) + ["must not be read"], need_sku=False, need_capacity=True)
                self.assertEqual(result["capacity"], "2.5 kg")
                pages[-1].extract_text.assert_not_called()

    def test_pdf_normal_labels_early_stop_and_confirmed_fields_survive(self):
        for text, expected in (("Rated capacity 9", "9 kg"), ("Nennkapazität (a) (kg): 2,5", "2.5 kg"),
                               ("Trockenkapazität: 2.5 kg", "2.5 kg")):
            result, pages, _ = parse_pages([text, "skip"], need_sku=False, need_capacity=True)
            self.assertEqual(result["capacity"], expected)
            pages[-1].extract_text.assert_not_called()
        result, pages, _ = parse_pages(["Modellkennung: PDF900\nBauart: Frontlader\nRated capacity 2 5 kg",
                                        "Rated capacity 9", "skip"],
                                       need_sku=True, need_capacity=True, need_loading=True)
        self.assertEqual((result["sku"], result["loading_type"], result["capacity"]), ("PDF900", "Front loader", "9 kg"))
        pages[-1].extract_text.assert_not_called()
        result, _, _ = parse_pages(["Modellkennung: PDF900\nRated capacity 2 5 kg", ValueError("PRIVATE_TEXT")],
                                   need_sku=True, need_capacity=True)
        self.assertEqual(result["sku"], "PDF900")
        self.assertIsNone(result.get("capacity"))
        self.assertEqual(result["_error"], "pdf_extract:ValueError")
        self.assertNotIn("PRIVATE_TEXT", str(result))

    def test_capacity_candidate_diagnostics_are_saved(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            log = events.Events(Path(root), "fixture")
            log.emit("pdf_capacity_candidate", page=1, raw="Rated capacity 2 5 kg", normalized=None,
                     reason="ambiguous_numeric_capacity")
            log.close()
            saved = json.loads((Path(root) / "diagnostics.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(saved["reason"], "ambiguous_numeric_capacity")


class FourthSelectionTests(base.Offline):
    def test_included_abbreviation_protects_appliances_but_not_sold_racks(self):
        for name in (
            "Wäschetrockner DR900 inkl. Heiztrockner mit Flügeln",
            "Wäschetrockner DR900 INKL. Heiztrockner mit Flügeln",
            "Wäschetrockner DR900 Heiztrockner mit Flügeln inkl.",
            "Wäschetrockner DR900 inkl. Filter",
            "Wäschetrockner DR900 Filter inkl.",
        ):
            for chars in ({}, {"Produkttyp": "Wäschetrockner"}):
                with self.subTest(name=name, chars=chars):
                    self.assertTrue(fields.classify(name)[0])
        for name in (RACK_TITLE, RACK_TITLE + " inkl. Handtuchhalter",
                     "Portabler Wäschetrockner inkl. Fernbedienung",
                     "Lüfter für Wäscheständer inkl. Netzteil",
                     "Ersatzfilter für Wäschetrockner inkl. Halter",
                     "Wäschetrockner-Kondensator inkl. Trocknerbälle"):
            with self.subTest(sold_type=name):
                self.assertFalse(fields.classify(name)[0])

    def test_included_abbreviation_reaches_comparison_and_collection(self):
        names = ("Wäschetrockner DR900 inkl. Heiztrockner mit Flügeln",
                 "Wäschetrockner DR901 Heiztrockner mit Flügeln inkl.",
                 "Wäschetrockner DR902 inkl. Filter", "Wäschetrockner DR903 Filter inkl.")
        source = base.Source([[base.target(1, RACK_TITLE)] + [base.target(i, name) for i, name in enumerate(names, 2)]])
        state = dict(target=4, batch_id="test", calendar_week="w43", crawl_datetime="2026-10-25 00:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda r: None)
        self.assertEqual([r["item"] for r in rows], ["2", "3", "4", "5"])
        self.assertEqual([r["main_rank"] for r in rows], [1, 2, 3, 4])
        self.assertEqual([r["bsr_rank"] for r in rows], [1, 2, 3, 4])
        self.assertEqual(state["counts"]["excluded"], 1)
        compared = [vid for kind, *args in source.calls if kind == "comparison" for vid in args[0]]
        self.assertEqual(compared, ["v2", "v3", "v4", "v5"])
        self.assertEqual([c[1] for c in source.calls if c[0] == "rating"], ["2", "3", "4", "5"])
        self.assertFalse(any(c[0] == "pdf" for c in source.calls))

    def test_heated_rack_long_title_not_rescued_by_card_name(self):
        for name in (RACK_TITLE,"Brand Heiztrockner mit Flügeln","Elektrischer Wäschetrockner Heiztrockner mit Fluegeln"):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])
                self.assertFalse(fields.classify("Wäschetrockner",selection_name=name)[0])
                self.assertFalse(fields.classify("Wärmepumpentrockner DR900",selection_name=name)[0])

    def test_appliance_features_compatibility_delivery_and_conflicts_stay_included(self):
        titles = (
            "Wärmepumpentrockner DR900 mit Heizfunktion und Lüfter",
            "Wärmepumpentrockner DR900 mit Heiztrockner mit Flügeln",
            "Wäschetrockner DR900, kompatibel mit Heiztrockner mit Flügeln",
            "Wäschetrockner DR900, Heiztrockner mit Flügeln inklusive",
            "Wäschetrockner DR900, Heiztrockner mit Flügeln im Lieferumfang enthalten",
            "Wärmepumpentrockner DR900 Heiztrockner mit Flügeln",
            "Wäschetrockner DR900 mit Flügeln", "Heiztrockner DR900",
            "Wäschetrockner DR900, Filter leicht zu reinigen", "Wäschetrockner DR900, Trocknerbälle inklusive",
            "Wärmepumpentrockner DR900, mobile Fernsteuerung", AIMAX_TITLE,
        )
        for name in titles:
            with self.subTest(name=name):
                self.assertTrue(fields.classify(name)[0])

    def test_title_only_selection_keeps_details_and_refills_duplicates(self):
        source=base.Source([[base.target(1,RACK_TITLE),base.target(2,"Unknown item"),base.target(3),
                             base.target(4,base.target(3)["retailer_sku_name"]),base.target(5)]],
                           {"v2":{"_name":RACK_TITLE}})
        state=dict(target=3,batch_id="test",calendar_week="w43",crawl_datetime="2026-10-25 00:00:00")
        log=base.Log()
        rows=pipeline.collect(source,log,state,lambda r:None)
        self.assertEqual([r["item"] for r in rows],["2","3","5"])
        self.assertEqual([r["main_rank"] for r in rows],[1,2,3])
        self.assertEqual([r["bsr_rank"] for r in rows],[1,2,3])
        self.assertEqual((state["counts"]["excluded"],state["counts"]["duplicates"]),(1,1))
        self.assertEqual(sum(c[0]=="pdf" for c in source.calls),1)
        self.assertEqual([c[1] for c in source.calls if c[0]=="rating"],["2","3","5"])
        compared=[v for c in source.calls if c[0]=="comparison" for v in c[1]]
        self.assertEqual(compared,["v2","v3","v5"])
        reasons=[v["reason"] for e,v in log.records if e=="classification" and not v["accepted"]]
        self.assertTrue(any("heated_drying_rack" in r for r in reasons))


class FourthStorageFlowTests(base.Offline):
    def test_null_fields_keep_row_csv_and_database_null(self):
        for name, field in ((FEATURE_TITLE, "sku"), (CAPACITY_TITLE, "capacity")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                source = base.Source([[base.target(1, name)]], {"v1": {"Beladung": "Frontlader"}})
                source.pdf = MagicMock(return_value={"_unavailable": "own_pdf_link_missing"})
                connection = base.FakeConnection()
                database = lambda t, e, rows=None: storage.database(t, e, rows, connector=lambda: connection)
                code, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: source, database=database)
                self.assertEqual(code, 0)
                rows = storage.read_output(state["csv"])[0]
                self.assertEqual((len(rows), rows[0]["main_rank"], rows[0]["bsr_rank"]), (1, "1", "1"))
                self.assertEqual(rows[0][field], "")
                self.assertIsNone(connection.records[0][config.COLUMNS[1:].index(field)])

    def test_only_missing_fields_use_existing_pdf_and_success_avoids_request(self):
        source = base.Source([[base.target(1, CAPACITY_TITLE), base.target(2)]],
                             {"v1": {"Modellbezeichnung": "GOOD900", "Beladung": "Frontlader"}})
        original_tiles = source.tiles
        def tiles(ids):
            values = original_tiles(ids)
            for value in values.values():
                value["top_infos"] = {"Nennkapazität": "9 kg"}
            return values
        source.tiles = tiles
        state = dict(target=2, batch_id="test", calendar_week="w43", crawl_datetime="2026-10-25 00:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda r: None)
        self.assertEqual(rows[0]["capacity"], "9 kg")
        self.assertFalse(any(c[0] == "pdf" for c in source.calls))
        source = base.Source([[base.target(1, CAPACITY_TITLE)]],
                             {"v1": {"Modellbezeichnung": "GOOD900", "Beladung": "Frontlader"}})
        result, _, _ = parse_pages(["Rated capacity 2 5 kg\nRated capacity 2.5 kg"], need_sku=False, need_capacity=True)
        source.pdf = MagicMock(return_value=result)
        state["target"] = 1
        rows = pipeline.collect(source, base.Log(), state, lambda r: None)
        self.assertEqual(rows[0]["capacity"], "2.5 kg")
        source.pdf.assert_called_once_with(None, need_sku=False, need_capacity=True, need_loading=False)


if __name__ == "__main__":
    unittest.main()
