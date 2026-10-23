"""Offline-only DRY contracts. Synthetic sources, blocked sockets, transactional fake DB."""
import copy
import csv
import io
import json
import socket
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import io_util, listing
from dry import client, config, fields, pipeline, storage


class Log:
    def __init__(self):
        self.records = []

    def emit(self, event, **values):
        self.records.append((event, values))

    @contextmanager
    def stage(self, name):
        yield

    @contextmanager
    def activity(self, name, **values):
        yield


def target(number, name=None, pid=None):
    return dict(product_id=pid or str(number), variation_id="v" + str(number),
                retailer_sku_name=name or f"Brand Wärmepumpentrockner DR{number:04}, 9 kg",
                product_url=f"https://www.otto.de/p/dryer-{pid or number}/?variationId=v{number}")


def chars(number):
    return {"Modellbezeichnung": "DR" + str(number), "_name": f"Brand Wärmepumpentrockner DR{number}, 9 kg, Frontlader"}


def rating_document(mean="4,3", count="123", font="future-font"):
    return (f'<div class="pdp_cr-rating"><div class="pdp_cr-rating-score">'
            f'<span class="{font}">{mean}</span><span>von 5</span></div>({count})</div>').encode()


def tile_fetch(rejected):
    def fetch(url, endpoint, **kwargs):
        if endpoint != "tiles":
            raise AssertionError("UNEXPECTED_ENDPOINT")
        ids = parse_qs(urlsplit(url).query)["variationIds"][0].split(",")
        if any(vid in rejected for vid in ids):
            raise client.RequestFailure("bad_id", 400)
        return [{"variationId": vid, "name": f"Trockner DR{vid}, 9 kg"} for vid in ids], {}
    return fetch


class Offline(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("LIVE_NETWORK_FORBIDDEN"))
        self.environment = patch.object(io_util, "_env_text", side_effect=AssertionError("SECRET_READ_FORBIDDEN"))
        self.network.start()
        self.environment.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.environment.stop)


class ClassificationTests(Offline):
    def test_standalone_types_and_features(self):
        for name in (
            "Brand Wärmepumpentrockner DR900, 9 kg", "Brand Kondenstrockner AB123",
            "Brand Ablufttrockner AB123", "Brand Wäschetrockner AB123, 9 kg",
            "Brand Wärme-Pumpentrockner AB123", "Brand Trockner AB123, 9 kg mit Filter und Schlauch",
            "Brand Wärmepumpen-Trockner AB123",
            "Brand Wärmepumpentrockner AB123, kompatibel mit Waschmaschine",
            "Brand Wärmepumpentrockner AB123, passend für Waschmaschine und Untergestell",
            "Brand Wärmepumpentrockner AB123 für Waschmaschine und Untergestell",
            "Wärmepumpentrockner DR900, 9 kg, mit Waschmaschine vernetzbar und per App bedienbar",
            "Wärmepumpentrockner DR900 mit Ersatzfilter und Schlauch",
            "Wärmepumpentrockner DR900, mobile Steuerung",
        ):
            with self.subTest(name=name):
                self.assertTrue(fields.classify(name)[0])

    def test_exclusions(self):
        for name in (
            "Brand Waschmaschine WA900", "Brand Waschtrockner WA900", "Wasch-Trockner WA900",
            "Waschmaschine WA900 + Wärmepumpentrockner DR900", "Wärmepumpentrockner DR900 und Waschmaschine WA900 Set",
            "Wärmepumpentrockner DR900 & Waschmaschine WA900", "Mobiler Wäschetrockner DR900",
            "Tragbarer Wäschetrockner DR900", "Mini-Trockner DR900", "Trockner DR900 portable",
            "Brand AirDresser", "Brand Styler", "Brand Wäscheschleuder",
            "Brand Haartrockner", "Brand Wäscheständer", "Flügelwäschetrockner XL",
            "Brand Trockenschrank", "Brand Trockengestell", "Brand Trockensack",
            "Untergestell für Wärmepumpentrockner", "Zwischenbaurahmen Waschmaschine Trockner",
            "Ersatzfilter für Trockner", "Trocknerbälle für Wäschetrockner",
            "Filter für Trockner", "Trockner-Untergestell", "Wäschetrockner-Abdeckung",
            "Wärmepumpentrockner Ersatzfilter DR900",
        ):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])

    def test_conflicting_structured_type_is_included_for_review(self):
        self.assertEqual(fields.classify("Wärmepumpentrockner DR900", {"Produkttyp": "Waschtrockner"}),
                         (True, "ambiguous_included_conflicting_product_types"))
        self.assertTrue(fields.classify("Brand DR900", {"Produkttyp": "Wärmepumpentrockner"})[0])

    def test_unknown_is_reported_ambiguous(self):
        self.assertEqual(fields.classify("Brand 900"), (True, "ambiguous_included_product_type"))

    def test_uncertain_generic_dryer_is_included_for_review(self):
        self.assertEqual(fields.classify("Wäschetrockner Modell 180, 18 m Leinenlänge"),
                         (True, "ambiguous_included_generic_dryer"))
        self.assertTrue(fields.classify("Wäschetrockner Modell 180")[0])
        self.assertTrue(fields.classify("Wäschetrockner DR900", {"Nennkapazität": "9 kg"})[0])
        self.assertTrue(fields.classify("Wäschetrockner DR900", {"Gewicht": "9 kg"})[0])

    def test_multiple_types_without_explicit_composition_are_included(self):
        self.assertTrue(fields.classify("Wärmepumpentrockner DR900, Waschmaschine vernetzbar")[0])
        self.assertTrue(fields.classify("Waschmaschine kompatibel Wärmepumpentrockner DR900")[0])

    def test_included_accessories_are_not_a_dryer_exclusion(self):
        name = "Wärmepumpentrockner DR900"
        self.assertTrue(fields.classify(name, {"Set-Bestandteile": "Schlauch, Trockner DR900"})[0])
        self.assertFalse(fields.classify(name, {"Set-Bestandteile": "Waschmaschine WA900, Trockner DR900"})[0])

    def test_loading_does_not_infer_installation_or_technology(self):
        for value in ("freistehend", "Wärmepumpentrockner", "Standgerät", "Einbau", "Frontlader und Toplader"):
            self.assertIsNone(fields.loading(value))
        self.assertEqual(fields.loading("Toplader"), "Top-loading")

    def test_capacity_priority_and_conflict_diagnostics(self):
        t = target(1, "Wärmepumpentrockner DR123, 8 kg")
        t["top_infos"] = {"Nennkapazität": "7 kg", "Gewicht": "50 kg", "Kapazität Waschen": "20 kg"}
        c = {"_name": "Wärmepumpentrockner DR123, 9 kg", "Nennkapazität": "6 kg"}
        log = Log()
        self.assertEqual(fields.choose(t, c, {"capacity": "5 kg"}, log)["capacity"], "9 kg")
        cap_event = next(v for e, v in log.records if v["field"] == "capacity")
        self.assertTrue(cap_event["conflict"])
        del c["_name"]
        self.assertEqual(fields.choose(t, c)["capacity"], "8 kg")
        t["retailer_sku_name"] = "Trockner DR123"
        self.assertEqual(fields.choose(t, c)["capacity"], "7 kg")
        t["top_infos"] = {}
        self.assertEqual(fields.choose(t, c)["capacity"], "6 kg")
        self.assertEqual(fields.choose(t, {}, {"capacity": "5 kg"})["capacity"], "5 kg")

    def test_capacity_units_weight_and_wash_labels(self):
        self.assertEqual(fields.capacity("9,0 kg"), "9 kg")
        self.assertIsNone(fields.capacity("Produktgewicht 50 kg"))
        self.assertIsNone(fields.capacity("Trockner DR900 Gewicht 50 kg"))
        self.assertIsNone(fields.capacity("Trockner DR900, 50 kg Produktgewicht"))
        self.assertIsNone(fields.capacity("Trockner 8 kg / 9 kg"))
        self.assertFalse(fields.capacity_label("Nennkapazität (Waschen)"))

    def test_loading_priority_and_actual_dryer_category(self):
        t = target(1)
        c = {"_name": "Wärmepumpentrockner, Frontlader", "Beladung": "Toplader", "Bauart": "Toplader"}
        self.assertEqual(fields.choose(t, c)["loading_type"], "Front loader")
        del c["_name"]
        self.assertEqual(fields.choose(t, c)["loading_type"], "Top-loading")
        c = {"Bauart": "freistehend", "Kategorie": "Waschmaschinen > Frontlader"}
        self.assertIsNone(fields.choose(t, c)["loading_type"])
        c["Kategorie"] = "Trockner > Frontlader"
        self.assertEqual(fields.choose(t, c)["loading_type"], "Front loader")

    def test_model_priority_and_split_models(self):
        t = target(1, "Brand Wärmepumpentrockner WAM 914 A, 9 kg")
        self.assertEqual(fields.choose(t, {"Modellbezeichnung": "COMP900"}, {"sku": "PDF900"})["sku"], "COMP900")
        self.assertEqual(fields.choose(t, {}, {"sku": "PDF900"})["sku"], "PDF900")
        self.assertEqual(fields.choose(t, {})["sku"], "WAM 914 A")
        self.assertEqual(fields.model_from_name("Brand WT47X900 Wärmepumpentrockner"), "WT47X900")
        self.assertIsNone(fields.model_from_name("Wärmepumpentrockner 9 kg, weiß"))
        self.assertEqual(fields.model_from_name("Wärmepumpentrockner DR900 besonders leise"), "DR900")

    def test_product_url_only_keeps_original_option(self):
        url = fields.public_product_url("https://www.otto.de/p/dryer-100/?tracking=discard&variationId=wrong#x", "v1")
        self.assertEqual(url, "https://www.otto.de/p/dryer-100/?variationId=v1")
        self.assertIsNone(fields.public_product_url("https://example.com/p/x", "v1"))


