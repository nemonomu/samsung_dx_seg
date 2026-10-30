"""All received listing candidates, measurement-free models and batch compatibility."""
from collections import Counter
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import test_dry as base
import test_dry_limits as limits
import test_dry_production as production
import test_dry_revision as revision
from dry import client, config, fields, history, pipeline, storage


def product(number, prefix="p", name=None):
    return dict(id=prefix+str(number), bestVariationId=prefix+"v"+str(number),
                variationPath=f"/p/dryer-{prefix}{number}/",
                name=name or f"Wärmepumpentrockner DR{number:04}")


def payload(ordinary, ads=(), count=None):
    return dict(intents=[dict(intent="ranked", products=ordinary, count=len(ordinary) if count is None else count),
                         dict(intent="sponsored", products=list(ads)),
                         dict(intent="context", products=[product(999, "context")])])


def run_state(limit=300):
    return dict(target=limit, batch_id="fixture", calendar_week="w44", crawl_datetime="2026-10-30 12:00:00")


class CandidatePageTests(base.Offline):
    def page(self, ordinary, ads=(), count=None):
        c = client.Client(base.Log())
        with patch.object(c, "fetch", return_value=(payload(ordinary, ads, count), {})) as fetch:
            result = c.page(1, 0)
        self.assertEqual(fetch.call_count, 1)
        return c, result

    def test_full_response_keeps_120_ordinary_and_11_ads_without_changing_offset_step(self):
        ordinary = [product(i) for i in range(120)]
        ads = [product(i, "ad") for i in range(11)]
        c, (rows, step, end, _) = self.page(ordinary, ads, 637)
        self.assertEqual((len(rows), step, end), (131, 109, False))
        self.assertEqual(c.last_page_meta["organic_occurrences"], 120)
        self.assertEqual(c.last_page_meta["sponsored_occurrences"], 11)
        self.assertEqual([r["product_id"] for r in rows if r["product_id"].startswith("p")],
                         [p["id"] for p in ordinary])
        self.assertEqual([i for i, r in enumerate(rows, 1) if r["product_id"].startswith("ad")],
                         list(client.listing.SPONSORED_SLOT_POSITIONS))
        self.assertFalse(any("context" in r["product_id"] for r in rows))
        self.assertTrue(all(set(r) == {"product_id", "variation_id", "product_url", "retailer_sku_name"} for r in rows))

    def test_variable_short_empty_and_ad_only_responses_preserve_occurrences_and_group_order(self):
        for size, ad_count in ((120, 0), (120, 1), (120, 14), (1, 11), (7, 11), (0, 13), (0, 0)):
            with self.subTest(size=size, ads=ad_count):
                ordinary = [product(i) for i in range(size)]
                ads = [product(i, "ad") for i in range(ad_count)]
                _, (rows, _, _, _) = self.page(ordinary, ads)
                self.assertEqual(Counter(r["variation_id"] for r in rows),
                                 Counter(p["bestVariationId"] for p in ordinary+ads))
                for prefix, expected in (("p", ordinary), ("ad", ads)):
                    self.assertEqual([r["product_id"] for r in rows if r["product_id"].startswith(prefix)],
                                     [p["id"] for p in expected])

    def test_short_page_appends_remaining_ads_without_empty_slots(self):
        _, (rows, _, _, _) = self.page([product(1)], [product(i, "ad") for i in range(5)])
        self.assertEqual([r["product_id"] for r in rows], ["ad0", "ad1", "p1", "ad2", "ad3", "ad4"])

    def test_repeated_option_occurrences_survive_page_composition_until_name_dedup(self):
        ordinary = [product(1), product(1)]
        ads = [product(1)]
        _, (rows, _, _, _) = self.page(ordinary, ads)
        self.assertEqual([r["variation_id"] for r in rows], ["pv1"]*3)

    def test_pipeline_positions_follow_actual_count_but_api_offsets_keep_existing_step(self):
        responses = [payload([product(i) for i in range(120)], [product(i, "ad") for i in range(11)], 114),
                     payload([product(i) for i in range(120, 125)], count=114)]
        c, log, state = client.Client(base.Log()), base.Log(), run_state()
        with patch.object(c, "fetch", side_effect=[(p, {}) for p in responses]) as fetch:
            rows = pipeline.collect_listing(c, log, state)
        self.assertEqual(len(rows), 136)
        self.assertEqual([r["listing_position"] for r in rows], list(range(1, 137)))
        offsets = [base.parse_qs(base.urlsplit(call.args[0]).query)["ranked.offset"][0] for call in fetch.call_args_list]
        self.assertEqual(offsets, ["0", "109"])
        self.assertEqual(rows[131]["listing_page"], 2)
        self.assertTrue(state["listing_complete"])
        self.assertEqual((state["listing"]["organic_occurrences"], state["listing"]["sponsored_occurrences"]), (125, 11))
        first = next(v for e, v in log.records if e == "page")
        self.assertEqual((first["received_organic"], first["received_sponsored"], first["candidates"]), (120, 11, 131))

    def test_request_failure_is_not_actual_search_end(self):
        c, state = client.Client(base.Log()), run_state()
        with patch.object(c, "fetch", side_effect=client.RequestFailure("listing_timeout")) as fetch:
            with self.assertRaisesRegex(client.RequestFailure, "listing_timeout"):
                pipeline.collect_listing(c, base.Log(), state)
        self.assertFalse(state["listing_complete"])
        self.assertEqual(fetch.call_count, 1)

    def test_malformed_ads_raise_instead_of_silently_disappearing(self):
        for ads in ([None], [dict(name="unknown")], "not_a_list"):
            c = client.Client(base.Log())
            data = payload([product(1)])
            data["intents"][1]["products"] = ads
            with self.subTest(ads=ads), patch.object(c, "fetch", return_value=(data, {})):
                with self.assertRaises(client.RequestFailure):
                    c.page(1, 0)

    def test_existing_card_extraction_keeps_prices_specs_and_pdf_without_unused_fields(self):
        raw = dict(brand="Brand", title=dict(shortened="Trockner DR900"),
                   price=dict(retailPrice="499,00", suggestedRetailPrice="599,00"), saleTags=dict(discount="-17%"),
                   topInfos=[dict(label="Nennkapazität", value="9 kg"), dict(label="Energy", value="unused")],
                   energyLabels=[dict(datasheetUri="https://d.otto.de/own.pdf")])
        result = client.minimal_tile(raw)
        self.assertEqual((result["final_sku_price"], result["original_sku_price"], result["savings"]),
                         ("499,00 €", "599,00 €", "17%"))
        self.assertEqual(result["top_infos"], {"Nennkapazität": "9 kg"})
        self.assertEqual(result["pdf_uri"], "https://d.otto.de/own.pdf")

    def test_actual_page_and_tiles_flow_to_selection_then_only_selected_details(self):
        ordinary = [product(i) for i in range(125)]
        ordinary[2]["name"] = "Filter für Trockner"
        ordinary[3]["name"] = ordinary[4]["name"]
        ads = [dict(p, id="ad"+p["id"], bestVariationId="ad"+p["bestVariationId"]) for p in ordinary[:11]]
        by_id = {p["bestVariationId"]: p for p in ordinary+ads}
        c, calls = client.Client(base.Log()), []
        def fetch(url, endpoint, **kwargs):
            calls.append((endpoint, url))
            if endpoint == "listing":
                offset = int(base.parse_qs(base.urlsplit(url).query)["ranked.offset"][0])
                self.assertIn(offset, (0, 109))
                return (payload(ordinary[:120], ads, 114) if offset == 0 else payload(ordinary[120:], count=114)), {}
            self.assertEqual(endpoint, "tiles")
            ids = base.parse_qs(base.urlsplit(url).query)["variationIds"][0].split(",")
            return [dict(variationId=v, title=dict(shortened=by_id[v]["name"]),
                         price=dict(retailPrice="499,00"), topInfos=[dict(label="Nennkapazität", value="9 kg")]) for v in ids], {}
        c.fetch = Mock(side_effect=fetch)
        c.comparison = Mock(side_effect=lambda ids: {v: {"Modellbezeichnung": "MODEL900", "Bauart": "Frontlader"} for v in ids})
        c.pdf = Mock(side_effect=AssertionError("UNNECESSARY_PDF"))
        c.rating = Mock(return_value=dict(average_rating="4.3", rating_count=123, rating_state="rated"))
        state = run_state()
        rows = pipeline.collect(c, base.Log(), state, lambda r: None)
        self.assertEqual((len(rows), state["end_reason"]), (123, "actual_search_end"))
        self.assertEqual([endpoint for endpoint, _ in calls[:2]], ["listing", "listing"])
        self.assertFalse(any(endpoint == "listing" for endpoint, _ in calls[2:]))
        self.assertTrue({"p119", "p124"}.issubset({r["item"] for r in rows}))
        self.assertEqual([r["main_rank"] for r in rows], list(range(1, 124)))
        self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101))+[None]*23)
        self.assertEqual(len({r["retailer_sku_name"] for r in rows}), 123)
        compared = [v for call in c.comparison.call_args_list for v in call.args[0]]
        self.assertEqual(set(compared), {base.parse_qs(base.urlsplit(r["product_url"]).query)["variationId"][0] for r in rows})
        self.assertEqual(c.rating.call_count, 123)
        c.pdf.assert_not_called()
        self.assertTrue(all(r["final_sku_price"] == "499,00 €" and r["capacity"] == "9 kg" for r in rows))


