"""Offline DRY behavior tests. No real browser, API, credentials or DB access."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from html import escape
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dry import config, database, run
from dry.detail import CollectionError, DetailCollector, parse_ratings, parse_specs
from dry.output import canonical_product_url, merge_rows, positive_savings, run_metadata, write_output
from dry.policy import exclusion_reason


def page_html(products=(), *, ads=(), shown=0, total=0, more=False):
    apollo = {"page": {"__typename": "ProductListPage", "products": []}}
    cards = []
    for pid, name in products:
        apollo["page"]["products"].append({"productId": pid})
        apollo[pid] = {"__typename": "GraphqlProduct", "id": pid, "title": name,
                       "url": f"/de/product/_test-{pid}.html"}
        cards.append(f'<li><article><span>Gesponsert</span><a href="/de/product/_test-{pid}.html">{escape(name)}</a></article></li>')
    for pid, name in ads:
        cards.insert(0, f'<li data-test="mms-sba-product-tile"><span>Gesponsert</span><a href="/de/product/_test-{pid}.html">{escape(name)}</a></li>')
    return (f'<script>window.__PRELOADED_STATE__ = {json.dumps({"apolloState": apollo})};</script>'
            '<div id="mms-search-productlist"><ul role="list">' + ''.join(cards) + '</ul>'
            + (f'<div>{shown} von {total}</div>' if shown is not None else '')
            + ('<button>12 weitere Produkte anzeigen</button>' if more else '') + '</div>')


def specs(pid, *, product_type="Wärmepumpentrockner", name="DRYER MODEL123 Trockner", extra=None):
    features = {"Produkttyp": product_type, "Modelkennung": "REALMODEL456", "Beladung": "Frontlader",
                "Nennkapazität": "8 kg", **(extra or {})}
    return {"data": {"comparisonTableRecommendations": {"tableData": {"products": [
        {"productAggregate": {"productId": pid, "product": {"id": pid, "title": name,
         "featureGroupsWithProductId": {"featureGroups": [{"features": [
             {"name": key, "value": value} for key, value in features.items()]}]}}}}]}}}}


def ratings(count=7, counts=(0, 0, 1, 2, 9)):
    return {"data": {"reviews": {"totalResults": count, "rating": {
        "distribution": [{"value": i, "count": n} for i, n in enumerate(counts, 1)]},
        "reviews": [{"text": "UNUSED_REVIEW_BODY"}]}}}


class Browser:
    def __init__(self, pages, detail_types=None):
        self.pages = pages
        self.detail_types = detail_types or {}
        self.driver = Mock(page_source="")
        self.calls = []

    def navigate(self, url, **kwargs):
        params = parse_qs(urlparse(url).query)
        key = ("bsr" if "sort" in params else "main", int(params.get("page", [1])[0]))
        self.driver.page_source = self.pages[key]
        return {"html": self.driver.page_source, "blocked": False, "error": None}

    def _blocked(self, html):
        return "BLOCKED" in html

    def _click_consent(self):
        return None

    def gql(self, operation, variables):
        self.calls.append((operation, variables))
        pid = variables.get("ref") or variables.get("id")
        data = (specs(pid, product_type=self.detail_types.get(pid, "Wärmepumpentrockner"))
                if operation == "GetComparisonTableRecommendations" else ratings())
        return {"status": 200, "data": data}


class DryPolicyTests(unittest.TestCase):
    def test_heat_pump_spelling_variants_from_title_and_api_type(self):
        for label in ("Wärmepumpentrockner", "Wärmepumpetrockner", "WÄRMEPUMPETROCKNER"):
            for source in ("listing", "api_type", "api_name"):
                with self.subTest(label=label, source=source):
                    row = {"retailer_sku_name": label if source == "listing" else "MODEL123"}
                    features = {"Produkttyp": label} if source == "api_type" else {}
                    detail_name = label if source == "api_name" else None
                    self.assertIsNone(exclusion_reason(row, features=features, detail_name=detail_name))

    def test_spelling_variant_preserves_exclusions_and_standard_sponsored_cards(self):
        cases = (
            ("Waschmaschine + Wärmepumpetrockner", "standard", "washing_machine"),
            ("Waschtrockner", "standard", "washer_dryer"),
            ("Wärmepumpetrockner Set", "standard", "set_or_combo"),
            ("Zubehör-Set für Wärmepumpetrockner", "standard", "set_or_combo"),
            ("Ersatzfilter für Wärmepumpetrockner", "standard", "accessory"),
            ("Wärmepumpetrockner", "sponsored-ad", "advertisement"),
            ("Wärmepumpetrockner", "standard", None),
        )
        for name, card_type, expected in cases:
            with self.subTest(name=name, card_type=card_type):
                row = {"retailer_sku_name": name, "listing_card_type": card_type, "sku_status": "Sponsored"}
                self.assertEqual(expected, exclusion_reason(row, features={"Produkttyp": "Wärmepumpetrockner"}))

    def test_titles_and_actual_target_types(self):
        include = ["Wärmepumpentrockner", "Kondenstrockner", "Ablufttrockner", "Wäschetrockner", "Trockner"]
        exclude = ["Waschmaschine", "Waschtrockner", "Wasch-Trockner", "Waschmaschine + Trockner",
                   "Trockner Bundle", "Waschturm", "Wash Tower", "Trockner Set", "Trockner Combo",
                   "Hair dryer", "Haartrockner", "Wäscheständer", "Zwischenbaurahmen für Trockner"]
        for name in include:
            with self.subTest(name=name):
                self.assertIsNone(exclusion_reason({"retailer_sku_name": name}))
        for name in exclude:
            with self.subTest(name=name):
                self.assertIsNotNone(exclusion_reason({"retailer_sku_name": name}))
        self.assertEqual("washer_dryer", exclusion_reason({"retailer_sku_name": "Trockner"}, features={"Produkttyp": "Waschtrockner"}))
        self.assertIsNone(exclusion_reason({"retailer_sku_name": "MODEL123"}, features={"Produkttyp": "Wärmepumpentrockner"}))
        self.assertEqual("unconfirmed_product_type", exclusion_reason({"retailer_sku_name": "MODEL123"}))

    def test_banner_occurrence_not_global_id(self):
        row = {"sku_id": "1", "retailer_sku_name": "Trockner", "sku_status": "Sponsored"}
        self.assertEqual("advertisement", exclusion_reason(dict(row, listing_card_type="sponsored-ad")))
        self.assertIsNone(exclusion_reason(dict(row, listing_card_type="standard")))

    def test_feature_terms_do_not_prove_washer_dryer(self):
        for name in ("BEKO MODEL123 Wärmepumpentrockner mit 2-in-1-Filter",
                     "BEKO MODEL123 Wärmepumpentrockner 2-in-1-Filter",
                     "Wäschetrockner mit All-in-One Filter"):
            with self.subTest(name=name):
                row = {"retailer_sku_name": name}
                self.assertIsNone(exclusion_reason(row, require_evidence=False))
                self.assertIsNone(exclusion_reason(row, features={"Produkttyp": "Wärmepumpentrockner"}))

    def test_included_accessories_are_not_appliance_bundles(self):
        for name in ("Wärmepumpentrockner mit Zubehör-Set",
                     "Wärmepumpentrockner inkl. Zubehör-Set",
                     "Wärmepumpentrockner inklusive Anschluss-Set",
                     "Wärmepumpentrockner with accessory set",
                     "Wärmepumpentrockner + Zubehör-Set",
                     "Wärmepumpentrockner mit Zubehör-Set für Trockner",
                     "Wärmepumpentrockner mit Verbindungssatz"):
            with self.subTest(name=name):
                row = {"retailer_sku_name": name}
                self.assertIsNone(exclusion_reason(row, require_evidence=False))
                self.assertIsNone(exclusion_reason(row, features={"Produkttyp": "Wärmepumpentrockner"}))

    def test_explicit_washer_bundle_and_combination_override_dryer_type(self):
        for name in ("Waschmaschine + Trockner mit Zubehör-Set",
                     "Wärmepumpentrockner mit Waschmaschine im Set",
                     "Waschtrockner mit 2-in-1-Filter", "Wasch-Trocken-Säule mit Zubehör-Set",
                     "Wash Tower mit All-in-One Filter", "Trockner Bundle mit Zubehör",
                     "Trockner Set", "Trockner Combo", "Trockner mit Combo",
                     "Trockner mit Trockner Set", "Trockner mit Zubehör-Set und Trockner"):
            with self.subTest(name=name):
                row = {"retailer_sku_name": name}
                self.assertIsNotNone(exclusion_reason(row, require_evidence=False))
                self.assertIsNotNone(exclusion_reason(row, features={"Produkttyp": "Wärmepumpentrockner"}))

    def test_accessory_products_remain_excluded(self):
        for name in ("Zwischenbaurahmen für Trockner", "Zubehör-Set für Wärmepumpentrockner",
                     "Ersatzfilter für Trockner", "Verbindungssatz für Waschmaschine und Trockner"):
            with self.subTest(name=name):
                self.assertIsNotNone(exclusion_reason({"retailer_sku_name": name}))
        self.assertIsNotNone(exclusion_reason({"retailer_sku_name": "Trockner"}, features={"Produkttyp": "Zubehör"}))

    def test_ambiguous_two_in_one_waits_for_actual_type(self):
        for name in ("Trockner 2-in-1", "Trockner mit 2-in-1", "Trockner mit All-in-One Filter"):
            with self.subTest(name=name):
                row = {"retailer_sku_name": name}
                self.assertIsNone(exclusion_reason(row, require_evidence=False))
                self.assertEqual("unconfirmed_product_type", exclusion_reason(row))
                self.assertIsNone(exclusion_reason(row, features={"Produkttyp": "Wärmepumpentrockner"}))
                self.assertEqual("washer_dryer", exclusion_reason(row, features={"Produkttyp": "Waschtrockner"}))


class DryDetailTests(unittest.TestCase):
    def test_specs_select_exact_target_between_recommendations(self):
        payload = specs("1")
        products = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"]
        first = specs("99", name="OTHER_PRODUCT_MODEL Waschtrockner", product_type="Waschtrockner",
                      extra={"Modelkennung": "OTHER_PRODUCT_MODEL", "Beladung": "Toplader", "Nennkapazität": "99 kg"})
        last = specs("98", extra={"Modelkennung": "ANOTHER_MODEL", "Nennkapazität": "98 kg"})
        products.insert(0, first["data"]["comparisonTableRecommendations"]["tableData"]["products"][0])
        products.extend(last["data"]["comparisonTableRecommendations"]["tableData"]["products"])
        result = parse_specs(payload, "1")
        self.assertEqual(("REALMODEL456", "Front load", "8kg"),
                         (result["sku"], result["loading_type"], result["capacity"]))
        self.assertEqual("Wärmepumpentrockner", result["_features"]["Produkttyp"])
        self.assertNotIn("OTHER_PRODUCT_MODEL", repr(result))

    def test_specs_missing_target_never_use_recommendation(self):
        with self.assertRaisesRegex(CollectionError, "^DETAIL_TARGET_NOT_FOUND$"):
            parse_specs(specs("99"), "1")

    def test_specs_missing_target_features_never_borrow_recommendation(self):
        payload = specs("1")
        products = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"]
        products[0]["productAggregate"]["product"]["featureGroupsWithProductId"]["featureGroups"] = []
        products.extend(specs("99")["data"]["comparisonTableRecommendations"]["tableData"]["products"])
        with self.assertRaisesRegex(CollectionError, "^DETAIL_FEATURES_MISSING$"):
            parse_specs(payload, "1")

    def test_dryer_labels_and_no_wash_capacity_fallback(self):
        result = parse_specs(specs("1", extra={"Füllmenge Baumwolle (Waschen)": "99 kg",
                                                  "Füllmenge Baumwolle (Trocknen)": "7,5 kg"}), "1")
        self.assertEqual(("REALMODEL456", "Front load", "7,5kg"), (result["sku"], result["loading_type"], result["capacity"]))
        result = parse_specs(specs("1", extra={"Nennkapazität": None, "Beladung": "Other", "Füllmenge Baumwolle (Waschen)": "99 kg"}), "1")
        self.assertIsNone(result["capacity"])
        self.assertIsNone(result["loading_type"])
        with self.assertRaises(CollectionError):
            parse_specs(specs("99"), "1")

    def test_distinct_review_count_distribution_and_missing_values(self):
        result = parse_ratings(ratings())
        self.assertEqual({"count_of_reviews": 7, "count_of_star_ratings": 12, "star_rating": 4.7}, result)
        self.assertEqual({"count_of_reviews": None, "count_of_star_ratings": None, "star_rating": None},
                         parse_ratings({"data": {"reviews": {"totalResults": None, "rating": None}}}))
        self.assertEqual(0, parse_ratings(ratings(0, (0, 0, 0, 0, 0)))["count_of_star_ratings"])
        with self.assertRaises(CollectionError):
            parse_ratings({"data": {}})

    def test_only_two_necessary_calls_once_per_accepted_product(self):
        browser = Browser({})
        collector = DetailCollector(browser, sleep_s=0)
        collector.get("1", {"retailer_sku_name": "Trockner"})
        collector.get("1", {"retailer_sku_name": "Trockner"})
        self.assertEqual(["GetComparisonTableRecommendations", "GetProductReviews"], [op for op, _ in browser.calls])
        self.assertEqual(0, browser.calls[0][1]["limit"])
        self.assertFalse(browser.calls[0][1]["cofrConfig"]["isEnabled"])
        self.assertEqual(1, browser.calls[1][1]["reviewPage"])
        self.assertNotIn("UNUSED_REVIEW_BODY", repr(collector.cache))
        browser = Browser({}, {"2": "Waschtrockner"})
        DetailCollector(browser, sleep_s=0).get("2", {"retailer_sku_name": "Trockner"})
        self.assertEqual(1, len(browser.calls))

    def test_http_graphql_and_rate_limit_fail_without_printing_raw_errors(self):
        for response in ({"status": 403, "raw": "RAW_RESPONSE_MUST_NOT_PRINT"},
                         {"status": 429}, {"status": 200, "data": {"errors": [{"message": "RAW_RESPONSE_MUST_NOT_PRINT"}]}},
                         {"status": 200, "data": None}):
            browser = Mock()
            browser.gql.return_value = response
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(CollectionError):
                DetailCollector(browser, retries=1, sleep_s=0).get("1")
            self.assertNotIn("RAW_RESPONSE_MUST_NOT_PRINT", output.getvalue())
            self.assertEqual(2, browser.gql.call_count)


class DryPipelineTests(unittest.TestCase):
    def args(self, root, **kwargs):
        args = run.parse_args(["--output-dir", str(root), "--render-settle", "0", "--sponsored-extra-wait", "0",
                               "--sleep", "0", "--page-retries", "0", "--captcha-wait", "0"])
        for key, value in kwargs.items():
            setattr(args, key, value)
        return args

    def test_spelling_variant_keeps_raw_name_sku_ranks_and_filtered_products(self):
        raw_name = "BRAND MODEL123 Wärmepumpetrockner (9,0 kg, Weiß)"
        browser = Browser({
            ("main", 1): page_html([("9", "Waschmaschine"), ("1", raw_name),
                                   ("8", "Wärmepumpetrockner Set"), ("2", "Trockner")], shown=4, total=4),
            ("bsr", 1): page_html([("1", raw_name)], shown=1, total=1),
        }, {"1": "Wärmepumpetrockner"})
        original_gql = browser.gql

        def gql(operation, variables):
            response = original_gql(operation, variables)
            if operation == "GetComparisonTableRecommendations" and variables.get("ref") == "1":
                response["data"] = specs("1", product_type="Wärmepumpetrockner", name=raw_name)
            return response

        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = run.crawl(self.args(root), browser)
            with (root / "mmkt_dry_output.csv").open(encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(config.COLUMNS, tuple(reader.fieldnames))
                rows = list(reader)
        self.assertTrue(manifest["success"])
        self.assertEqual((2, 1, 4), (manifest["main"]["rows"], manifest["bsr"]["rows"], manifest["api_calls"]))
        self.assertEqual([("1", "1", "1"), ("2", "2", "")],
                         [(row["item"], row["main_rank"], row["bsr_rank"]) for row in rows])
        self.assertEqual((raw_name, "REALMODEL456"), (rows[0]["retailer_sku_name"], rows[0]["sku"]))
        self.assertEqual({"1", "2"}, {variables.get("ref") or variables.get("id") for _, variables in browser.calls})

    def test_full_short_category_with_hidden_combo_dedup_ranks_union_and_shared_api_cache(self):
        pages = {("main", 1): page_html([("1", "Trockner"), ("9", "Waschmaschine"), ("2", "MODEL123")],
                                       ads=[("1", "Trockner")], shown=3, total=5, more=True),
                 ("main", 2): page_html([("1", "Trockner"), ("3", "Trockner")], shown=5, total=5),
                 ("bsr", 1): page_html([("3", "Trockner"), ("4", "Trockner")], shown=2, total=2)}
        browser = Browser(pages, {"2": "Waschtrockner"})
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = run.crawl(self.args(root), browser)
            self.assertTrue(manifest["success"])
            self.assertEqual("last_page", manifest["main"]["stop_reason"])
            self.assertEqual("last_page", manifest["bsr"]["stop_reason"])
            self.assertEqual(7, manifest["api_calls"])
            with (root / "mmkt_dry_output.csv").open(encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(config.COLUMNS, tuple(reader.fieldnames))
                rows = list(reader)
            self.assertEqual([("1", "1", ""), ("3", "2", "1"), ("4", "", "2")],
                             [(r["item"], r["main_rank"], r["bsr_rank"]) for r in rows])
            self.assertEqual(1, len({r["batch_id"] for r in rows}))
            self.assertEqual({"mmkt_dry_output.csv"}, {p.name for p in root.iterdir()})
            (root / "mmkt_dry_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(3, len(database.load_validated_output(root / "mmkt_dry_output.csv")))
            (root / "mmkt_dry_output.csv").write_text("changed", encoding="utf-8")
            with self.assertRaises(CollectionError):
                database.load_validated_output(root / "mmkt_dry_output.csv")

    def test_extra_recommendations_do_not_get_collected_or_ranked(self):
        browser = Browser({(sort, 1): page_html([("1", "Trockner")], shown=1, total=1)
                           for sort in ("main", "bsr")})
        original_gql = browser.gql
        def gql(operation, variables):
            response = original_gql(operation, variables)
            if operation == "GetComparisonTableRecommendations":
                products = response["data"]["data"]["comparisonTableRecommendations"]["tableData"]["products"]
                other = specs("99", name="OTHER_PRODUCT_MODEL Waschtrockner", product_type="Waschtrockner",
                              extra={"Modelkennung": "OTHER_PRODUCT_MODEL", "Nennkapazität": "99 kg"})
                products.insert(0, other["data"]["comparisonTableRecommendations"]["tableData"]["products"][0])
            return response
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = run.crawl(self.args(root, target=1, bsr_target=1), browser)
            with (root / "mmkt_dry_output.csv").open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        self.assertTrue(manifest["success"])
        self.assertEqual((1, 2, 1), (manifest["output_rows"], manifest["api_calls"], manifest["unique_detail_products"]))
        self.assertEqual([("1", "1", "1", "REALMODEL456", "8kg")],
                         [(row["item"], row["main_rank"], row["bsr_rank"], row["sku"], row["capacity"]) for row in rows])
        self.assertEqual({"1"}, {variables.get("ref") or variables.get("id") for _, variables in browser.calls})
        self.assertNotIn("OTHER_PRODUCT_MODEL", repr(rows))

    def test_keeps_paging_until_eligible_target(self):
        pages = {("main", 1): page_html([("9", "Waschmaschine")], shown=1, total=2, more=True),
                 ("main", 2): page_html([("1", "Trockner")], shown=2, total=2),
                 ("bsr", 1): page_html([("1", "Trockner")], shown=1, total=1)}
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            manifest = run.crawl(self.args(Path(directory), target=1, bsr_target=1), Browser(pages))
        self.assertEqual("target_reached", manifest["main"]["stop_reason"])
        self.assertEqual([1, 2], [p["page"] for p in manifest["pages"]["main"]])

    def test_feature_and_accessory_descriptions_reach_api_and_keep_correct_ranks(self):
        browser = Browser({
            ("main", 1): page_html([
                ("1", "Wärmepumpentrockner mit 2-in-1-Filter"),
                ("2", "Wärmepumpentrockner mit Zubehör-Set"),
                ("3", "Waschmaschine + Trockner mit Zubehör-Set")], shown=3, total=3),
            ("bsr", 1): page_html([("2", "Wärmepumpentrockner mit Zubehör-Set")], shown=1, total=1),
        })
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = run.crawl(self.args(root), browser)
            with (root / "mmkt_dry_output.csv").open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual((2, 1, 4), (manifest["main"]["rows"], manifest["bsr"]["rows"], manifest["api_calls"]))
        self.assertEqual([("1", "1", ""), ("2", "2", "1")],
                         [(row["item"], row["main_rank"], row["bsr_rank"]) for row in rows])
        self.assertNotIn("3", {variables.get("ref") for _, variables in browser.calls})

    def test_no_detail_calls_past_target_on_final_page(self):
        browser = Browser({(sort, 1): page_html([("1", "Trockner"), ("2", "Trockner")], shown=2, total=2)
                           for sort in ("main", "bsr")})
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            manifest = run.crawl(self.args(Path(directory), target=1, bsr_target=1), browser)
        self.assertEqual((1, 2), (manifest["output_rows"], manifest["api_calls"]))

    def test_unknown_type_is_failure_instead_of_short_category(self):
        browser = Browser({(sort, 1): page_html([("1", "MODEL123")], shown=1, total=1)
                           for sort in ("main", "bsr")}, {"1": "Unbekannt"})
        def gql(operation, variables):
            return {"status": 200, "data": specs("1", product_type="Unbekannt", name="MODEL123")}
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(CollectionError, "UNCONFIRMED_PRODUCT_TYPES"):
                run.crawl(self.args(Path(directory)), browser)

    def test_errors_and_repeated_pages_never_become_short_success(self):
        for html in ("BLOCKED", "<html>bad parser response</html>",
                     page_html(shown=2, total=2), page_html([("1", "Trockner")], shown=None)):
            pages = {("main", 1): html, ("main", 2): html}
            with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(CollectionError):
                    run.crawl(self.args(Path(directory)), Browser(pages))
                self.assertFalse((Path(directory) / "mmkt_dry_output.csv").exists())

    def test_empty_last_page_is_success_without_detail_calls(self):
        browser = Browser({(sort, 1): page_html() for sort in ("main", "bsr")})
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            result = run.crawl(self.args(Path(directory)), browser)
        self.assertEqual((0, 0), (result["output_rows"], result["api_calls"]))

    def test_default_targets_and_remote_switches(self):
        args = run.parse_args([])
        self.assertEqual((100, 100, "test", False), (args.target, args.bsr_target, args.table, args.write_db))
        self.assertFalse(args.collect_only)
        self.assertEqual(("dx_seg", "dx_seg_ldy_dryer_retail_test"), config.DB_TABLE)
        self.assertEqual(300, run.parse_args(["--target", "300"] ).target)

    def test_failed_browser_invalidates_old_success_without_connecting_database(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("common.uc.UcSession", side_effect=RuntimeError("RAW_ERROR_MUST_NOT_PRINT")), \
                patch.object(run, "inspect_or_insert", side_effect=AssertionError("DB forbidden")) as db, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory)
            (root / "mmkt_dry_manifest.json").write_text('{"success": true}', encoding="utf-8")
            self.assertEqual(1, run.main(["--output-dir", directory, "--log-dir", str(root / "logs")]))
            self.assertFalse(json.loads((root / "mmkt_dry_manifest.json").read_text())["success"])
            self.assertNotIn("RAW_ERROR_MUST_NOT_PRINT", output.getvalue())
            db.assert_not_called()


class DryOutputDatabaseTests(unittest.TestCase):
    def test_all_columns_positive_percent_and_no_invented_zero(self):
        meta = run_metadata(datetime(2026, 10, 21, 10, 20, 30))
        rows = merge_rows([{"sku_id": "1", "retailer_sku_name": "Trockner", "savings": "-10%",
                            "product_url": "https://www.mediamarkt.de/de/product/_test-1.html"}], [],
                          {"1": {}}, meta)
        self.assertEqual(config.COLUMNS, tuple(rows[0]))
        self.assertEqual("10%", rows[0]["savings"])
        self.assertEqual(("SEG", "Mediamarkt", "w43", "m_20261021_102030"),
                         tuple(rows[0][k] for k in ("country", "account_name", "calendar_week", "batch_id")))
        self.assertIsNone(rows[0]["id"])
        self.assertIsNone(rows[0]["count_of_reviews"])
        self.assertEqual("10%", positive_savings("10%"))
        self.assertIsNone(positive_savings("unknown"))

    def test_listing_rating_count_never_fills_review_count_and_main_prices_win(self):
        main = [{"sku_id": "1", "count_of_reviews": 20, "star_rating": 4.2, "final_sku_price": "399,– €",
                 "product_url": "https://www.mediamarkt.de/de/product/_test-1.html"}]
        bsr = [{"sku_id": "1", "count_of_reviews": 30, "final_sku_price": "499,– €"}]
        row = merge_rows(main, bsr, {"1": {}}, run_metadata())[0]
        self.assertEqual((None, 20, 4.2, "399,– €"), tuple(row[k] for k in ("count_of_reviews", "count_of_star_ratings", "star_rating", "final_sku_price")))
        zero = merge_rows(main, bsr, {"1": {"count_of_star_ratings": 0}}, run_metadata())[0]
        self.assertIsNone(zero["star_rating"])
        self.assertEqual(0, zero["count_of_star_ratings"])

    def test_output_urls_omit_tracking_and_reject_wrong_product_or_host(self):
        self.assertEqual("https://www.mediamarkt.de/de/product/_test-1.html",
                         canonical_product_url("https://www.mediamarkt.de/de/product/_test-1.html?tracking=example#reviews", "1"))
        for url in ("https://example.invalid/de/product/_test-1.html", "https://www.mediamarkt.de/de/product/_test-2.html"):
            with self.assertRaises(CollectionError):
                canonical_product_url(url, "1")

    def test_numeric_conversion_and_schema_fail_closed(self):
        metadata = {k: {"nullable": True, "default": False, "identity": False,
                        "type": "text", "max_length": None, "udt": "text"} for k in config.COLUMNS}
        with self.assertRaises(CollectionError):
            database.validate_schema(metadata)
        metadata["id"]["identity"] = True
        database.validate_schema(metadata)
        numeric = dict(metadata["capacity"], type="numeric")
        for column, raw, expected in [("savings", "10%", "10"),
                                       ("final_sku_price", "1.299,– €", "1299.00")]:
            self.assertEqual(expected, str(database.typed_value(raw, column, numeric)))
        with self.assertRaisesRegex(CollectionError, "DB_CAPACITY_REQUIRES_TEXT_COLUMN"):
            database.typed_value("7,5kg", "capacity", numeric)
        with self.assertRaises(CollectionError):
            database.typed_value("missing", "capacity", dict(numeric, nullable=False))
        with self.assertRaises(CollectionError):
            database.typed_value("1.5", "main_rank", dict(numeric, type="integer"))

    def test_database_metadata_readonly_and_insert_omits_id(self):
        meta = {k: {"nullable": True, "default": False, "identity": k == "id",
                    "type": "text", "max_length": None, "udt": "text"} for k in config.COLUMNS}
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        cursor = Mock()
        cursor.__enter__ = Mock(return_value=cursor)
        cursor.__exit__ = Mock(return_value=False)
        connection.cursor.return_value = cursor
        cursor.fetchall.return_value = []
        cursor.fetchmany.return_value = []
        with patch.object(database, "connect", return_value=connection), \
                patch.object(database, "table_metadata", return_value=meta):
            self.assertEqual(0, database.inspect_or_insert(config.DB_TABLE)["inserted"])
            connection.set_session.assert_called_with(readonly=True)
            cursor.executemany.assert_not_called()
            row = dict.fromkeys(config.COLUMNS)
            row.update(country="SEG", account_name="Mediamarkt", item="123", batch_id="m_20261022_100000")
            self.assertEqual(1, database.inspect_or_insert(config.DB_TABLE, [row])["inserted"])
            connection.set_session.assert_called_with(readonly=False)
            values = cursor.executemany.call_args.args[1]
            self.assertEqual((19, "123"), (len(values[0]), values[0][2]))
            meta["id"]["identity"] = False
            cursor.executemany.reset_mock()
            with self.assertRaises(CollectionError):
                database.inspect_or_insert(config.DB_TABLE, [row])
            cursor.executemany.assert_not_called()


if __name__ == "__main__":
    unittest.main()
