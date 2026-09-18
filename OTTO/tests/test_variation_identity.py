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

from common import compare, datasheet, full_output, listing, model_sku, parsers
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

    def test_ref_and_tv_comparison_models_and_values_are_option_specific(self):
        targets = [{"product_id": "1302284733", "variation_id": "2035204916"},
                   {"product_id": "1302284733", "variation_id": "2035204907"}]
        chars = {"2035204916": {"Modellbezeichnung": "GBBSJ2CCPY", "Gesamtrauminhalt": "375 l",
                                tv.HDR_POWER_LABEL: "100 W"},
                 "2035204907": {"Modellbezeichnung": "GBBSJ2CCEP", "Gesamtrauminhalt": "400 l",
                                tv.HDR_POWER_LABEL: "120 W"}}
        with patch.object(compare, "characteristics_map", return_value=chars) as fetch:
            ctx = ref.prepare_context(targets + targets[:1])
        self.assertEqual(fetch.call_args.args[0], [t["variation_id"] for t in targets])
        self.assertEqual([ref.extract_sku(t, {}, ctx) for t in targets], ["GBBSJ2CCPY", "GBBSJ2CCEP"])
        self.assertEqual([ref._ctx_values(t, ctx)["Gesamtrauminhalt"] for t in targets], ["375 l", "400 l"])
        self.assertEqual([tv.extract_spec(t, {}, ctx)["estimated_annual_electricity_use"] for t in targets],
                         ["100 W", "120 W"])
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