class DimensionSkuTests(base.Offline):
    def test_measurements_rejected_before_normalization_without_removing_just_units(self):
        for raw in ("50x80cm", "50X80CM", "50×80 cm", "50 cm x 80 cm", "50cmx80cm", "150mm", "150 mm",
                    "27 inch", "27 inches", "27 Zoll", '27"', "27″", "27''", "27 인치", "1,5 mm", "50x80,5cm"):
            with self.subTest(raw=raw):
                self.assertIsNone(fields.valid_sku(raw))
                self.assertEqual(fields.sku_rejection(raw), "sku_dimension")
        for raw in ("A", "AB", "ABC", "  ABC  "):
            self.assertIsNone(fields.valid_sku(raw))

    def test_normal_models_with_unit_letters_and_long_models_are_not_replaced(self):
        for raw in ("CM9000", "MM150", "DR900MM", "AB12", "TSA523WP 8kg Active", "TQ 1000 WP Nova Edition"):
            self.assertEqual(fields.valid_sku(raw), raw)

    def test_title_extraction_does_not_turn_dimensions_into_models(self):
        for title in ("Brand Badematte 50x80cm", "Brand Gitter 150mm", "Brand Badematte 50x80,5cm",
                      "Brand Gitter 150 mm", "Brand Badematte 1,5 mm"):
            with self.subTest(title=title):
                self.assertIsNone(fields.model_from_name(title))
        self.assertEqual(fields.model_from_name("Wäschetrockner DR900 150 mm"), "DR900")

    def test_source_priority_skips_dimensions_and_keeps_normal_value(self):
        target = base.target(1, "Wäschetrockner TITLE900")
        log = base.Log()
        self.assertEqual(fields.choose(target, {"Modellbezeichnung": "150mm"}, {"sku": "PDF900"}, log)["sku"], "PDF900")
        diag = next(v for e, v in log.records if e == "field" and v["field"] == "sku")
        self.assertEqual(diag["candidates"][0]["reason"], "sku_dimension")
        self.assertEqual(fields.choose(target, {"Modellbezeichnung": "50x80cm"}, {"sku": "150mm"})["sku"], "TITLE900")
        self.assertEqual(fields.choose(target, {"Modellbezeichnung": "GOOD900"}, {"sku": "PDF900"})["sku"], "GOOD900")

    def test_pdf_raw_dimension_cannot_be_hidden_by_a_previously_shortened_value(self):
        target = base.target(1, "Brand textile")
        result = fields.choose(target, {}, {"sku_raw": "50x80 cm", "sku": "50x80"})
        self.assertIsNone(result["sku"])

    def test_actual_pdf_candidate_path_skips_same_page_measurements_then_stops_on_model(self):
        log = base.Log()
        texts = ["Modellkennung: 50x80 cm\nModel identifier: 150mm\nModellbezeichnung: PDF900", "must not read"]
        pages = [Mock(), Mock()]
        for page, text in zip(pages, texts):
            page.extract_text.return_value = text
        document = MagicMock()
        document.__enter__.return_value.pages = pages
        with patch.dict("sys.modules", {"pdfplumber": types.SimpleNamespace(open=Mock(return_value=document))}):
            result = client.parse_pdf(b"%PDF", need_sku=True, need_capacity=False, progress=log.emit)
        self.assertEqual(result["sku"], "PDF900")
        pages[1].extract_text.assert_not_called()
        rejected = [v for e, v in log.records if e == "pdf_sku_candidate" and not v["selected"]]
        self.assertEqual([v["reason"] for v in rejected], ["sku_dimension", "sku_dimension"])

    def test_pdf_next_page_and_other_fields_are_preserved(self):
        result, pages = revision.PdfRevisionTests().pages([
            "Modellkennung: 1,5 mm\nRated capacity 9\nBauart: Frontlader", "Model identifier: MODEL900"],
            need_sku=True, need_capacity=True, need_loading=True)
        self.assertEqual((result["sku"], result["capacity"], result["loading_type"]), ("MODEL900", "9 kg", "Front loader"))
        self.assertEqual([p.extract_text.call_count for p in pages], [1, 1])

    @unittest.skipUnless(importlib.util.find_spec("pdfplumber"), "pdfplumber not installed; no package installation")
    def test_real_in_memory_pdf_skips_dimension_and_finds_model_on_next_page(self):
        log = base.Log()
        result = client.parse_pdf(revision.pdf_bytes(["Modellkennung: 50x80 cm", "Model identifier: PDF900", "later"]),
                                  need_sku=True, need_capacity=False, progress=log.emit)
        self.assertEqual(result["sku"], "PDF900")
        self.assertNotIn("_error", result)
        self.assertEqual([v["page"] for e, v in log.records if e == "pdf_page"], [1, 2])

    def test_null_model_keeps_product_csv_rank_and_db_null(self):
        source = base.Source([[base.target(1, "Brand Textile 50x80cm")]],
                             {"v1": {"Modellbezeichnung": "150mm", "_name": "Trockner 9 kg Frontlader"}})
        source.pdf = Mock(return_value={})
        db = base.FakeConnection()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: source,
                database=lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db),
                backfill=Mock(return_value={}), notifier=Mock(return_value=dict(status="sent", sent=True)))
            rows, _ = storage.read_output(state["csv"])
        self.assertEqual((code, len(rows), rows[0]["sku"], rows[0]["main_rank"], rows[0]["bsr_rank"]), (0, 1, "", "1", "1"))
        self.assertIsNone(db.records[0][config.COLUMNS[1:].index("sku")])
        source.pdf.assert_called_once_with(None, need_sku=True, need_capacity=False, need_loading=False)

    def test_history_skips_latest_measurement_and_uses_older_normal_model_in_one_query(self):
        row = production.current(batch_id="o_20261030_120000", crawl_datetime="2026-10-30 12:00:00")
        records = [production.past(id=2, sku="150mm", batch_id="o_dry_20261029_120000"),
                   production.past(id=1, sku="MODEL900", batch_id="o_dry_20261028_120000", crawl_datetime="2026-10-26 12:00:00")]
        connection, log = production.HistoryConnection(records), base.Log()
        result = history.backfill(config.DEFAULT_TABLE, log, [row], connector=lambda: connection)
        self.assertEqual(row["sku"], "MODEL900")
        self.assertEqual(result["rejected"]["invalid_sku"], 1)
        self.assertEqual(len([sql for sql, _ in connection.statements if sql.startswith('SELECT "id",')]), 1)
        rejected = next(v for e, v in log.records if e == "history_rejected")
        self.assertEqual(rejected["validation_reason"], "sku_dimension")

    def test_all_invalid_history_leaves_only_sku_null_and_keeps_current_fields(self):
        row = production.current(capacity="9 kg", loading_type="Front loader")
        connection = production.HistoryConnection([production.past(sku="50x80cm")])
        history.backfill(config.DEFAULT_TABLE, base.Log(), [row], connector=lambda: connection)
        self.assertIsNone(row["sku"])
        self.assertEqual((row["capacity"], row["loading_type"], row["main_rank"]), ("9 kg", "Front loader", 1))


