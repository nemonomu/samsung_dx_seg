"""Offline collection policy and pagination tests; no credentials or browser needed."""
from __future__ import annotations

import csv
import importlib
import io
import json
import sys
import tempfile
import unittest
from html import escape
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.listing_policy import filter_listing_rows, listing_exclusion_reason
from common.parsers import IS_BUNDLE, extract_listing_pagination, parse_listing_html


def read_csv(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path, data):
    Path(path).write_text(json.dumps(data), encoding="utf-8")


# Import the collectors without loading deployment settings or network transports.
settings = ModuleType("common.config")
settings.ACCOUNT_NAME = "Mediamarkt"
settings.COUNTRY = "SEG"
settings.PAGE_TYPE = "main"
settings.REFERENCES_ROOT = Path("unused-test-references")
settings.ensure_dirs = lambda *paths: [Path(path).mkdir(parents=True, exist_ok=True) for path in paths]
settings.read_csv = read_csv
settings.write_json = write_json
settings.page_url = lambda url, page: f"{url}&page={page}"
settings.env_value = lambda key, default=None: default
settings.db_config = Mock(side_effect=AssertionError("DB access is not allowed in offline tests"))
transport = ModuleType("common.zenrows")
transport.DEFAULT_PROXY_COUNTRY = "de"
transport.build_scraping_browser_url = Mock(side_effect=AssertionError("Network access is not allowed"))
with patch.dict(sys.modules, {"common.config": settings, "common.zenrows": transport}):
    listing = importlib.import_module("common.listing")
    full_output = importlib.import_module("common.full_output")
    pdp_detail = importlib.import_module("common.pdp_detail")
    db_save = importlib.import_module("common.db_save")
    notify = importlib.import_module("common.notify")


def page_html(products=(), *, ads=(), shown=12, total=100, more=True, legacy_ads=()):
    apollo = {"page": {"__typename": "ProductListPage", "products": []}}
    cards = []
    for sku_id, name in products:
        sku_id = str(sku_id)
        apollo["page"]["products"].append({"productId": sku_id,
            "adData": {"campaign": "test"} if sku_id in legacy_ads else None})
        apollo[sku_id] = {"__typename": "GraphqlProduct", "id": sku_id,
                         "title": name, "url": f"/de/product/_test-{sku_id}.html"}
        cards.append(f'<li><article><a href="/de/product/_test-{sku_id}.html">{escape(name)}</a></article></li>')
    for sku_id, name in ads:
        cards.insert(0, f'<li><article data-test="mms-sba-product-tile"><span>Gesponsert</span>'
                     f'<a href="/de/product/_test-{sku_id}.html">{escape(name)}</a></article></li>')
    counter = f'<div>{shown} von {total}</div>' if shown is not None else ""
    button = '<button>12 weitere Produkte anzeigen</button>' if more else ""
    return (f'<script>window.__PRELOADED_STATE__ = {json.dumps({"apolloState": apollo})};</script>'
            '<div id="mms-search-productlist"><ul role="list">' + "".join(cards)
            + '</ul>' + button + counter + '</div>')


def response(html, status=200):
    return html, status, 0.01, None, 0


