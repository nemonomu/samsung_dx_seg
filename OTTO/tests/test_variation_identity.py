from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import browser, compare, datasheet, full_output, listing, model_sku, parsers
from ldy import config as ldy
from ref import config as ref
from tv import config as tv


def washer(vid, model, rank=1, pid="2048348958"):
    return {"product_id": pid, "variation_id": vid,
            "retailer_sku_name": f"Samsung Waschmaschine WW8400D {model}",
            "main_rank": str(rank), "product_url": f"https://www.otto.de/p/washer-{pid}/",
            "energy_datasheet_uri": f"https://d.otto.de/files/{vid}.pdf",
            "top_infos": json.dumps({"Kapazität Waschen": "11 kg"})}


class VariationIdentityTests(unittest.TestCase):
    def test_ldy_capacity_priority_matches_pdf_fetch_decision(self):
        cases = (
            ("Waschmaschine 9 kg", "Waschmaschine 10 kg", "11 kg", "8 kg", "9 kg", False),
            ("Waschmaschine", "Waschmaschine 10 kg", "11 kg", "8 kg", "10 kg", False),
            ("Waschmaschine", "Waschmaschine", "11 kg", "8 kg", "11 kg", False),
            ("Waschmaschine", "Waschmaschine", None, "8 kg", "8 kg", False),
            ("Waschtrockner", "Waschtrockner", None, "6 kg", "9 kg", True),
        )
        for name, listing_name, top, comparison, expected, needs_pdf in cases:
            with self.subTest(name=name, listing=listing_name, top=top):
                target = washer("v1", "MODEL1234")
                target["retailer_sku_name"] = listing_name
                target["top_infos"] = json.dumps({"Kapazität Waschen": top})
                chars = {"v1": {"_name": name, "Modellbezeichnung": "MODEL1234",
                                "Fassungsvermögen": comparison}}
                with patch.object(compare, "characteristics_map", return_value=chars), \
                        patch.object(ldy, "_category_vids", return_value=set()), \
                        patch.object(datasheet, "fetch_datasheet_bytes", return_value=(b"pdf", 200, None)) as fetch, \
                        patch.object(datasheet, "parse", return_value={"text": "Nennkapazität 9"}):
                    ctx = ldy.prepare_context([target])
                self.assertEqual(fetch.call_count, int(needs_pdf))
                self.assertEqual(ldy.extract_spec(target, {}, ctx)["ldy_capacity"], expected)

    def test_ldy_washer_dryer_capacity_uses_wash_label_or_stays_null(self):
        for extra, expected, pdf_calls in (
            ({"Nennkapazität (Waschen)": "10 kg", "Fassungsvermögen": "6 kg"}, "10 kg", 0),
            ({"Fassungsvermögen": "6 kg"}, None, 1),
        ):
            with self.subTest(extra=extra):
                target = washer("v1", "MODEL1234")
                target.update(retailer_sku_name="Waschtrockner", top_infos="{}")
                chars = {"v1": {"_name": "Waschtrockner", "Modellbezeichnung": "MODEL1234", **extra}}
                with patch.object(compare, "characteristics_map", return_value=chars), \
                        patch.object(ldy, "_category_vids", return_value=set()), \
                        patch.object(datasheet, "fetch_datasheet_bytes", return_value=(b"pdf", 200, None)) as fetch, \
                        patch.object(datasheet, "parse", return_value={}):
                    ctx = ldy.prepare_context([target])
                self.assertEqual(fetch.call_count, pdf_calls)
                self.assertEqual(ldy.extract_spec(target, {}, ctx)["ldy_capacity"], expected)

    def test_ldy_same_item_keeps_both_models_and_specs(self):
        for pid, models, vids in [
            ("2048348958", ["WW11DB8B95GH", "WW11DB8B95GB"], ["1855570148", "1855568959"]),
            ("2011648160", ["WW9A-10B", "WW9A-10W"], ["2011648161", "2011648170"]),
        ]:
            with self.subTest(pid=pid):
                targets = [washer(v, m, pid=pid) for v, m in zip(vids, models)]
                chars = {v: {"Modellbezeichnung": m, "Bauart": "Frontlader",
                             "_name": f"Waschmaschine {m}, {i + 9} kg"}
                         for i, (v, m) in enumerate(zip(vids, models))}
                with patch.object(compare, "characteristics_map", return_value=chars) as fetch, \
                        patch.object(ldy, "_category_vids", return_value=set()), \
                        patch.object(datasheet, "fetch_datasheet_bytes") as pdf:
                    ctx = ldy.prepare_context(targets + targets[:1])
                self.assertEqual(fetch.call_args.args[0], vids)
                pdf.assert_not_called()
                self.assertEqual([ldy.extract_sku(t, {}, ctx) for t in targets], models)
                self.assertEqual([ldy.extract_spec(t, {}, ctx)["ldy_capacity"] for t in targets],
                                 ["9 kg", "10 kg"])

    def test_missing_option_uses_only_its_own_pdf_then_its_name(self):
        white = washer("1855570148", "WW11DB8B95GH")
        black = washer("1855568959", "WW11DB8B95GB")
        chars = {white["variation_id"]: {"Modellbezeichnung": "WW11DB8B95GH", "Bauart": "Frontlader"}}
        with patch.object(compare, "characteristics_map", return_value=chars), \
                patch.object(ldy, "_category_vids", return_value={white["variation_id"]}), \
                patch.object(datasheet, "fetch_datasheet_bytes", return_value=(b"pdf", 200, None)) as fetch, \
                patch.object(datasheet, "parse", return_value={"sku": "WW11DB8B95GB"}):
            ctx = ldy.prepare_context([white, black])
        fetch.assert_called_once_with(black["energy_datasheet_uri"], 45)
        self.assertEqual(ldy.extract_sku(black, {}, ctx), "WW11DB8B95GB")
        self.assertIsNone(ldy.extract_spec(black, {}, ctx)["ldy_loading_type"])
        ctx["datasheets"] = {}
        self.assertEqual(ldy.extract_sku(black, {}, ctx), "WW11DB8B95GB")
        black["retailer_sku_name"] = "Waschmaschine"
        self.assertIsNone(ldy.extract_sku(black, {}, ctx))

    def test_ldy_comparison_uses_the_option_title_for_loading_type(self):
        vid = "S04FW09YJABM"
        target = {"variation_id": vid, "retailer_sku_name": "TELEFUNKEN Waschmaschine W714AS1DI"}
        html = f'''<a data-navigation="title" data-variation-id="{vid}">
          TELEFUNKEN Waschmaschine W714AS1DI, 7 kg, 1400 U/min, <span>Frontlader</span>,
          Inverter Motor, AquaStop, 7kg, leise, effizient, dark inox</a>
          <div data-variation-id="{vid}">Baumwolle, Eco 40-60°C, Baumwolle Vorwäsche,
          Baumwolle 20°C, Buntwäsche, Wolle, Spülen, Allergy, Schleudern / Abpumpen,
          Hand-/Feinwäsche, Sport / Pflegeleicht, Mix, Jeans / Dunkle Kleidung, Schnell 60 Min.</div>
          <div><span>Bauart</span><div class="pcp_content__column-list">
          <div data-variation-id="{vid}">stehend</div></div></div>
          <div><span>Modellbezeichnung</span><div class="pcp_content__column-list">
          <div data-variation-id="{vid}">W714AS1DI</div></div></div>'''
        with patch.object(compare, "_fetch", return_value=html), \
                patch.object(compare.time, "sleep"), \
                patch.object(ldy, "_category_vids", return_value=set()):
            ctx = ldy.prepare_context([target])
        self.assertEqual(ldy.extract_spec(target, {}, ctx),
                         {"ldy_loading_type": "Front loader", "ldy_capacity": "7 kg"})

    def test_comparison_title_falls_back_only_for_the_unresolved_option(self):
        html = '''<a data-navigation="title" data-variation-id="other">Other model, Toplader</a>
          <div data-variation-id="target">Legacy model, Frontlader</div>'''
        with patch.object(compare, "_fetch", return_value=html):
            names = compare.name_map(["target", "other"], sleep=0)
        self.assertEqual(names, {"target": "Legacy model, Frontlader", "other": "Other model, Toplader"})

    def test_ref_comparison_models_and_values_are_option_specific(self):
        targets = [{"product_id": "1302284733", "variation_id": "2035204916"},
                   {"product_id": "1302284733", "variation_id": "2035204907"}]
        chars = {"2035204916": {"Modellbezeichnung": "GBBSJ2CCPY", "Gesamtrauminhalt": "375 l"},
                 "2035204907": {"Modellbezeichnung": "GBBSJ2CCEP", "Gesamtrauminhalt": "400 l"}}
        with patch.object(compare, "characteristics_map", return_value=chars) as fetch:
            ctx = ref.prepare_context(targets + targets[:1])
        self.assertEqual(fetch.call_args.args[0], [t["variation_id"] for t in targets])
        self.assertEqual([ref.extract_sku(t, {}, ctx) for t in targets], ["GBBSJ2CCPY", "GBBSJ2CCEP"])
        self.assertEqual([ref._ctx_values(t, ctx)["Gesamtrauminhalt"] for t in targets], ["375 l", "400 l"])
        self.assertIsNone(model_sku.model_sku({"product_id": "1302284733", "variation_id": "missing"}, ctx))

    def test_absent_variation_id_does_not_query_product_id(self):
        with patch.object(compare, "characteristics_map") as fetch:
            ctx = model_sku.model_context([{"product_id": "2048348958"}], "waschmaschinen")
        fetch.assert_not_called()
        self.assertEqual(ctx, {"model": {}})

    def test_missing_batch_cell_is_retried_individually_without_replacing_id(self):
        html = '''<div><span>Modellbezeichnung</span><div class="pcp_content__column-list">
          <div data-variation-id="1855570148">WW11DB8B95GH</div>
          <div data-variation-id="1855568959">-</div></div></div>'''
        recovered = html.replace(">-</div>", ">WW11DB8B95GB</div>")
        with patch.object(compare, "_fetch", side_effect=[html, recovered]) as fetch:
            result = compare.characteristics_map(["1855570148", "1855568959"], ["Modellbezeichnung"],
                                                required=["Modellbezeichnung"], retry_rounds=0,
                                                sleep=0, retry_sleep=0)
        self.assertEqual([c.args[0] for c in fetch.call_args_list],
                         [["1855570148", "1855568959"], ["1855568959"]])
        self.assertEqual(result["1855568959"]["Modellbezeichnung"], "WW11DB8B95GB")

    def test_unresolved_option_logs_its_own_id(self):
        with patch.object(compare, "_fetch", return_value=None), patch("sys.stdout", new=io.StringIO()) as log:
            result = compare.characteristics_map(["1855568959"], ["Modellbezeichnung"],
                                                required=["Modellbezeichnung"], retry_rounds=0,
                                                sleep=0, retry_sleep=0)
        self.assertEqual(result, {"1855568959": {}})
        self.assertIn("variation_id=1855568959 reason=missing_after_retries", log.getvalue())

    def test_listing_url_selects_original_option(self):
        data = {"intents": [{"intent": "ranked", "products": [
            {"id": "2048348958", "bestVariationId": "1855568959", "variationPath": "/p/washer-2048348958/"}
        ]}]}
        row = listing.compose_rows(1, 0, data)[0]
        self.assertEqual(row["product_id"], "2048348958")
        self.assertEqual(parse_qs(urlsplit(row["product_url"]).query)["variationId"], ["1855568959"])

    def test_url_replaces_conflicting_option_and_preserves_other_parameters(self):
        url = parsers.ensure_variation_query(
            "https://www.otto.de/p/test/?foo=bar&variationId=wrong#variationId=wrong", "right")
        self.assertEqual(parse_qs(urlsplit(url).query), {"foo": ["bar"], "variationId": ["right"]})
        self.assertNotIn("wrong", url)
        self.assertEqual(parsers.ensure_variation_query(url, "right"), url)
        self.assertTrue(parsers.ensure_variation_query("https://www.otto.de/p/test/#details", "right").endswith("#details"))

    def test_full_output_preserves_item_and_repairs_legacy_target_urls(self):
        targets = [washer("1855570148", "WW11DB8B95GH", 84), washer("1855568959", "WW11DB8B95GB", 248)]
        chars = {t["variation_id"]: {"Modellbezeichnung": t["retailer_sku_name"].split()[-1],
                                    "Bauart": "Frontlader"} for t in targets}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0]))
                writer.writeheader()
                writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(compare, "characteristics_map", return_value=chars), \
                    patch.object(ldy, "_category_vids", return_value=set()), \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}), \
                    patch.object(datasheet, "fetch_datasheet_bytes") as pdf:
                full_output.run(ldy, pdp_supplement="none", detail_sleep=0)
            pdf.assert_not_called()
            with (out / "otto_full_output.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["sku"] for r in rows], ["WW11DB8B95GH", "WW11DB8B95GB"])
            self.assertEqual([r["item"] for r in rows], ["2048348958", "2048348958"])
            self.assertEqual([parse_qs(urlsplit(r["product_url"]).query)["variationId"][0] for r in rows],
                             ["1855570148", "1855568959"])

    def test_ldy_zenrows_fills_only_missing_fields_for_selected_option(self):
        targets = [
            {"product_id": "p" + vid, "variation_id": vid, "main_rank": rank,
             "retailer_sku_name": "Waschtrockner MODEL1234", "product_url": f"https://www.otto.de/p/model-p{vid}/"}
            for rank, vid in enumerate(("v1", "v2", "v3", "v4", "v5", "v6", "v7"), 1)
        ]
        def page(vid, pid):
            return (f'<div class="pdp-frame" data-variation-id="{vid}" data-product-id="{pid}">'
                    '<div class="pdp_details__characteristics-html"><table class="dv_characteristicsTable">'
                    '<tr><td>Beladung</td><td>Frontlader</td></tr>'
                    '<tr><td>Nennkapazität (Waschen)</td><td>9 kg</td></tr>'
                    '<tr><td>Fassungsvermögen</td><td>6 kg</td></tr></table></div></div>').encode()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0])); writer.writeheader(); writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(ldy, "prepare_context", return_value={}), \
                    patch.object(ldy, "extract_spec", side_effect=[
                        {"ldy_loading_type": None, "ldy_capacity": None},
                        {"ldy_loading_type": None, "ldy_capacity": None},
                        {"ldy_loading_type": "Top-loading", "ldy_capacity": "8 kg"},
                        {"ldy_loading_type": None, "ldy_capacity": None},
                        {"ldy_loading_type": None, "ldy_capacity": None},
                        {"ldy_loading_type": None, "ldy_capacity": None},
                        {"ldy_loading_type": None, "ldy_capacity": None},
                    ]), patch.object(browser, "BrowserSession") as session, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                session.return_value.fetch_pdp.side_effect = [
                    {"status": 200, "detail_present": True, "body": page("v1", "pv1")},
                    {"status": 200, "detail_present": True, "body": page("wrong", "pv2")},
                    {"status": 200, "detail_present": True, "body": page("v4", "other")},
                    {"status": 200, "detail_present": True, "body": page("v5", "pv5") + page("other", "other")},
                    {"status": 200, "detail_present": True, "body": page("v6", "pv6") + b'<div class="pdp-frame"></div>'},
                    {"status": 200, "detail_present": True, "body": page("v7", "pv7").replace(
                        b'</table>', b'<tr><td>Beladung</td><td>Toplader</td></tr></table>')},
                ]
                manifest = full_output.run(ldy, detail_sleep=0)
            with (out / "otto_full_output.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([(r["ldy_loading_type"], r["ldy_capacity"]) for r in rows],
                             [("Front loader", "9 kg"), ("", ""), ("Top-loading", "8 kg"), ("", ""), ("", ""), ("", ""), ("", "9 kg")])
            self.assertEqual(session.return_value.fetch_pdp.call_count, 6)
            self.assertEqual(manifest["attempts"][1]["pdp_supplement_failure_reason"], "variation_id_mismatch")
            self.assertEqual(manifest["attempts"][3]["pdp_supplement_failure_reason"], "product_id_mismatch")
            self.assertEqual(manifest["attempts"][4]["pdp_supplement_failure_reason"], "variation_id_unverified")
            self.assertEqual(manifest["attempts"][5]["pdp_supplement_failure_reason"], "variation_id_unverified")
            self.assertEqual(manifest["attempts"][6]["pdp_supplement_failure_reason"], "pdp_conflicting_characteristics:ldy_loading_type")

    def test_ldy_retries_new_pdp_session_after_open_failure_without_exposing_error(self):
        targets = [{"product_id": "p1", "variation_id": "v1", "main_rank": 1,
                    "retailer_sku_name": "Waschmaschine MODEL123", "product_url": "https://www.otto.de/p/model-p1/"},
                   {"product_id": "p2", "variation_id": "v2", "main_rank": 2,
                    "retailer_sku_name": "Waschmaschine MODEL123", "product_url": "https://www.otto.de/p/model-p2/"}]
        html = (b'<div class="pdp-frame" data-variation-id="v2" data-product-id="p2">'
                b'<div class="pdp_details__characteristics-html"><table class="dv_characteristicsTable">'
                b'<tr><td>Beladung</td><td>Frontlader</td></tr></table></div></div>')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0])); writer.writeheader(); writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(ldy, "prepare_context", return_value={}), \
                    patch.object(ldy, "extract_spec", side_effect=lambda *a, **kw: {"ldy_loading_type": None, "ldy_capacity": "9 kg"}), \
                    patch.object(browser, "BrowserSession") as sessions, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                first, second = sessions.return_value, type(sessions.return_value)()
                sessions.side_effect = [first, second]
                first.open.side_effect = RuntimeError("secret-test-token")
                second.fetch_pdp.return_value = {"status": 200, "detail_present": True, "body": html}
                manifest = full_output.run(ldy, detail_sleep=0)
            self.assertEqual(sessions.call_count, 2)
            first.close.assert_called_once()
            second.close.assert_called_once()
            self.assertEqual(manifest["attempts"][0]["pdp_supplement_error"], "RuntimeError")
            self.assertNotIn("secret-test-token", json.dumps(manifest))
            self.assertEqual(manifest["attempts"][1]["spec"]["ldy_loading_type"], "Front loader")

    def test_ldy_pdp_session_failures_are_bounded_and_rows_continue(self):
        targets = [{"product_id": f"p{i}", "variation_id": f"v{i}", "main_rank": i,
                    "retailer_sku_name": "Waschmaschine MODEL123", "product_url": f"https://www.otto.de/p/model-p{i}/"}
                   for i in range(1, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0])); writer.writeheader(); writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(ldy, "prepare_context", return_value={}), \
                    patch.object(ldy, "extract_spec", side_effect=lambda *a, **kw: {"ldy_loading_type": None, "ldy_capacity": "9 kg"}), \
                    patch.object(browser, "BrowserSession") as sessions, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                sessions.return_value.open.side_effect = RuntimeError("token")
                manifest = full_output.run(ldy, detail_sleep=0)
            self.assertEqual(sessions.call_count, 2)
            self.assertEqual(sessions.return_value.close.call_count, 2)
            self.assertEqual(manifest["output_rows"], 3)
            self.assertEqual(manifest["attempts"][2]["pdp_supplement_failure_reason"], "PdpSessionRetryLimit")

    def test_ldy_interrupt_during_pdp_open_closes_browser(self):
        target = {"product_id": "p1", "variation_id": "v1", "main_rank": 1,
                  "retailer_sku_name": "Waschmaschine MODEL123", "product_url": "https://www.otto.de/p/model-p1/"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(target)); writer.writeheader(); writer.writerow(target)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(ldy, "prepare_context", return_value={}), \
                    patch.object(ldy, "extract_spec", return_value={"ldy_loading_type": None, "ldy_capacity": "9 kg"}), \
                    patch.object(browser, "BrowserSession") as session, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                session.return_value.open.side_effect = KeyboardInterrupt()
                with self.assertRaises(KeyboardInterrupt):
                    full_output.run(ldy, detail_sleep=0)
            session.return_value.close.assert_called_once()

    def test_ldy_pdp_product_alias_requires_numeric_id_and_matching_canonical(self):
        cases = [("C123", "123", "C123"), ("C124", "124", "other"), ("CAB12", "AB12", "CAB12")]
        targets = [{"product_id": pid, "variation_id": f"v{i}", "main_rank": i,
                    "retailer_sku_name": "Waschmaschine MODEL123", "product_url": f"https://www.otto.de/p/model-{pid}/"}
                   for i, (pid, _, _) in enumerate(cases, 1)]
        bodies = [(f'<link rel="canonical" href="https://www.otto.de/p/model-{canonical}/">'
                   f'<div class="pdp-frame" data-product-id="{actual}" data-variation-id="v{i}">'
                   '<div class="pdp_details__characteristics-html"><table class="dv_characteristicsTable">'
                   '<tr><td>Beladung</td><td>Frontlader</td></tr></table></div></div>').encode()
                  for i, (_, actual, canonical) in enumerate(cases, 1)]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0])); writer.writeheader(); writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(ldy, "prepare_context", return_value={}), \
                    patch.object(ldy, "extract_spec", side_effect=lambda *a, **kw: {"ldy_loading_type": None, "ldy_capacity": "9 kg"}), \
                    patch.object(browser, "BrowserSession") as session, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                session.return_value.fetch_pdp.side_effect = [{"status": 200, "detail_present": True, "body": body} for body in bodies]
                manifest = full_output.run(ldy, detail_sleep=0)
            self.assertEqual([a["spec"]["ldy_loading_type"] for a in manifest["attempts"]], ["Front loader", None, None])
            self.assertEqual([a["pdp_supplement_failure_reason"] for a in manifest["attempts"]],
                             [None, "product_id_mismatch", "product_id_mismatch"])

    def test_full_output_uses_hdr_only_from_selected_option(self):
        targets = [
            {"product_id": "1995107347", "variation_id": vid, "main_rank": rank,
             "retailer_sku_name": "Samsung GQ55Q6FAAU QLED-Fernseher 55 Zoll",
             "product_url": "https://www.otto.de/p/samsung-gq55q6faau-1995107347/"}
            for rank, vid in enumerate(("1995107348", "other", "no-hdr"), 1)
        ]
        html = (ROOT / "references" / "har" / "detail_sample.html").read_bytes()
        no_hdr = html.replace(tv.HDR_POWER_LABEL.encode(), b"Unrelated characteristic")
        responses = [html, html, no_hdr.replace(b'"1995107348"', b'"no-hdr"')]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with (out / "otto_final_targets.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(targets[0]))
                writer.writeheader()
                writer.writerows(targets)
            with patch.object(full_output, "category_output_root", return_value=out), \
                    patch.object(tv, "prepare_context", return_value={}), \
                    patch.object(browser, "BrowserSession") as session, \
                    patch.object(full_output, "fetch_similar_product_names", return_value={}), \
                    patch.object(full_output, "collect_review", return_value={}), \
                    patch.object(full_output, "refresh_sticker_fields"), \
                    patch.object(full_output, "sticker_diagnostics", return_value={}):
                session.return_value.fetch_pdp.side_effect = [
                    {"status": 200, "detail_present": True, "body": body} for body in responses
                ]
                manifest = full_output.run(tv, detail_sleep=0)
            with (out / "otto_full_output.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([r["estimated_annual_electricity_use"] for r in rows], ["114 W", "", ""])
            self.assertEqual(manifest["attempts"][1]["pdp_supplement_failure_reason"], "variation_id_mismatch")
            self.assertEqual(manifest["attempts"][2]["pdp_supplement_failure_reason"], "no_allowed_fields_recovered")


class CompletePdfModelTests(unittest.TestCase):
    def test_letter_prefix_numeric_segment_and_suffix_are_all_preserved(self):
        for model in ["PRGF 6421 XP4E", "PRGF 6441 XP4E", "WAM 914 A", "CR 8052", "75E7S PRO"]:
            with self.subTest(model=model):
                self.assertEqual(datasheet._sku({}, f"Modellkennung: {model}\nArt des Kühlgeräts: freistehend"), model)
                self.assertEqual(datasheet._sku({}, "", [["Modellkennung", model]]), model)

    def test_merged_next_field_and_unrelated_address_are_not_model_text(self):
        self.assertEqual(datasheet._sku({}, "Modellkennung: PRGF 6421 XP4E Art des Kühlgeräts: freistehend"),
                         "PRGF 6421 XP4E")
        self.assertIsNone(datasheet._sku({}, "Anschrift: Samsung GU46 6GG"))
        self.assertIsNone(datasheet._sku({}, "Modellkennung:\nAnschrift: Samsung GU46 6GG"))


if __name__ == "__main__":
    unittest.main()