class BatchCompatibilityTests(base.Offline):
    def test_new_batch_is_shared_by_rows_state_events_db_email_and_folder_collision_guard(self):
        now = datetime(2026, 10, 30, 12, 34, 56)
        db, notice = base.FakeConnection(), Mock(return_value=dict(status="sent", sent=True))
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()), patch.object(pipeline, "datetime") as clock:
            clock.now.return_value = now
            kwargs = dict(limit=1, output_root=root, client_factory=lambda e: base.Source([[base.target(1)]]),
                database=lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db),
                backfill=Mock(return_value={}), notifier=notice)
            code, state = pipeline.execute(**kwargs)
            rows, saved = storage.read_output(state["csv"])
            expected = "o_20261030_123456"
            self.assertEqual((code, state["batch_id"], saved["batch_id"], rows[0]["batch_id"]), (0, expected, expected, expected))
            self.assertEqual(Path(state["csv"]).parent.name, expected)
            self.assertEqual(db.records[0][config.COLUMNS[1:].index("batch_id")], expected)
            self.assertEqual(notice.call_args.args[0]["batch_id"], expected)
            records = [json.loads(line) for line in Path(state["log"]).read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(r["batch_id"] == expected for r in records))
            self.assertEqual({p.name for p in Path(state["csv"]).parent.iterdir()}, {"otto_dry.csv", "state.json", "run.log", "diagnostics.jsonl"})
            with self.assertRaises(FileExistsError):
                pipeline.execute(**kwargs)

    def test_old_prefix_100_and_300_csv_reload_preserves_batch_and_prevents_duplicate_insert(self):
        for count in (100, 300):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                directory = Path(root)
                rows, state = limits.saved_fixture(directory, count)
                old_batch = "o_dry_20261029_120000"
                for row in rows:
                    row["batch_id"] = old_batch
                state["batch_id"] = old_batch
                storage.checkpoint(directory, rows, state)
                csv_path = directory / "otto_dry.csv"
                original = csv_path.read_bytes()
                db = base.FakeConnection()
                forbidden = Mock(side_effect=AssertionError("NO_SITE_OR_HISTORY"))
                kwargs = dict(load_only=csv_path, client_factory=forbidden, backfill=forbidden,
                    database=lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db),
                    notifier=Mock(return_value=dict(status="sent", sent=True)))
                code, loaded = pipeline.execute(**kwargs)
                self.assertEqual((code, loaded["batch_id"], len(db.records)), (0, old_batch, count))
                code, reloaded = pipeline.execute(**kwargs)
                self.assertEqual((code, reloaded["database"]["duplicates"], len(db.records)), (0, count, count))
                self.assertEqual(csv_path.read_bytes(), original)
                forbidden.assert_not_called()