def write_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class ListingPolicyTests(unittest.TestCase):
    def test_reported_dryer_and_refrigerator_are_excluded(self):
        for title, reason in [
            ("HAIER HD90-C657U1 W\u00e4rmepumpentrockner G5 (9 kg, Wei\u00df)", "standalone_dryer"),
            ("BEKO B5RCNO377X K\u00fchlgefrierkombination (377 l)", "refrigerator"),
            ("K\u00fchlschrank", "refrigerator"),
            ("Tumble dryer", "standalone_dryer"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(reason, listing_exclusion_reason({"retailer_sku_name": title}, "LDY"))

    def test_laundry_combinations_bundles_and_frontloaders_are_retained(self):
        for title in [
            "HISENSE Waschmaschine und W\u00e4rmepumpentrockner Bundle",
            "KOENIC Waschmaschine + W\u00e4rmepumpentrockner + Zwischenbaurahmen Bundle",
            "HAIER HWD130-BP14657U1 Waschtrockner",
            "LG WT1210BBF Wasch/Trocken-S\u00e4ule",
            "LG WashTower", "Washer-dryer", "KOENIC KBWM 8112 A-10 Frontlader",
        ]:
            with self.subTest(title=title):
                self.assertIsNone(listing_exclusion_reason({"retailer_sku_name": title}, "ldy"))

    def test_ref_category_keeps_refrigerators(self):
        self.assertIsNone(listing_exclusion_reason({"retailer_sku_name": "K\u00fchlschrank"}, "REF"))

    def test_same_sku_organic_occurrence_survives_dom_and_legacy_ads(self):
        for legacy in [(), ("111",)]:
            html = page_html([("111", "Waschmaschine"), ("222", "Waschmaschine")],
                             ads=[("111", "Waschmaschine")], legacy_ads=legacy)
            rows = filter_listing_rows(parse_listing_html(html), "ldy")
            self.assertEqual(["111", "222"], [row["sku_id"] for row in rows])
            self.assertTrue(all(row["sku_status"] is None for row in rows))

    def test_ad_only_state_and_dom_rows_do_not_return_via_fallback(self):
        html = page_html([("111", "Waschmaschine")], legacy_ads=("111",),
                         ads=[("3036864", "K\u00fchlgefrierkombination")])
        self.assertEqual([], filter_listing_rows(parse_listing_html(html), "ldy"))

    def test_ad_card_without_visible_label_is_excluded(self):
        html = page_html(ads=[("111", "Waschmaschine")]).replace("<span>Gesponsert</span>", "")
        self.assertEqual([], filter_listing_rows(parse_listing_html(html), "ldy"))

    def test_multi_product_ad_cannot_reenter_through_state_fallback(self):
        html = page_html([("111", "Waschmaschine"), ("222", "Waschmaschine")],
                         ads=[("111", "Waschmaschine")])
        html = html.replace('<span>Gesponsert</span>',
                            '<span>Gesponsert</span><a href="/de/product/_test-222.html">Waschmaschine</a>')
        html = html.replace('<li><article><a href="/de/product/_test-222.html">Waschmaschine</a></article></li>', '')
        rows = filter_listing_rows(parse_listing_html(html), "ldy")
        self.assertEqual(["111"], [r["sku_id"] for r in rows])

    def test_legacy_csv_filter_renumbers_only_when_rows_are_removed(self):
        rows = [{"sku_id": "1", "rank": "1", "sku_status": "Sponsored"},
                {"sku_id": "1", "rank": "2", "sku_status": ""}]
        result = filter_listing_rows(rows, "ldy", renumber=True)
        self.assertEqual([{"sku_id": "1", "rank": "1", "position": "1", "sku_status": ""}], result)
        self.assertEqual("2", rows[1]["rank"])


class CollectionTests(unittest.TestCase):
    def collect(self, fetch, target=300, **kwargs):
        with patch("sys.stdout", new=io.StringIO()):
            return listing.collect_pages(fetch, product="ldy", target=target, **kwargs)

    def test_main_continues_for_300_pages_without_a_page_cap(self):
        calls = []
        def fetch(page):
            calls.append(page)
            return response(page_html([(str(page), "Waschmaschine"),
                                       (str(1000 + page), "W\u00e4rmepumpentrockner")],
                                      ads=[("9999", "Waschmaschine")], shown=page * 2,
                                      total=1000, more=True))
        rows, log, reason = self.collect(fetch)
        self.assertEqual("target_reached", reason)
        self.assertEqual(list(range(1, 301)), calls)
        self.assertEqual(300, len(rows))
        self.assertEqual({"advertisement": 1, "standalone_dryer": 1}, log[0]["excluded"])

    def test_bsr_stops_at_100_and_trims_the_last_page(self):
        calls = []
        def fetch(page):
            calls.append(page)
            products = [(str(n), "Waschmaschine") for n in range((page - 1) * 12 + 1, page * 12 + 1)]
            return response(page_html(products, ads=[("9999", "Waschmaschine")], shown=page * 12, total=600))
        rows, _, reason = self.collect(fetch, target=100)
        self.assertEqual((100, 9, "target_reached"), (len(rows), len(calls), reason))
        self.assertEqual("100", rows[-1]["sku_id"])

    def test_last_page_keeps_a_partial_result_including_bundles(self):
        fetch = Mock(return_value=response(page_html([
            ("1", "Waschmaschine"), ("2", "Waschmaschine und Trockner Bundle"),
            ("3", "Trockner")], shown=3, total=3, more=False)))
        rows, _, reason = self.collect(fetch)
        self.assertEqual((["1", "2"], "last_page"), ([r["sku_id"] for r in rows], reason))
        self.assertEqual(1, fetch.call_count)

    def test_empty_last_page_is_valid_but_a_block_page_is_not(self):
        rows, _, reason = self.collect(Mock(return_value=response(page_html(shown=0, total=0, more=False))))
        self.assertEqual(([], "last_page"), (rows, reason))
        fetch = Mock(return_value=response("<html>Checking your browser</html>"))
        rows, _, reason = self.collect(fetch)
        self.assertEqual(([], "parse_failed", 3), (rows, reason, fetch.call_count))

    def test_page_with_only_excluded_products_does_not_end_collection(self):
        fetch = Mock(side_effect=[response(page_html([("1", "Trockner")], shown=1, total=2)),
                                  response(page_html([("2", "Waschmaschine")], shown=2, total=2, more=False))])
        rows, _, reason = self.collect(fetch)
        self.assertEqual((["2"], "last_page"), ([r["sku_id"] for r in rows], reason))

    def test_failure_retries_the_same_page_then_preserves_partial_rows(self):
        def fetch(page):
            return response(page_html([("1", "Waschmaschine")])) if page == 1 else response("blocked", 403)
        mocked = Mock(side_effect=fetch)
        rows, log, reason = self.collect(mocked)
        self.assertEqual([1, 2, 2, 2], [call.args[0] for call in mocked.call_args_list])
        self.assertEqual((1, "fetch_failed"), (len(rows), reason))
        self.assertEqual(403, log[-1]["status"])

    def test_transient_failure_recovers_without_skipping_a_page(self):
        fetch = Mock(side_effect=[response("blocked", 403),
                                  response(page_html([("1", "Waschmaschine")], shown=1, total=1, more=False))])
        rows, _, reason = self.collect(fetch)
        self.assertEqual([1, 1], [call.args[0] for call in fetch.call_args_list])
        self.assertEqual((1, "last_page"), (len(rows), reason))

    def test_repeated_page_fails_even_if_it_claims_to_be_the_last_page(self):
        fetch = Mock(side_effect=[response(page_html([("1", "Waschmaschine")]))] + [
            response(page_html([("1", "Waschmaschine")], shown=100, total=100, more=False))] * 3)
        rows, _, reason = self.collect(fetch)
        self.assertEqual((1, "repeated_page", 4), (len(rows), reason, fetch.call_count))

    def test_ad_only_repeated_response_cannot_loop_forever(self):
        fetch = Mock(return_value=response(page_html(ads=[("9999", "Waschmaschine")], shown=0)))
        rows, _, reason = self.collect(fetch)
        self.assertEqual(([], "repeated_page", 4), (rows, reason, fetch.call_count))

    def test_missing_counter_is_unknown_and_ratings_do_not_prove_last_page(self):
        html = page_html([("1", "Waschmaschine")], shown=None, more=False)
        html = html.replace("</article>", "<span>5 von 5</span></article>")
        pagination = extract_listing_pagination(html)
        self.assertIsNone(pagination["has_next"])
        self.assertFalse(pagination["last_page"])

    def test_last_page_counter_can_use_nested_markup_and_thousands_separator(self):
        html = page_html(shown=1200, total=1200, more=False).replace(
            "1200 von 1200", "<span>1.200</span><span>von</span><span>1.200</span>")
        pagination = extract_listing_pagination(html)
        self.assertEqual((1200, 1200, True),
                         (pagination["shown"], pagination["total"], pagination["last_page"]))


class SavedOutputTests(unittest.TestCase):
    def test_listing_entrypoint_ignores_page_cap_and_uses_bsr_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = SimpleNamespace(OUTPUT_ROOT=root, MAIN_TARGET_UNIQUE=300, BSR_TARGET_RANK=100,
                                  LISTING_URL="https://example.invalid/main?test=1",
                                  BSR_URL="https://example.invalid/bsr?test=1")
            args = SimpleNamespace(product="ldy", sort="bsr", target=0, max_pages=1,
                                   page_retries=2, sleep=0, render_settle=0, sponsored_extra_wait=0,
                                   timeout=90, transport="uc", output="")
            session = Mock(warmup_status=200)
            session.navigate.side_effect = [
                {"html": page_html([("1", "Waschmaschine"), ("2", "Trockner")],
                                   ads=[("3", "Waschmaschine")], shown=2, total=3),
                 "blocked": False, "error": None},
                {"html": page_html([("4", "Waschmaschine und Trockner Bundle")],
                                   shown=3, total=3, more=False), "blocked": False, "error": None}]
            uc = ModuleType("common.uc")
            uc.UcSession = Mock(return_value=session)
            with patch.object(listing, "parse_args", return_value=args), \
                    patch.object(listing, "load_cfg", return_value=cfg), \
                    patch.object(listing, "REFERENCES_ROOT", root / "references"), \
                    patch.dict(sys.modules, {"common.uc": uc}), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, listing.main())
            rows = read_csv(root / "mmkt_listing_bsr.csv")
            self.assertEqual(["1", "4"], [r["sku_id"] for r in rows])
            self.assertEqual(["1", "2"], [r["rank"] for r in rows])
            self.assertTrue(all(not r["sku_status"] for r in rows))
            manifest = json.loads((root / "mmkt_step01_listing_bsr_manifest.json").read_text())
            self.assertEqual((100, 2, "last_page", True),
                             (manifest["target"], manifest["pages_fetched"],
                              manifest["stop_reason"], manifest["success"]))
            session.close.assert_called_once()

    def test_detail_resume_removes_excluded_rows_and_keeps_good_rows_outside_slice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["Waschmaschine", "Waschmaschine und Trockner Bundle", "Waschmaschine",
                     "Trockner", "K\u00fchlschrank", "Waschmaschine"]
            targets = [{"sku_id": str(i), "product_url": f"https://example.invalid/{i}",
                        "retailer_sku_name": name, "rank": str(i),
                        "sku_status": "Sponsored" if i == 6 else ""}
                       for i, name in enumerate(names, 1)]
            write_csv(root / "mmkt_listing_main.csv", targets)
            cached = [{"sku_id": row["sku_id"], "rank": row["rank"], "ldy_capacity": "8kg",
                       IS_BUNDLE: "True" if row["sku_id"] == "2" else "False",
                       "review_partial": "False", "review_stop_reason": "actual_last_page",
                       "star_rating": "0.0", "count_of_star_ratings": "0",
                       "count_of_reviews": "0"} for row in targets]
            write_csv(root / "mmkt_pdp_detail.csv", cached)
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="LDY", SPEC_FIELDS=["ldy_capacity"])
            args = SimpleNamespace(product="ldy", input="", bsr="", output="", start=1, limit=1,
                                   sleep=0, review_pages=1, review_max_pages=1, transport="uc",
                                   concurrency=1, max_retries=0, resume=True, missing_retry="none")
            with patch.object(pdp_detail, "parse_args", return_value=args), \
                    patch.object(pdp_detail, "load_cfg", return_value=cfg), \
                    patch.object(pdp_detail, "make_session", side_effect=AssertionError("Network access")) as session, \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, pdp_detail.main())
            self.assertEqual(["1", "2", "3"],
                             [r["sku_id"] for r in read_csv(root / "mmkt_pdp_detail.csv")])
            session.assert_not_called()

    def test_no_eligible_products_complete_without_detail_or_database_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(root / "mmkt_listing_main.csv", [
                {"sku_id": "1", "retailer_sku_name": "Trockner", "product_url": "https://example.invalid/1"}])
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="LDY", SPEC_FIELDS=["ldy_capacity"],
                                  DB_TABLE=("test", "test"))
            detail_args = SimpleNamespace(product="ldy", input="", bsr="", output="", start=1, limit=0,
                                          sleep=0, review_pages=1, review_max_pages=1, transport="uc",
                                          concurrency=1, max_retries=0, resume=False, missing_retry="none")
            with patch.object(pdp_detail, "parse_args", return_value=detail_args), \
                    patch.object(pdp_detail, "load_cfg", return_value=cfg), \
                    patch.object(pdp_detail, "make_session", side_effect=AssertionError("Network access")) as session, \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, pdp_detail.main())
            session.assert_not_called()
            full_args = SimpleNamespace(product="ldy", listing="", bsr="", detail="", output="")
            with patch.object(full_output, "parse_args", return_value=full_args), \
                    patch.object(full_output, "load_cfg", return_value=cfg), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, full_output.main())
            self.assertEqual([], read_csv(root / "mmkt_full_output.csv"))
            db_args = SimpleNamespace(product="ldy", input="", dry_run=True)
            with patch.object(db_save, "parse_args", return_value=db_args), \
                    patch.object(db_save, "load_cfg", return_value=cfg), \
                    patch.object(db_save, "db_config", side_effect=AssertionError("DB access")) as database, \
                    patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, db_save.main())
            database.assert_not_called()

    def test_full_output_filters_each_listing_before_union_and_keeps_bundles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = {"batch_id": "test", "crawl_strdatetime": "2026-09-15 00:00:00", "calendar_week": "w38"}
            rows = [dict(common, sku_id="1", retailer_sku_name="Waschmaschine", rank="1", position="1", sku_status="Sponsored"),
                    dict(common, sku_id="2", retailer_sku_name="Trockner", rank="2", position="2", sku_status=""),
                    dict(common, sku_id="3", retailer_sku_name="Waschmaschine und Trockner Bundle", rank="3", position="3", sku_status="")]
            bsr = [dict(rows[0], sku_status=""), dict(rows[1], sku_id="4", retailer_sku_name="K\u00fchlschrank")]
            write_csv(root / "mmkt_listing_main.csv", rows)
            write_csv(root / "mmkt_listing_bsr.csv", bsr)
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="LDY", SPEC_FIELDS=["ldy_loading_type", "ldy_capacity"])
            args = SimpleNamespace(product="ldy", listing="", bsr="", detail="", output="")
            with patch.object(full_output, "parse_args", return_value=args), patch.object(full_output, "load_cfg", return_value=cfg), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, full_output.main())
            result = read_csv(root / "mmkt_full_output.csv")
            self.assertEqual(["3", "1"], [r["item"] for r in result])
            self.assertEqual("1", result[0]["main_rank"])
            self.assertEqual("1", result[1]["bsr_rank"])

    def test_db_dry_run_applies_the_same_filter_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{"item": "1", "retailer_sku_name": "Waschmaschine", "sku_status": "Sponsored"},
                    {"item": "2", "retailer_sku_name": "Trockner", "sku_status": ""},
                    {"item": "3", "retailer_sku_name": "Waschmaschine und Trockner Bundle", "sku_status": ""}]
            write_csv(root / "mmkt_full_output.csv", rows)
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="LDY", DB_TABLE=("test", "test"), SPEC_FIELDS=[])
            args = SimpleNamespace(product="ldy", input="", dry_run=True)
            with patch.object(db_save, "parse_args", return_value=args), patch.object(db_save, "load_cfg", return_value=cfg), patch.object(db_save, "db_config", side_effect=AssertionError("DB access")), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(0, db_save.main())
            manifest = json.loads((root / "step14_db_save_manifest.json").read_text())
            self.assertEqual((1, 2, True), (manifest["csv_rows"], manifest["excluded_rows"], manifest["dry_run"]))

    def test_report_accepts_short_last_page_and_zero_advertisements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sort, target in [("main", 300), ("bsr", 100)]:
                write_json(root / f"mmkt_step01_listing_{sort}_manifest.json", {
                    "success": True, "stop_reason": "last_page", "written_rows": 1, "target": target,
                    "sponsored_monitoring": {"visible_label_occurrences": 2, "excluded_rows": 2}})
            cfg = SimpleNamespace(OUTPUT_ROOT=root, PRODUCT="LDY", MAIN_TARGET_UNIQUE=300, BSR_TARGET_RANK=100, SPEC_FIELDS=[])
            rows = [{"main_rank": "1", "bsr_rank": "1", "sku": "test", "sku_status": ""}]
            with patch.object(notify, "env_value", side_effect=lambda key, default=None: default):
                subject, report = notify.build_report(cfg, rows)
            self.assertNotIn("[CHECK]", subject)
            self.assertIn("main - last_page", report)
            self.assertIn("main excluded - 2", report)


if __name__ == "__main__":
    unittest.main()