class ClientTests(Offline):
    def test_minimal_tile_ignores_unrequested_fields(self):
        tile = {"variationId": "v1", "title": {"shortened": "Trockner DR900"}, "brand": "Brand",
                "price": {"retailPrice": "499,99", "suggestedRetailPrice": "700,00"}, "saleTags": {"discount": "-29%"},
                "deal": {"image": "do-not-read"}, "customerReviews": {"amount": 999}, "delivery": "unused",
                "energyLabels": [{"datasheetUri": "https://d.otto.de/own.pdf", "category": "unused"}],
                "topInfos": [{"label": "Nennkapazität", "value": "9 kg"}, {"label": "Energieverbrauch", "value": "unused"}]}
        actual = client.minimal_tile(tile)
        self.assertEqual(set(actual), {"retailer_sku_name", "top_infos", "pdf_uri", "final_sku_price", "original_sku_price", "savings"})
        self.assertEqual(actual["final_sku_price"], "499,99 €")
        self.assertEqual(actual["top_infos"], {"Nennkapazität": "9 kg"})

    def test_ldy_ad_placement_matches(self):
        products = [dict(id=str(i), bestVariationId="v"+str(i), variationPath=f"/p/dryer-{i}/", name=f"Trockner DR{i}") for i in range(130)]
        data = {"intents": [{"intent": "ranked", "products": products, "count": 200},
                            {"intent": "sponsored", "products": [dict(products[0], id="ad", bestVariationId="vad")]}]}
        c = client.Client(Log())
        with patch.object(c, "fetch", return_value=(data, {})):
            actual, consumed, end, _ = c.page(1, 0)
        expected = listing.compose_rows(1, 0, data)
        self.assertEqual([r["product_id"] for r in actual], [r["product_id"] for r in expected])
        self.assertEqual(consumed, 119)
        self.assertFalse(end)

    def test_listing_errors_are_not_end(self):
        c = client.Client(Log())
        for data in ({}, {"intents": [{"intent": "ranked", "products": [], "count": 100}]}):
            with patch.object(c, "fetch", return_value=(data, {})), self.assertRaises(client.RequestFailure):
                c.page(1, 0)
        with patch.object(c, "fetch", return_value=({"intents": [{"intent": "ranked", "products": [], "count": 0}]}, {})):
            self.assertTrue(c.page(1, 0)[2])

    def test_comparison_keyed_by_own_option(self):
        html = b'<div><span>Modellbezeichnung</span><div class="pcp_content__column-list"><div data-variation-id="v2">MODEL2</div><div data-variation-id="v1">MODEL1</div></div></div>'
        c = client.Client(Log())
        with patch.object(c, "fetch", return_value=(html, {"status": 200})) as fetch:
            values = c.comparison(["v1", "v2", "absent"])
            c.comparison(["v1"])
        self.assertEqual(values["v1"]["Modellbezeichnung"], "MODEL1")
        self.assertEqual(values["v2"]["Modellbezeichnung"], "MODEL2")
        self.assertEqual(values["absent"]["_unavailable"], "comparison_option_missing")
        self.assertEqual(fetch.call_count, 1)

    def test_latest_rating_fonts_and_no_review_pagination(self):
        for font in ("oc-headline-300", "oc-display-200", "future-font"):
            c = client.Client(Log())
            with patch.object(c, "fetch", return_value=(rating_document(font=font), {"status": 200})) as fetch:
                value = c.rating("100")
            self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), ("4.3", 123, "rated"))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], "https://www.otto.de/kundenbewertungen/100/")

    def test_unrated_only_explicit_zero(self):
        c = client.Client(Log())
        body = '<div class="js_pdp_cr_sidebar__noReviewsText">Für diesen Artikel wurde noch keine Bewertung abgegeben.</div>'.encode()
        with patch.object(c, "fetch", return_value=(body, {"status": 200})):
            value = c.rating("100")
        self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), ("0.0", 0, "unrated"))

    def test_rating_failure_preserves_independent_field(self):
        for body, expected in ((rating_document(mean="bad"), (None, 123)),
                               (rating_document(count="bad"), ("4.3", None))):
            with self.subTest(expected=expected):
                c = client.Client(Log())
                with patch.object(c, "fetch", side_effect=[(body, {"status": 200}), client.RequestFailure("timeout"), client.RequestFailure("timeout")]) as fetch, patch.object(client.time, "sleep"):
                    value = c.rating("100")
                self.assertEqual((value["average_rating"], value["rating_count"]), expected)
                self.assertEqual(value["rating_state"], "failed")
                self.assertEqual(fetch.call_count, 3)

    def test_partial_rating_merge_drops_contradictory_zero_in_both_orders(self):
        partials = [rating_document(mean="bad", count="0"), rating_document(count="bad")]
        for bodies in (partials, partials[::-1]):
            with self.subTest(order=bodies):
                log = Log()
                c = client.Client(log)
                responses = [(body, {"status": 200}) for body in bodies] + [client.RequestFailure("timeout")]
                with patch.object(c, "fetch", side_effect=responses) as fetch, patch.object(client.time, "sleep"):
                    value = c.rating("100")
                self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), ("4.3", None, "failed"))
                self.assertEqual(fetch.call_count, 3)
                self.assertTrue(all(call.args[0] == "https://www.otto.de/kundenbewertungen/100/" for call in fetch.call_args_list))
                self.assertFalse(any(v.get("average_rating") == "4.3" and v.get("rating_count") == 0
                                     for event, v in log.records if event == "rating"))

    def test_partial_rating_pair_stays_failed_until_one_response_is_valid(self):
        c = client.Client(Log())
        responses = [(rating_document(count="bad"), {"status": 200}),
                     (rating_document(mean="bad"), {"status": 200}), client.RequestFailure("timeout")]
        with patch.object(c, "fetch", side_effect=responses) as fetch, patch.object(client.time, "sleep"):
            value = c.rating("100")
        self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), ("4.3", 123, "failed"))
        self.assertEqual(fetch.call_count, 3)

    def test_complete_rating_response_supersedes_partial_values(self):
        for body, expected in ((rating_document(), ("4.3", 123, "rated")),
                               (rating_document(mean="0", count="0"), ("0.0", 0, "unrated"))):
            with self.subTest(expected=expected):
                c = client.Client(Log())
                responses = [(rating_document(count="bad"), {"status": 200}), (body, {"status": 200})]
                with patch.object(c, "fetch", side_effect=responses) as fetch, patch.object(client.time, "sleep"):
                    value = c.rating("100")
                self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), expected)
                self.assertEqual(fetch.call_count, 2)

    def test_missing_rating_summary_is_null_not_unrated(self):
        c = client.Client(Log())
        with patch.object(c, "fetch", return_value=(b"<html></html>", {"status": 200})) as fetch, patch.object(client.time, "sleep"):
            value = c.rating("100")
        self.assertEqual((value["average_rating"], value["rating_count"], value["rating_state"]), (None, None, "failed"))
        self.assertEqual(fetch.call_count, 3)

    def test_wrong_review_product_and_redirect_are_null(self):
        c = client.Client(Log(), attempts=1)
        for body, response in (
            (rating_document() + b'<div class="js_cr_sidebar"><a href="/p/dryer-999/">x</a></div>', {"status": 200}),
            (rating_document(), {"status": 200, "final_url": "https://www.otto.de/login/"}),
        ):
            with patch.object(c, "fetch", return_value=(body, response)):
                value = c.rating("100")
            self.assertIsNone(value["average_rating"])
            self.assertIsNone(value["rating_count"])

    def test_network_retries_bounded_and_no_raw_error(self):
        log = Log()
        c = client.Client(log, pause=0)
        with patch.object(client, "urlopen", side_effect=OSError("UNSAFE_RESPONSE_CONTENT")) as fetch, patch.object(client.time, "sleep"):
            with self.assertRaises(client.RequestFailure):
                c.fetch("https://www.otto.de/", "listing")
        self.assertEqual(fetch.call_count, 3)
        self.assertNotIn("UNSAFE_RESPONSE_CONTENT", str(log.records))

    def test_http_400_split_recovers(self):
        c = client.Client(Log())
        def response(url, endpoint, **kwargs):
            ids = parse_qs(urlsplit(url).query)["variationIds"][0].split(",")
            if len(ids) > 1:
                raise client.RequestFailure("bad_batch", 400)
            return [{"variationId": ids[0], "name": "Trockner DR900"}], {}
        with patch.object(c, "fetch", side_effect=response):
            self.assertEqual(set(c.tiles(["v1", "v2"])), {"v1", "v2"})

    def test_http_400_one_or_two_rejected_ids_in_batch_are_skipped(self):
        for rejected in ({"v1"}, {"v1", "v3"}):
            with self.subTest(rejected=rejected):
                c = client.Client(Log())
                with patch.object(c, "fetch", side_effect=tile_fetch(rejected)), patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3):
                    self.assertEqual(set(c.tiles(["v1", "v2", "v3"])), {"v1", "v2", "v3"} - rejected)
                self.assertEqual(c.tile_http_400_failures, len(rejected))

    def test_http_400_three_rejections_in_batch_abort_despite_successes(self):
        log = Log()
        c = client.Client(log)
        with patch.object(c, "fetch", side_effect=tile_fetch({"v1", "v3", "v5"})) as fetch, patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3):
            with self.assertRaisesRegex(client.RequestFailure, "systemic_tile_rejection"):
                c.tiles(["v1", "v2", "v3", "v4", "v5"])
        visited = [parse_qs(urlsplit(call.args[0]).query)["variationIds"][0] for call in fetch.call_args_list]
        self.assertIn("v2", visited)
        self.assertIn("v4", visited)
        self.assertEqual(c.tile_http_400_failures, 3)
        failures = [v for e, v in log.records if e == "failure"]
        self.assertEqual([v["batch_failures"] for v in failures], [1, 2, 3])
        self.assertEqual([v["outcome"] for v in failures], ["skipped_id", "skipped_id", "aborted_systemic_400"])

    def test_http_400_separate_batches_reset_abort_count_but_keep_total(self):
        log = Log()
        c = client.Client(log)
        with patch.object(c, "fetch", side_effect=tile_fetch({"v1", "v3", "v5"})), patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3):
            for bad, good in (("v1", "v2"), ("v3", "v4"), ("v5", "v6")):
                self.assertEqual(set(c.tiles([bad, good])), {good})
        failures = [v for e, v in log.records if e == "failure"]
        self.assertEqual([v["batch_failures"] for v in failures], [1, 1, 1])
        self.assertEqual([v["total_failures"] for v in failures], [1, 2, 3])
        self.assertEqual(c.tile_http_400_failures, 3)

    def test_http_400_uses_ldy_failure_limit_setting(self):
        c = client.Client(Log())
        with patch.object(c, "fetch", side_effect=tile_fetch({"v1", "v2"})), patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 2):
            with self.assertRaisesRegex(client.RequestFailure, "systemic_tile_rejection"):
                c.tiles(["v1", "v2"])
        self.assertEqual(c.tile_http_400_failures, 2)

    def test_non_400_tile_errors_keep_existing_retry_and_abort_behavior(self):
        for status, attempts in ((403, 1), (503, 3)):
            with self.subTest(status=status):
                c = client.Client(Log(), pause=0)
                failure = HTTPError("https://www.otto.de/", status, "fake_error", {}, None)
                with patch.object(client, "urlopen", side_effect=failure) as fetch, patch.object(client.time, "sleep"):
                    with self.assertRaises(client.RequestFailure) as caught:
                        c.tiles(["v1", "v2"])
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(fetch.call_count, attempts)
                self.assertEqual(c.tile_http_400_failures, 0)

    def test_real_http_adapter_success_and_response_limit(self):
        c = client.Client(Log(), pause=0, attempts=1)
        response = MagicMock()
        response.status, response.geturl.return_value = 200, "https://www.otto.de/"
        response.__enter__.return_value = response
        response.read1.side_effect = [b'{"valid":true}', b'']
        with patch.object(client, "urlopen", return_value=response):
            payload, meta = c.fetch("https://www.otto.de/", "listing", kind="json")
        self.assertEqual(payload, {"valid": True})
        self.assertEqual(meta["status"], 200)
        response.read1.side_effect = [b'x' * 8_000_001]
        with patch.object(client, "urlopen", return_value=response), self.assertRaisesRegex(client.RequestFailure, "response_too_large"):
            c.fetch("https://www.otto.de/", "listing")

    def test_missing_pdf_link_does_not_request_anything(self):
        c = client.Client(Log())
        with patch.object(c, "fetch") as fetch:
            self.assertEqual(c.pdf(None, need_sku=True, need_capacity=False)["_unavailable"], "own_pdf_link_missing")
        fetch.assert_not_called()

    def test_pdf_only_required_labels_and_stops_early(self):
        first, second = MagicMock(), MagicMock()
        first.extract_text.return_value = "Modellkennung: DR900\nNennkapazität (kg) 9,0\nEnergieverbrauch 300 kWh"
        pdf = MagicMock()
        pdf.__enter__.return_value.pages = [first, second]
        with patch.dict(sys.modules, {"pdfplumber": types.SimpleNamespace(open=MagicMock(return_value=pdf))}):
            value = client.parse_pdf(b"%PDF", need_sku=True, need_capacity=True)
        self.assertEqual(value["sku"], "DR900")
        self.assertEqual(value["capacity"], "9 kg")
        self.assertEqual(value["capacity_raw"], "9,0")
        second.extract_text.assert_not_called()


class Source:
    def __init__(self, pages, overrides=None, failure=None):
        self.pages, self.overrides, self.failure = pages, overrides or {}, failure
        self.calls = []

    def page(self, page, offset):
        self.calls.append(("page", page, offset))
        if page == self.failure:
            raise client.RequestFailure("listing_timeout")
        rows = self.pages[page-1]
        return copy.deepcopy(rows), len(rows), page == len(self.pages), tuple(t["variation_id"] for t in rows)

    def tiles(self, ids):
        self.calls.append(("tiles", ids))
        return {v: dict(final_sku_price="499,00 €", top_infos={}) for v in ids}

    def comparison(self, ids):
        self.calls.append(("comparison", ids))
        return {v: self.overrides.get(v, chars(v[1:])) for v in ids}

    def pdf(self, uri, **needed):
        self.calls.append(("pdf", uri, needed))
        return {"sku": "PDF900", "capacity": "9 kg"}

    def rating(self, product_id):
        self.calls.append(("rating", product_id))
        return dict(average_rating="4.3", rating_count=123, rating_state="rated")


class PipelineTests(Offline):
    def collect(self, source, limit):
        state = dict(target=limit, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-23 12:00:00")
        rows = pipeline.collect(source, Log(), state, lambda rows: None)
        return rows, state

    def test_100_rows_ranks_filters_and_duplicates(self):
        input_rows = [target(900, "Waschtrockner X900"), target(1), target(2, target(1)["retailer_sku_name"])] + [target(i) for i in range(3, 110)]
        source = Source([input_rows])
        rows, state = self.collect(source, 100)
        self.assertEqual(len(rows), 100)
        self.assertEqual([r["main_rank"] for r in rows], list(range(1, 101)))
        self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101)))
        self.assertEqual(tuple(rows[0]), config.COLUMNS)
        self.assertEqual(state["counts"]["excluded"], 1)
        self.assertEqual(state["counts"]["duplicates"], 1)
        self.assertEqual(len([c for c in source.calls if c[0] == "rating"]), 100)
        self.assertFalse(any(c[0] == "pdf" for c in source.calls))

    def test_fewer_than_target_is_shortfall_and_more_than_six_pages(self):
        source = Source([[target(i)] for i in range(1, 9)])
        rows, state = self.collect(source, 100)
        self.assertEqual(len(rows), 8)
        self.assertEqual(state["collection_status"], "shortfall")
        self.assertEqual(state["shortfall_reason"]["missing"], 92)

    def test_failure_not_shortfall_and_repeated_page_aborts(self):
        for source in (Source([[target(1)], [target(2)]], failure=2), Source([[target(1)], [target(1)]])):
            with self.assertRaises(client.RequestFailure):
                self.collect(source, 100)

    def test_comparison_exclusion_replenishes_target(self):
        source = Source([[target(1, "Brand X900"), target(2), target(3)]], {"v1": {"Produkttyp": "Waschtrockner"}})
        rows, state = self.collect(source, 2)
        self.assertEqual([r["item"] for r in rows], ["2", "3"])
        self.assertFalse(any(c == ("rating", "1") for c in source.calls))

    def test_uncertain_item_is_collected_and_marked_for_review(self):
        source = Source([[target(1, "Brand DR900"), target(2, "Waschmaschine WA900"), target(3)]], {"v1": {}})
        state = dict(target=2, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-23 12:00:00")
        log = Log()
        rows = pipeline.collect(source, log, state, lambda rows: None)
        self.assertEqual([row["item"] for row in rows], ["1", "3"])
        self.assertEqual(state["counts"]["ambiguous"], 1)
        self.assertEqual(state["counts"]["excluded"], 1)
        decision = next(values for event, values in log.records if event == "classification" and values.get("item") == "1")
        self.assertTrue(decision["accepted"])
        self.assertTrue(decision["needs_review"])

    def test_pdf_only_missing_and_own_uri(self):
        source = Source([[target(1), target(2)]], {"v2": {"_name": "Wärmepumpentrockner, 9 kg"}})
        rows, _ = self.collect(source, 2)
        calls = [c for c in source.calls if c[0] == "pdf"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], dict(need_sku=True, need_capacity=False, need_loading=True))
        self.assertEqual(rows[1]["sku"], "PDF900")

    def test_end_to_end_artifacts_and_load_only_no_site(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(side_effect=lambda table, events, rows=None: dict(status="loaded" if rows else "preflight_ok", inserted=len(rows or []), duplicates=0))
            code, state = pipeline.execute(limit=2, output_root=root, client_factory=lambda e: Source([[target(1), target(2)]]), database=db)
            self.assertEqual(code, 0)
            rows, saved = storage.read_output(state["csv"])
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(not row["id"] for row in rows))
            self.assertEqual(saved["db_status"], "loaded")
            no_client = MagicMock(side_effect=AssertionError("NO_RECRAWL"))
            code2, loaded = pipeline.execute(load_only=state["csv"], client_factory=no_client, database=db)
            self.assertEqual(code2, 0)
            self.assertEqual(loaded["batch_id"], state["batch_id"])
            no_client.assert_not_called()

    def test_db_failure_keeps_loadable_csv(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(side_effect=[{}, config.DryError("db_failed:insert:Fake")])
            code, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: Source([[target(1)]]), database=db)
            self.assertEqual(code, 4)
            self.assertEqual(len(storage.read_output(state["csv"])[0]), 1)

    def test_collection_failure_preserves_checkpoint_but_cannot_load(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(return_value={})
            code, state = pipeline.execute(limit=2, output_root=root, client_factory=lambda e: Source([[target(1)], [target(2)]], failure=2), database=db)
            self.assertEqual(code, 3)
            self.assertEqual(state["output_rows"], 1)
            self.assertEqual(db.call_count, 1)
            with self.assertRaisesRegex(config.DryError, "output_not_completed"):
                storage.read_output(state["csv"])

    def test_merged_ratings_preserve_equal_count_columns_in_csv(self):
        cases = (
            ([rating_document()], ("4.3", "123", "rated")),
            ([rating_document(mean="0", count="0")], ("0.0", "0", "unrated")),
            ([rating_document(count="bad")], ("4.3", "", "failed")),
            ([rating_document(mean="bad")], ("", "123", "failed")),
            ([rating_document(mean="bad", count="0"), rating_document(count="bad")], ("4.3", "", "failed")),
            ([rating_document(count="bad"), rating_document(mean="bad")], ("4.3", "123", "failed")),
        )
        for bodies, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                source = Source([[target(1)]])
                log = Log()
                c = client.Client(log)
                source.rating = c.rating
                responses = [(body, {"status": 200}) for body in bodies]
                responses += [client.RequestFailure("timeout")] * (3 - len(bodies))
                with patch.object(c, "fetch", side_effect=responses), patch.object(client.time, "sleep"):
                    code, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: source,
                                                   database=MagicMock(return_value={"status": "loaded"}))
                row = storage.read_output(state["csv"])[0][0]
                self.assertEqual(row["star_rating"], expected[0])
                self.assertEqual(row["count_of_reviews"], expected[1])
                self.assertEqual(row["count_of_star_ratings"], expected[1])
                self.assertEqual(log.records[-1][1]["rating_state"], expected[2])
                self.assertEqual(code, 2 if expected[2] == "failed" else 0)

    def test_separate_tile_failures_continue_and_preserve_run_statistics(self):
        source = Source([[target(i) for i in range(1, 16)]])
        def factory(events):
            real = client.Client(events)
            real.page, real.comparison, real.rating = source.page, source.comparison, source.rating
            real.fetch = tile_fetch({"v1", "v6", "v11"})
            return real
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()), patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3):
            code, state = pipeline.execute(limit=12, output_root=root, client_factory=factory,
                                           database=MagicMock(return_value={"status": "loaded"}))
            rows, saved = storage.read_output(state["csv"])
            self.assertEqual(code, 2)
            self.assertEqual(saved["collection_status"], "complete")
            self.assertEqual(len(rows), 12)
            self.assertEqual([int(r["main_rank"]) for r in rows], list(range(1, 13)))
            self.assertEqual(saved["counts"]["item_failures"], 3)
            self.assertEqual(saved["counts"]["tile_http_400_failures"], 3)

    def test_tile_abort_preserves_csv_state_and_cumulative_error_count(self):
        source = Source([[target(1)], [target(i) for i in range(2, 7)]])
        def factory(events):
            real = client.Client(events)
            real.page, real.comparison, real.rating = source.page, source.comparison, source.rating
            real.fetch = tile_fetch({"v2", "v4", "v6"})
            return real
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()), patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3):
            db = MagicMock(return_value={})
            code, state = pipeline.execute(limit=100, output_root=root, client_factory=factory, database=db)
            self.assertEqual(code, 3)
            self.assertEqual(state["failure"], "systemic_tile_rejection")
            saved = json.loads(Path(state["csv"]).with_name("state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["collection_status"], "failed")
            self.assertEqual(saved["counts"]["tile_http_400_failures"], 3)
            self.assertNotIn("end_reason", saved)
            with Path(state["csv"]).open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual([row["item"] for row in csv.DictReader(handle)], ["1"])
            self.assertEqual(db.call_count, 1)  # preflight only, no partial insert
            with self.assertRaisesRegex(config.DryError, "output_not_completed"):
                storage.read_output(state["csv"])

    def test_changed_csv_rejected(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            _, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: Source([[target(1)]]), database=MagicMock(return_value={"status": "loaded"}))
            with Path(state["csv"]).open("a", encoding="utf-8") as handle:
                handle.write("changed")
            with self.assertRaisesRegex(config.DryError, "output_changed"):
                storage.read_output(state["csv"])

    def test_db_preflight_blocks_network_on_schema_failure(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            factory = MagicMock()
            code, state = pipeline.execute(output_root=root, client_factory=factory,
                                           database=MagicMock(side_effect=config.DryError("table_requires_nullable:star_rating")))
            self.assertEqual(code, 4)
            self.assertEqual(state["collection_status"], "not_started")
            factory.assert_not_called()

    def test_shortfall_is_saved_loaded_and_reported(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(return_value={"status": "loaded", "inserted": 1, "duplicates": 0})
            code, state = pipeline.execute(limit=100, output_root=root, client_factory=lambda e: Source([[target(1)]]), database=db)
            self.assertEqual(code, 2)
            self.assertEqual(state["shortfall_reason"]["missing"], 99)
            self.assertEqual(len(storage.read_output(state["csv"])[0]), 1)
            self.assertEqual(len(db.call_args.args[2]), 1)

    def test_independent_runs_do_not_mix(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            states = []
            for day in (23, 24):
                with patch.object(pipeline, "datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 10, day, 12, 34, 31)
                    states.append(pipeline.execute(limit=1, output_root=root, client_factory=lambda e: Source([[target(1)]]),
                                                   database=MagicMock(return_value={"status": "loaded"}))[1])
            self.assertNotEqual(states[0]["batch_id"], states[1]["batch_id"])
            self.assertNotEqual(states[0]["csv"], states[1]["csv"])

    def test_real_client_pipeline_only_required_endpoints(self):
        requested = []
        def factory(events):
            real = client.Client(events)
            def fetch(url, endpoint, **kwargs):
                requested.append((endpoint, url))
                if endpoint == "listing":
                    products = [{"id": "100", "bestVariationId": "v100", "variationPath": "/p/dryer-100/", "name": "Wärmepumpentrockner DR900, 9 kg"}]
                    return {"intents": [{"intent": "ranked", "products": products, "count": 1}]}, {}
                if endpoint == "tiles":
                    return [{"variationId": "v100", "name": "Wärmepumpentrockner DR900, 9 kg", "price": {"retailPrice": "499,99"}}], {}
                if endpoint == "comparison":
                    return b'<div><span>Modellbezeichnung</span><div class="pcp_content__column-list"><div data-variation-id="v100">DR900</div></div></div>', {"status": 200}
                if endpoint == "rating":
                    return rating_document(), {"status": 200}
                raise AssertionError("UNEXPECTED_ENDPOINT")
            real.fetch = fetch
            return real
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(limit=1, output_root=root, client_factory=factory,
                                           database=MagicMock(return_value={"status": "loaded"}))
            self.assertEqual(code, 0)
            self.assertEqual([e for e, _ in requested], ["listing", "tiles", "comparison", "rating"])
            row = storage.read_output(state["csv"])[0][0]
            self.assertEqual(row["capacity"], "9 kg")
            self.assertEqual(row["loading_type"], "")


def schema_rows():
    return [(c, "bigint" if c == "id" else "text", "YES", "nextval('test_seq')" if c == "id" else None,
             "NO", None, "NEVER") for c in config.COLUMNS]


class FakeConnection:
    def __init__(self, records=None, fail_insert=False, schema=None):
        self.records = records if records is not None else []
        self.pending, self.fail_insert = [], fail_insert
        self.schema, self.statements, self.result = schema or schema_rows(), [], []
        self.cursor_object = self

    def set_session(self, **kwargs):
        pass

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.records.extend(self.pending)
        self.pending = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "information_schema" in sql:
            self.result = self.schema
        elif sql.startswith("SELECT") and "WHERE batch_id" in sql:
            self.result = self.records
        else:
            self.result = []

    def fetchall(self):
        return self.result

    def executemany(self, sql, values):
        self.statements.append((sql, values))
        self.pending.extend(values)
        if self.fail_insert:
            raise RuntimeError("DO_NOT_EXPOSE_DB_VALUE")

    def close(self):
        pass


class DatabaseTests(Offline):
    def row(self):
        row = dict.fromkeys(config.COLUMNS)
        row.update(country="SEG", account_name="OTTO", item="100", retailer_sku_name="Trockner DR900",
                   main_rank=1, bsr_rank=1, calendar_week="w43", crawl_datetime="2026-10-23 12:00:00",
                   batch_id="batch", product_url="https://www.otto.de/p/dryer-100/")
        return row

    def test_preflight_insert_id_excluded_and_repeat_is_noop(self):
        conn = FakeConnection()
        self.assertEqual(storage.database(config.DEFAULT_TABLE, Log(), connector=lambda: conn)["status"], "preflight_ok")
        result = storage.database(config.DEFAULT_TABLE, Log(), [self.row()], connector=lambda: conn)
        self.assertEqual(result["inserted"], 1)
        inserted_sql = next(s for s, _ in conn.statements if s.startswith("INSERT"))
        self.assertNotIn('"id"', inserted_sql)
        repeat = storage.database(config.DEFAULT_TABLE, Log(), [self.row()], connector=lambda: conn)
        self.assertEqual(repeat, dict(status="already_loaded", inserted=0, duplicates=1))
        self.assertEqual(len(conn.records), 1)
        self.assertFalse(any(any(word in sql for word in ("CREATE", "ALTER", "DELETE", "UPDATE")) for sql, _ in conn.statements))

    def test_wrong_schema_and_not_autogenerated_id(self):
        for wrong in (schema_rows()[1:], [(c, t, null, None, ident, length, gen) for c, t, null, default, ident, length, gen in schema_rows()],
                      [(c, t, "NO" if c == "star_rating" else null, default, ident, length, gen) for c, t, null, default, ident, length, gen in schema_rows()],
                      [(c, "numeric" if c == "capacity" else t, null, default, ident, length, gen) for c, t, null, default, ident, length, gen in schema_rows()]):
            with self.assertRaises(config.DryError):
                storage.database(config.DEFAULT_TABLE, Log(), connector=lambda: FakeConnection(schema=wrong))

    def test_failed_insert_rolls_back_and_safe_error(self):
        conn = FakeConnection(fail_insert=True)
        with self.assertRaisesRegex(config.DryError, "db_failed:insert:RuntimeError") as raised:
            storage.database(config.DEFAULT_TABLE, Log(), [self.row()], connector=lambda: conn)
        self.assertEqual(conn.records, [])
        self.assertNotIn("DO_NOT_EXPOSE", str(raised.exception))

    def test_old_batch_content_conflict_rejected(self):
        conn = FakeConnection()
        storage.database(config.DEFAULT_TABLE, Log(), [self.row()], connector=lambda: conn)
        changed = dict(self.row(), sku="CHANGED900")
        with self.assertRaisesRegex(config.DryError, "db_batch_partial_or_content_conflict"):
            storage.database(config.DEFAULT_TABLE, Log(), [changed], connector=lambda: conn)

    def test_numeric_price_null_and_capacity_contract(self):
        meta = dict(type="numeric", nullable=True, max_length=None)
        self.assertEqual(storage.typed("1.299,99 €", "final_sku_price", meta), Decimal("1299.99"))
        self.assertEqual(storage.typed("-25%", "savings", meta), Decimal("-25"))
        self.assertIsNone(storage.typed("", "star_rating", meta))
        with self.assertRaises(config.DryError):
            storage.typed("NaN", "star_rating", meta)

    def test_float_zero_matches_same_csv_on_repeat(self):
        schema = [(c, "double precision" if c == "star_rating" else t, null, default, ident, length, gen)
                  for c, t, null, default, ident, length, gen in schema_rows()]
        conn = FakeConnection(schema=schema)
        row = dict(self.row(), star_rating="0.0")
        storage.database(config.DEFAULT_TABLE, Log(), [row], connector=lambda: conn)
        index = config.COLUMNS[1:].index("star_rating")
        values = list(conn.records[0])
        values[index] = 0.0
        conn.records[0] = tuple(values)
        repeat = storage.database(config.DEFAULT_TABLE, Log(), [row], connector=lambda: conn)
        self.assertEqual(repeat["duplicates"], 1)



class RevisionTests(Offline):
    def test_positive_savings_and_nulls(self):
        for raw, expected in (("-43%", "43%"), ("−12,5 %", "12,5%"), ("43%", "43%"),
                              (None, None), ("", None), ("bad", None), ("101%", None), ("0%", "0%")):
            with self.subTest(raw=raw):
                self.assertEqual(client.minimal_tile({"saleTags": {"discount": raw}})["savings"], expected)

    def test_confirmed_dryers_survive_conflicting_description(self):
        for name, description in (
            ("Hanseatic Wärmepumpentrockner HWT8EGT", "Filter und Schlauch für einfache Reinigung"),
            ("Hanseatic Wärmepumpentrockner HWT8DGT", "Ersatzfilter und Zubehör"),
            ("Sharp Wärmepumpentrockner KD-NHH9S8GW3-DE", "Waschtrockner sind nicht dasselbe"),
            ("LG Wärmepumpentrockner Vivace RT90V9W", "Filter einfach reinigen"),
        ):
            with self.subTest(name=name):
                # Original excluded sentences were not captured. These reproduce the
                # documented comparison:accessory/washer_dryer failure paths, not quotations.
                accepted, reason = fields.classify(name, {"_name": description})
                self.assertTrue(accepted)
                self.assertTrue(reason.startswith("ambiguous"))
                evidence = fields.classification_evidence(name, {"_name": description})
                self.assertEqual(evidence[-1]["raw"], description)

    def test_certain_non_dryers_and_conflicting_listing_titles(self):
        for name in (
            "DYSON Multihaarstyler Airwrap i.d. Multi-Haarstyler und - trockner",
            "Furnicopia Dörrautomat Edelstahl Lebensmitteltrockner – 10 Tabletts",
            "TREND WERK Waschmaschinenumbauschrank 66×64×184 cm für Waschmaschine & Trockner",
            "Raumhirsch Furniture Waschmaschinenumbauschrank für zwei Geräte",
            "ATEROLL Luftentfeuchter Elektrisch 20L",
        ):
            self.assertFalse(fields.classify(name)[0], name)
        self.assertEqual(fields.classify("Filter für Trockner", selection_name="Wärmepumpentrockner DR900"),
                         (True, "ambiguous_included_conflicting_product_types"))

    def test_semantic_comparison_title_ignores_long_feature_and_wrong_option(self):
        from bs4 import BeautifulSoup
        from common import compare
        feature = "Kombinierter Filtermechanismus schützt den Kondensator. " * 12
        html = (f'<div data-variation-id="v1"><a href="/p/dryer-1/?variationId=v1">'
                '<h3>BEKO Wärmepumpentrockner BMO3T382CW</h3><span class="product-subtitle">8 kg, Frontlader</span></a>'
                f'<p>{feature}</p></div><div data-variation-id="v2"><a href="/p/dryer-2/?variationId=v2">'
                '<h3>Trockner TWO123, 7 kg</h3></a></div>'
                '<div data-variation-id="v1"><a href="/p/other/?variationId=v2">WRONG MODEL 99 kg</a></div>')
        soup = BeautifulSoup(html, "lxml")
        self.assertEqual(compare._names_from_html(soup, {"v1"})["v1"], fields.text(feature))
        c = client.Client(Log())
        with patch.object(c, "fetch", return_value=(html.encode(), {"status": 200})) as fetch:
            values = c.comparison(["v1", "v2"])
            c.comparison(["v1"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(values["v1"]["_name"], "BEKO Wärmepumpentrockner BMO3T382CW 8 kg, Frontlader")
        selected = fields.choose(target(1, "BEKO Wärmepumpentrockner BMO3T382CW, 7 kg"), values["v1"])
        self.assertEqual(selected["capacity"], "8 kg")
        self.assertEqual(selected["loading_type"], "Front loader")
        self.assertNotIn(feature.strip(), str(values))

    def test_comparison_title_does_not_cross_fragment_option_or_external_product(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("""<div data-variation-id="v1">
          <a href="/p/other/#variationId=v2">Wrong option DR999, 99 kg</a>
          <a href="https://external.example/p/dryer/">External DR999, 99 kg</a>
          <a href="/p/dryer/"><p>Long feature paragraph that is not a product title.</p></a>
          <a href="/p/dryer/#variationId=v1"><h3><span class="product-title">Trockner DR900</span></h3></a>
        </div>""", "lxml")
        self.assertEqual(client.comparison_names(soup, {"v1"}), {"v1": "Trockner DR900"})

    def test_comparison_without_semantic_title_leaves_name_empty(self):
        body = b'<div data-variation-id="v1"><p>Filter explanation is much longer than a product title.</p></div>'
        c = client.Client(Log())
        with patch.object(c, "fetch", return_value=(body, {"status": 200})):
            value = c.comparison(["v1"])["v1"]
        self.assertNotIn("_name", value)
        self.assertEqual(value["_title_reason"], "comparison_product_title_unavailable")

    def test_model_fallback_series_split_and_preservation(self):
        for name, model in (
            ("Haier Wärmepumpentrockner G5 Serie HD80-C657U1", "HD80-C657U1"),
            ("BOSCH Wärmepumpentrockner Serie 6 WQG235D00", "WQG235D00"),
            ("BAUKNECHT Wärmepumpentrockner BT 97A WB", "BT 97A WB"),
            ("BAUKNECHT Wärmepumpentrockner BTSV 87A WB", "BTSV 87A WB"),
            ("Brand Trockner HGDS 8N1B-S", "HGDS 8N1B-S"),
            ("Brand Trockner PT 840 DW", "PT 840 DW"),
        ):
            with self.subTest(name=name):
                self.assertEqual(fields.choose(target(1, name), {})["sku"], model)
                self.assertEqual(fields.choose(target(1, name), {"Modellbezeichnung": "VALID123"}, {"sku": "PDF123"})["sku"], "VALID123")
                self.assertEqual(fields.choose(target(1, name), {}, {"sku": "PDF123"})["sku"], "PDF123")
        for source in ({"Modellbezeichnung": "TSA523WP 8kg Active"},):
            self.assertEqual(fields.choose(target(1), source)["sku"], "TSA523WP 8kg Active")
        self.assertIsNone(fields.model_from_name("Trockner G5 Serie"))
        self.assertIsNone(fields.model_from_name("Trockner DR900 DR901"))

    def pdf_pages(self, texts, **needed):
        pages = [MagicMock() for _ in texts]
        for page, content in zip(pages, texts):
            page.extract_text.return_value = content
        document = MagicMock()
        document.__enter__.return_value.pages = pages
        with patch.dict(sys.modules, {"pdfplumber": types.SimpleNamespace(open=MagicMock(return_value=document))}):
            result = client.parse_pdf(b"%PDF", **needed)
        return result, pages

    def test_pdf_loading_does_not_stop_after_model_capacity(self):
        result, pages = self.pdf_pages([
            "Modellkennung: KD-NHA7S6GW1D-DE\nNennkapazität (kg) 7",
            "Ziehen Sie die Tür nach vorne und legen Sie die Wäsche ein.",
            "Wäschetrockner - Frontlader", "must not be read"],
            need_sku=True, need_capacity=True, need_loading=True)
        self.assertEqual(result["loading_type"], "Front loader")
        self.assertEqual(result["sku"], "KD-NHA7S6GW1D-DE")
        self.assertEqual(result["capacity"], "7 kg")
        pages[2].extract_text.assert_called_once()
        pages[3].extract_text.assert_not_called()
        self.assertEqual(fields.choose(target(1), {"Beladung": "Toplader"}, result)["loading_type"], "Top-loading")

    def test_pdf_explicit_only_and_page_limit(self):
        for content in ("Tür nach vorne ziehen", "freistehend Wärmepumpe", "Frontlader und Toplader", "",
                        "Für Waschmaschine Frontlader geeignet", "Bauart: kein Frontlader", "Zubehör für Frontlader"):
            result, pages = self.pdf_pages([content] * 3 + ["Frontlader"], need_sku=False, need_capacity=False, need_loading=True)
            self.assertFalse(result.get("loading_type"))
            self.assertIn("loading_type", result["_missing"])
            pages[3].extract_text.assert_not_called()

    def test_pdf_missing_only_loading_skips_model_and_capacity_extraction(self):
        with patch.object(client.datasheet, "_sku") as sku, patch.object(client, "capacity") as cap:
            result, pages = self.pdf_pages(["Bauart: Frontlader", "must not be read"],
                                           need_sku=False, need_capacity=False, need_loading=True)
        self.assertEqual(result["loading_type"], "Front loader")
        sku.assert_not_called()
        cap.assert_not_called()
        pages[1].extract_text.assert_not_called()

    def test_unconfirmed_pdf_raw_value_is_not_reinterpreted(self):
        # A diagnostic excerpt is not a substitute for the validated PDF result.
        self.assertIsNone(fields.choose(target(1), {}, {"loading_type_raw": "Frontlader", "loading_type": None})["loading_type"])

    def test_real_pdf_adapter_fetches_once_for_all_requested_fields(self):
        c = client.Client(Log())
        parsed = {"sku": "PDF900", "loading_type": "Front loader", "loading_type_raw": "Frontlader"}
        with patch.object(c, "fetch", return_value=(b"%PDF-1.7 mock", {})) as fetch, \
                patch.object(client, "parse_pdf", return_value=parsed) as parse:
            self.assertEqual(c.pdf("https://d.otto.de/files/own.pdf", need_sku=True, need_capacity=False, need_loading=True), parsed)
        fetch.assert_called_once_with("https://d.otto.de/files/own.pdf", "product_pdf", kind="pdf")
        parse.assert_called_once_with(b"%PDF-1.7 mock", need_sku=True, need_capacity=False, need_loading=True)

    def test_pdf_request_and_parse_failures_are_distinct(self):
        c = client.Client(Log())
        with patch.object(c, "fetch", side_effect=client.RequestFailure("request_failed:product_pdf:TimeoutError")):
            self.assertEqual(c.pdf("https://d.otto.de/own.pdf", need_sku=False, need_capacity=False, need_loading=True)["_error_kind"], "request")
        with patch.object(c, "fetch", return_value=(b"%PDF-1.7", {})), patch.object(client, "parse_pdf", side_effect=ValueError("DO_NOT_LOG_RAW")):
            result = c.pdf("https://d.otto.de/own.pdf", need_sku=False, need_capacity=False, need_loading=True)
        self.assertEqual(result["_error_kind"], "extraction")
        self.assertNotIn("DO_NOT_LOG_RAW", str(result))

    def run_source(self, source, limit=1):
        context = tempfile.TemporaryDirectory()
        self.addCleanup(context.cleanup)
        with patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(limit=limit, output_root=context.name, client_factory=lambda e: source,
                                           database=MagicMock(return_value={"status": "loaded", "inserted": limit, "duplicates": 0}))
        records = [json.loads(line) for line in Path(state["diagnostics"]).read_text(encoding="utf-8").splitlines()]
        rows = storage.read_output(state["csv"])[0]
        return code, state, records, rows

    def test_pdf_one_request_for_all_missing_fields_and_no_extra_endpoints(self):
        source = Source([[target(1)]], {"v1": {}})
        source.pdf = MagicMock(return_value={"sku": "PDF900", "capacity": "9 kg", "loading_type_raw": "Frontlader", "loading_type": "Front loader"})
        code, state, _, rows = self.run_source(source)
        self.assertEqual(code, 0)
        source.pdf.assert_called_once_with(None, need_sku=True, need_capacity=False, need_loading=True)
        self.assertEqual(rows[0]["loading_type"], "Front loader")
        self.assertEqual(state["counts"]["supplemented_items"], 1)

    def test_missing_info_is_not_lost_row_or_request_failure(self):
        source = Source([[target(1, "BOSCH Wärmepumpentrockner Serie 6 WQG235D00")]],
                        {"v1": {"_unavailable": "comparison_option_missing"}})
        source.tiles = MagicMock(return_value={"v1": {"final_sku_price": "499 €", "top_infos": {"Kapazität Trocknen": "8 kg"}}})
        source.pdf = MagicMock(return_value={"sku": "WQG235D00", "_unavailable": "pdf_label_unconfirmed_in_first_3_pages", "_missing": ["loading_type"]})
        code, state, events, rows = self.run_source(source)
        self.assertEqual(code, 0)
        self.assertEqual(state["counts"]["item_failures"], 0)
        self.assertEqual(state["counts"]["incomplete_items"], 1)
        self.assertEqual(state["counts"]["supplemented_items"], 1)
        self.assertEqual((rows[0]["sku"], rows[0]["capacity"], rows[0]["loading_type"]), ("WQG235D00", "8 kg", ""))
        outcome = next(e for e in events if e["event"] == "product_result")
        self.assertTrue(outcome["row_saved"])
        self.assertEqual(outcome["status"], "supplemented")

    def test_request_error_is_preserved_after_other_sources_recover(self):
        source = Source([[target(1)]], {"v1": {"_error": "request_failed:comparison:TimeoutError", "_error_kind": "request"}})
        code, state, events, rows = self.run_source(source)
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]["sku"], "PDF900")
        self.assertEqual(state["counts"]["item_failures"], 0)
        self.assertEqual(state["counts"]["request_error_items"], 1)
        self.assertTrue(next(e for e in events if e["event"] == "product_result")["row_saved"])

    def test_final_price_only_and_missing_savings_diagnostic(self):
        for prices, expected in (({"final_sku_price": "400 €"}, 0),
                                 ({"final_sku_price": "400 €", "original_sku_price": "600 €"}, 1)):
            source = Source([[target(1)]])
            source.tiles = MagicMock(return_value={"v1": prices})
            code, state, events, rows = self.run_source(source)
            self.assertEqual(state["counts"]["price_warning_items"], expected)
            self.assertEqual(code, 2 if expected else 0)
            self.assertEqual(rows[0]["savings"], "")

    def test_batch_format_and_same_second_file_protection(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()), patch.object(pipeline, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 23, 12, 34, 31)
            db = MagicMock(return_value={"status": "loaded"})
            _, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: Source([[target(1)]]), database=db)
            self.assertEqual(state["batch_id"], "o_dry_20261023_123431")
            before = Path(state["csv"]).read_bytes()
            with self.assertRaises(FileExistsError):
                pipeline.execute(limit=1, output_root=root, database=db)
            self.assertEqual(Path(state["csv"]).read_bytes(), before)
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(rows[0]["batch_id"], state["batch_id"])
            for file in ("run.log", "diagnostics.jsonl"):
                events = [json.loads(line) for line in Path(state["csv"]).with_name(file).read_text(encoding="utf-8").splitlines()]
                self.assertTrue(all(e["batch_id"] == state["batch_id"] for e in events))

    def test_legacy_csv_reload_keeps_original_batch(self):
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = FakeConnection()
            database = lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db)
            legacy = "o_dry_20261023_123431_2caccf63"
            row = DatabaseTests().row()
            row["batch_id"] = legacy
            state = dict(batch_id=legacy, target=1, collection_status="complete")
            storage.checkpoint(Path(root), [row], state)
            factory = MagicMock(side_effect=AssertionError("NO_RECRAWL"))
            for _ in range(2):
                code, result = pipeline.execute(load_only=Path(root) / "otto_dry.csv", client_factory=factory, database=database)
                self.assertEqual(code, 0)
                self.assertEqual(result["batch_id"], legacy)
            self.assertEqual(result["database"]["duplicates"], 1)
            self.assertEqual(result["database"]["inserted"], 0)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
