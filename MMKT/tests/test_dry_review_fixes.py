"""Reviewed product cases; browser/API/DB/SMTP are always fake."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_dry_pipeline import Browser, page_html, ratings, specs
from test_dry_updates import db_row, fake_db, schema
from dry import config, database, notify, run
from dry.detail import CollectionError, DetailCollector, parse_purchase_status, parse_specs
from dry.fields import title_sku
from dry.output import UNAVAILABLE_PRICE, merge_rows, run_metadata, write_manifest, write_output
from dry.policy import exclusion_reason, washer_dryer_evidence


def with_status(payload, *, buyable=False, delivery="NOT_AVAILABLE", pickup="NOT_AVAILABLE"):
    payload = copy.deepcopy(payload)
    target = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]
    target["cofrProductAggregate"] = {
        "productId": target["productAggregate"]["productId"],
        "cofrOnlineStatusFeature": {"isAvailableAndBuyable": buyable},
        "cofrDeliveryFeature": {"delivery": {"displayStatus": delivery}},
        "cofrPickupFeature": {"displayStatus": pickup}}
    return payload


class ReviewedPolicyTests(unittest.TestCase):
    def test_reviewed_mixed_products_and_equal_capacities_are_excluded(self):
        names = ["BOSCH WNA144V0FR Series 4 Wäschetrockner (9 kg / 5 kg, 1400 U/Min.)",
                 "KOENIC KBWDR 8612 A INV Frontlader (8 kg / 8 kg, 1400 U/Min.)",
                 "OTHER MODEL888 Trockner (7,5 kg / 4,5 kg, 1200 rpm)"]
        for name in names:
            with self.subTest(name=name):
                row = {"retailer_sku_name": name, "listing_card_type": "standard", "sku_status": "Sponsored"}
                self.assertEqual("washer_dryer", exclusion_reason(row))
                self.assertEqual("capacity_pair_and_spin_speed", washer_dryer_evidence(row)["basis"])

    def test_pair_spin_or_separate_sources_alone_cannot_exclude_a_dryer(self):
        for name in ["BRAND MODEL123 Trockner (9 kg / 5 kg)",
                     "BRAND MODEL123 Trockner (9 kg, 1400 U/Min.)",
                     "BRAND MODEL123 Trockner (8 kg) 6 Programme Serie 1400",
                     "Mini Trockner (4 kg)",
                     "BRAND MODEL123 Trockner mit Anschluss (9 kg / 5 kg, 1400 U/Min.)"]:
            with self.subTest(name=name):
                self.assertIsNone(exclusion_reason({"retailer_sku_name": name}))
        self.assertIsNone(exclusion_reason({"retailer_sku_name": "Trockner (9 kg / 5 kg)"},
                                          features={"Drehzahl": "1400 U/Min."}))

    def test_api_title_and_explicit_type_remain_valid_evidence(self):
        row = {"retailer_sku_name": "MODEL123"}
        title = "MODEL123 Frontlader (8 kg / 8 kg, 1400 U/Min.)"
        self.assertEqual("washer_dryer", exclusion_reason(row, detail_name=title))
        evidence = washer_dryer_evidence(row, features={"Produkttyp": "Einbau-Waschtrockner"})
        self.assertEqual(("spec", "Produkttyp", "explicit_washer_dryer"),
                         (evidence["source"], evidence["label"], evidence["basis"]))

    def test_existing_banner_portabel_and_normal_sponsored_rules_survive(self):
        for title, card, expected in [("Wäschetrockner", "standard", None),
                                      ("Mini Wäschetrockner", "standard", None),
                                      ("Wäschetrockner", "sponsored-ad", "advertisement"),
                                      ("PORTABEL Wäschetrockner", "standard", "portabel_title"),
                                      ("Waschmaschine", "standard", "washing_machine"),
                                      ("Wäschetrockner Set", "standard", "set_or_combo")]:
            self.assertEqual(expected, exclusion_reason({"retailer_sku_name": title,
                             "listing_card_type": card, "sku_status": "Sponsored"}))


class ReviewedSkuTests(unittest.TestCase):
    def test_reviewed_title_values_override_conflicting_api_models(self):
        cases = [("HEINNER HHPD-HMY80IVFC+++ Wärmepumpentrockner (8 kg, C)", "HHPD-HMK80IVFC+++", "HHPD-HMY80IVFC+++"),
                 ("CANDY CSOE H8A2DE-S Wärmepumpentrockner (8 kg, E)", "31102194", "CSOE H8A2DE-S"),
                 ("BEKO B3T42239 Wärmepumpentrockner (8 kg, E)", "7188236650", "B3T42239"),
                 ("HISENSE DHQE800BW2 Kondensationstrockner (8 kg, D)", "20014475", "DHQE800BW2"),
                 ("BOMANN WPT 7154 Wärmepumpentrockner (D)", "771540", "WPT 7154")]
        for title, api, expected in cases:
            with self.subTest(title=title):
                parsed = parse_specs(specs("1", name=title, extra={"Modelkennung": api}), "1")
                self.assertEqual(expected, parsed["sku"])
                self.assertEqual("title", parsed["_sources"]["sku"]["source"])
                self.assertTrue(parsed["_sources"]["sku"]["conflict"])
                self.assertEqual(api, parsed["_sources"]["sku"]["spec_value"])

    def test_spaced_models_are_not_truncated_or_replaced_by_spec_prefix(self):
        cases = [("BOSCH WQB 245 B 40 Trockner", "WQB245B40", "WQB 245 B 40"),
                 ("AEG TR 9 W 80690 Trockner", "TR9W80690 916099279", "TR 9 W 80690"),
                 ("SAMSUNG DV 91 DG 52 A 0 AEEG Trockner", "DV91DG52A0AE", "DV 91 DG 52 A 0 AEEG"),
                 ("SAMSUNG DV90DG52A0AEEG Trockner", "DV90DG52A0AE", "DV90DG52A0AEEG"),
                 ("HOOVER NDE H8A2TCEXS-S Trockner", "NDE H8A2TCEXS-S", "NDE H8A2TCEXS-S"),
                 ("KOENIC KTD 9332 C Trockner", "KTD 9332 C", "KTD 9332 C"),
                 ("HEINNER HHPD-M8G3PDD++ Trockner (A+++)", "HHPD-M8G3PDD++", "HHPD-M8G3PDD++")]
        for title, api, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(expected, parse_specs(specs("1", name=title, extra={"Modelkennung": api}), "1")["sku"])

    def test_explicit_numeric_spec_is_allowed_only_as_fallback(self):
        parsed = parse_specs(specs("1", name="BRAND Trockner", extra={"Modelkennung": "12345678"}), "1")
        self.assertEqual("12345678", parsed["sku"])
        self.assertEqual("spec", parsed["_sources"]["sku"]["source"])

    def test_matching_api_model_later_in_title_cannot_replace_the_first_model(self):
        parsed = parse_specs(specs("1", name="BRAND RIGHT123 Trockner mit WRONG456",
                                   extra={"Modelkennung": "WRONG456"}), "1")
        self.assertEqual("RIGHT123", parsed["sku"])

    def test_measurements_functions_and_energy_classes_never_become_title_models(self):
        for name in ["BRAND 800W/400W 3-in-1 Trockner", "BRAND PTC 1200 W Trockner",
                     "BRAND LED 2026 Trockner", "BRAND 10 Programme 8kg Trockner",
                     "BRAND Serie 8000 Trockner", "AEG 8000 Trockner", "BRAND A+++ Trockner"]:
            self.assertIsNone(title_sku(name))
        self.assertEqual("MODEL123", title_sku("BRAND MODEL123 A+++ Trockner"))

    def test_reviewed_model_missing_titles_do_not_invent_skus(self):
        names = ["COSTWAY Mini 5KG, LED Touch-Screen Wäschetrockner",
                 "FLYSTAR LED-Touchscreen 6 Programme Wäschetrockner, A++ -40%)",
                 "FLYSTAR 6 Programmen mit LED-Touchscreen Wäschetrockner, A++ -40%)",
                 "DEBUY Abluft 3kg 830W Wandmontierbar Wärmepumpentrockner (3 kg, -)",
                 "FLYSTAR 4kg kompakt 6 Programme Wäschetrockner, A++ -40%)",
                 "KITTECH 4 Kg Mit 5 Programme Bis Zu 60℃,800W/400W Filter für Wäschetrockner, G)",
                 "ZESHOP 4 kg mit Edelstahltrommel 800 W Wäschetrockner, A++ -40%)",
                 "FLYSTAR 4 kg PTC Keramikheizung Edelstahltrommel Wäschetrockner, A++ -40%)",
                 "FLYSTAR PTC Keramikheizung Edelstahltrommel Wäschetrockner, A++ -40%)",
                 "FLYSTAR 4 kg mit 5 Programme 800W/400W Mini Wäschetrockner, A++ -40%)",
                 "ZESHOP 4 kg mit 5 Programmen bis 60°C Wäschetrockner, A++ -40%)"]
        for name in names:
            with self.subTest(name=name):
                parsed = parse_specs(specs("1", name=name, extra={"Modelkennung": None}), "1")
                self.assertIsNone(parsed["sku"])

    def test_reviewed_capacity_missing_titles_stay_null_without_spec_units(self):
        for title in ["EXQUISIT TWP803-031D weiss Wärmepumpentrockner (D)",
                      "EXQUISIT TWP1001-030D weiss Wärmepumpentrockner (D)",
                      "SAMSUNG DV80DG52B0TEEG Wärmepumpentrockner", "BOMANN WPT 7155 Wärmepumpentrockner (D)",
                      "BOMANN WPT 7156 Wärmepumpentrockner (D)", "BOSCH HAUSGERÄTE WTH83004 Wärmepumpentrockner"]:
            self.assertIsNone(parse_specs(specs("1", name=title, extra={"Nennkapazität": None}), "1")["capacity"])


class PurchaseStatusTests(unittest.TestCase):
    def test_observed_target_status_is_available_without_an_extra_query(self):
        payload = with_status(specs("1"))
        browser = Browser({})
        browser.gql = Mock(side_effect=[{"status": 200, "data": payload}, {"status": 200, "data": ratings()}])
        with contextlib.redirect_stdout(io.StringIO()):
            collector = DetailCollector(browser, sleep_s=0)
            detail = collector.get("1")
            self.assertIs(detail, collector.get("1"))
        self.assertEqual(2, browser.gql.call_count)
        self.assertTrue(detail["_purchase_status"]["unavailable"])
        self.assertFalse(browser.gql.call_args_list[0].args[1]["cofrConfig"]["isEnabled"])

    def test_other_product_status_and_free_text_are_never_used_or_retained(self):
        payload = with_status(specs("1"), buyable=True, delivery="AVAILABLE", pickup="AVAILABLE")
        products = payload["data"]["comparisonTableRecommendations"]["tableData"]["products"]
        other = with_status(specs("99"))["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]
        products.insert(0, other)
        products[1]["cofrProductAggregate"]["cofrOnlineStatusFeature"].update(onlineStatus="DO_NOT_RETAIN", message="DO_NOT_RETAIN")
        state = parse_purchase_status(payload, "1")
        self.assertFalse(state["unavailable"])
        self.assertNotIn("DO_NOT_RETAIN", repr(state))
        with self.assertRaisesRegex(CollectionError, "DETAIL_TARGET_NOT_FOUND"):
            parse_purchase_status(payload, "missing")

    def test_partial_missing_unknown_or_contradictory_states_do_not_prove_unavailability(self):
        for buyable, delivery, pickup in [(True, "NOT_AVAILABLE", "NOT_AVAILABLE"),
                                          (None, "NOT_AVAILABLE", "NOT_AVAILABLE"),
                                          (False, "NOT_AVAILABLE", "AVAILABLE"),
                                          (False, None, "NOT_AVAILABLE"),
                                          (False, "UNRECOGNIZED_STATUS", "NOT_AVAILABLE")]:
            self.assertFalse(parse_purchase_status(with_status(specs("1"), buyable=buyable,
                             delivery=delivery, pickup=pickup), "1")["unavailable"])
        self.assertFalse(parse_purchase_status(specs("1"), "1")["unavailable"])

    def test_malformed_present_status_fails_instead_of_becoming_unavailable(self):
        for feature, value in [("cofrOnlineStatusFeature", []),
                                ("cofrOnlineStatusFeature", {"isAvailableAndBuyable": "false"}),
                                ("cofrOnlineStatusFeature", {"isAvailableAndBuyable": 0}),
                                ("cofrDeliveryFeature", {"delivery": []}),
                                ("cofrPickupFeature", {"displayStatus": False})]:
            payload = with_status(specs("1"))
            payload["data"]["comparisonTableRecommendations"]["tableData"]["products"][0]["cofrProductAggregate"][feature] = value
            with self.assertRaisesRegex(CollectionError, "DETAIL_AVAILABILITY_INVALID"):
                parse_purchase_status(payload, "1")

    def test_api_error_is_not_a_stock_state_and_is_not_cached(self):
        browser = Browser({})
        browser.gql = Mock(return_value={"status": 403, "raw": "DO_NOT_PRINT"})
        collector = DetailCollector(browser, retries=0, sleep_s=0)
        with contextlib.redirect_stdout(io.StringIO()) as screen, self.assertRaises(CollectionError):
            collector.get("1")
        self.assertEqual({}, collector.cache)
        self.assertNotIn("DO_NOT_PRINT", screen.getvalue())


class PriceAndReportTests(unittest.TestCase):
    def merge(self, detail=None, primary_price=None, secondary_price=None):
        row = {"sku_id": "1", "retailer_sku_name": "BRAND MODEL123 Trockner", "final_sku_price": primary_price,
               "product_url": "https://www.mediamarkt.de/de/product/_test-1.html"}
        secondary = {**row, "final_sku_price": secondary_price}
        with contextlib.redirect_stdout(io.StringIO()):
            return merge_rows([row], [secondary], {"1": detail or {}}, run_metadata())[0]

    def test_only_confirmed_unavailability_without_any_price_gets_the_phrase(self):
        state = parse_purchase_status(with_status(specs("1")), "1")
        detail = {"_purchase_status": state}
        self.assertEqual(UNAVAILABLE_PRICE, self.merge(detail)["final_sku_price"])
        self.assertEqual("499,99 €", self.merge(detail, "499,99 €")["final_sku_price"])
        self.assertEqual("489,99 €", self.merge(detail, secondary_price="489,99 €")["final_sku_price"])
        self.assertEqual(0, self.merge(detail, 0)["final_sku_price"])
        self.assertIsNone(self.merge()["final_sku_price"])
        unknown = parse_purchase_status(with_status(specs("1"), pickup="AVAILABLE"), "1")
        self.assertIsNone(self.merge({"_purchase_status": unknown})["final_sku_price"])

    def test_phrase_roundtrips_validated_csv_text_db_and_load_only_without_crawling(self):
        detail = {"_purchase_status": parse_purchase_status(with_status(specs("1")), "1"), "sku": "MODEL123"}
        row = self.merge(detail)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "mmkt_dry_output.csv"
            write_output(path, [row])
            manifest = {"success": True, "artifact_kind": "final", "collection_status": "complete",
                        "batch_id": row["batch_id"], "output_rows": 1, "output_digest": database.file_digest(path)}
            write_manifest(root / "mmkt_dry_manifest.json", manifest)
            self.assertEqual(UNAVAILABLE_PRICE, database.load_validated_output(path)[0]["final_sku_price"])
            connection, cursor = fake_db()
            with patch.object(database, "connect", return_value=connection), \
                 patch.object(database, "table_metadata", return_value=schema()), contextlib.redirect_stdout(io.StringIO()):
                database.inspect_or_insert(config.DB_TABLE, [row])
            inserted = dict(zip(config.COLUMNS[1:], cursor.executemany.call_args.args[1][0]))
            self.assertEqual(UNAVAILABLE_PRICE, inserted["final_sku_price"])
            self.assertEqual(20, len(row))
            digest = database.file_digest(path)
            with patch("common.uc.UcSession") as browser, patch.object(DetailCollector, "query") as api, \
                 patch.object(run, "inspect_or_insert", return_value={"inserted": 0, "already_loaded": True}) as db, \
                 patch.object(run, "notify_run", return_value={"state": "disabled", "failed": False}), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = run.main(["--write-db", "--output-dir", str(root), "--log-dir", str(root / "logs")])
            self.assertEqual(0, code)
            browser.assert_not_called()
            api.assert_not_called()
            self.assertEqual(UNAVAILABLE_PRICE, db.call_args.args[1][0]["final_sku_price"])
            self.assertEqual(digest, database.file_digest(path))

    def test_new_title_model_wins_while_only_missing_capacity_uses_history(self):
        connection, cursor = fake_db()
        row = db_row(sku="HHPD-HMY80IVFC+++", loading_type="Front load")
        cursor.fetchmany.side_effect = [[("SEG", "Mediamarkt", "1", "HHPD-HMK80IVFC+++", "Top load", "8kg")]]
        with patch.object(database, "connect", return_value=connection), \
             patch.object(database, "table_metadata", return_value=schema()), contextlib.redirect_stdout(io.StringIO()):
            result = database.inspect_or_insert(config.DB_TABLE, [row])
        inserted = dict(zip(config.COLUMNS[1:], cursor.executemany.call_args.args[1][0]))
        self.assertEqual(("HHPD-HMY80IVFC+++", "Front load", "8kg"), tuple(inserted[k] for k in database.BACKFILL_FIELDS))
        self.assertEqual({"sku": 0, "loading_type": 0, "capacity": 1}, result["backfilled"])
        self.assertIsNone(inserted["final_sku_price"])

    def test_bilingual_partial_report_distinguishes_product_failure_from_confirmed_db(self):
        manifest = {"success": True, "collection_status": "partial", "output_rows": 207,
                    "targets": {"main": 300, "bsr": 100}, "main": {"rows": 206, "stop_reason": "last_page"},
                    "bsr": {"rows": 100, "stop_reason": "target_reached"}, "product_failure_count": 1,
                    "product_failures": [{"sort": "main", "page": 22, "item": "1", "error": "UNCONFIRMED_PRODUCT_TYPE"}],
                    "database": {"state": "success", "db_written": True, "inserted": 207},
                    "timing": {"collection_elapsed_s": 1091.9}}
        subject, body = notify.build_report(manifest, config.DB_TABLE, success=False,
                                          error="PARTIAL_PRODUCT_FAILURES", elapsed_s=1144)
        self.assertTrue(subject.startswith("[CHECK]"))
        for text in ["일부 상품 실패", "DB 적재: 반영 확인됨", "신규 적재: 207개", "실패 상품: 1",
                     "MAIN: 206개 / 목표 300개", "마지막 목록 확인", "18분 11.9초", "19분 4.0초",
                     "main: 206/300 (last_page)", "English details"]:
            self.assertIn(text, body)
        self.assertLess(body.index("한글 요약"), body.index("English details"))

    def test_db_failure_uncertainty_and_unstarted_listing_are_clear_in_korean(self):
        for db, expected in [({"state": "not_started", "db_written": False}, "미실행"),
                              ({"state": "failed", "db_written": False}, "실패"),
                              ({"state": "failed", "db_written": None}, "반영 여부 불확실")]:
            _, body = notify.build_report({"collection_status": "failed", "main": {"state": "failed", "rows": 5},
                      "bsr": {"state": "not_started", "rows": None}, "database": db}, config.DB_TABLE, success=False)
            self.assertIn("DB 적재: " + expected, body)
            self.assertIn("BSR: 미실행", body)


class ReviewedExecutionTests(unittest.TestCase):
    def test_malformed_purchase_status_skips_only_that_product_and_keeps_check_policy(self):
        browser = Browser({("main", 1): page_html([("1", "BRAND BAD123 Trockner"),
                                                     ("2", "BRAND GOOD123 Trockner")], shown=2, total=2),
                           ("bsr", 1): page_html([("2", "BRAND GOOD123 Trockner")], shown=1, total=1)})
        browser.open, browser.close = Mock(), Mock()
        def gql(operation, variables):
            browser.calls.append((operation, variables))
            pid = variables.get("ref") or variables.get("id")
            if operation == "GetComparisonTableRecommendations":
                return {"status": 200, "data": with_status(specs(pid), buyable="false" if pid == "1" else False)}
            return {"status": 200, "data": ratings()}
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("common.uc.UcSession", return_value=browser), \
                 patch.object(run, "inspect_or_insert", return_value={"inserted": 1}) as db, \
                 patch.object(run, "notify_run", return_value={"state": "disabled", "failed": False}) as mail, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = run.main(["--output-dir", str(root), "--log-dir", str(root / "logs"), "--render-settle", "0", "--sleep", "0"])
            self.assertEqual(1, code)
            self.assertEqual(3, len(browser.calls))
            rows = database.load_validated_output(root / "mmkt_dry_output.csv")
            self.assertEqual(["2"], [r["item"] for r in rows])
            self.assertEqual(UNAVAILABLE_PRICE, rows[0]["final_sku_price"])
            self.assertEqual(["2"], [r["item"] for r in db.call_args.args[1]])
            self.assertFalse(mail.call_args.kwargs["collection_success"])
            manifest = json.loads((root / "mmkt_dry_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", manifest["collection_status"])
            self.assertEqual("DETAIL_AVAILABILITY_INVALID", manifest["product_failures"][0]["error"])

    def test_mixed_exclusions_status_phrase_ranks_db_and_bilingual_mail_end_to_end(self):
        stock_id, candy_id = "158447928", "103255436"
        stock_title = "HEINNER HHPD-M9K3A+++ Wärmepumpentrockner (9 kg, C)"
        candy_title = "CANDY CSOE H8A2DE-S Wärmepumpentrockner (8 kg, E)"
        pages = {("main", 1): page_html([
                    ("149712384", "BOSCH WNA144V0FR Wäschetrockner (9 kg / 5 kg, 1400 U/Min.)"),
                    ("3007945", "KOENIC KBWDR 8612 A INV Frontlader (8 kg / 8 kg, 1400 U/Min.)"),
                    ("9", "PORTABEL Wäschetrockner"), (stock_id, stock_title)],
                    ads=[("99", "Wäschetrockner")], shown=4, total=5, more=True),
                 ("main", 2): page_html([(candy_id, candy_title)], shown=5, total=5),
                 ("bsr", 1): page_html([(candy_id, candy_title), (stock_id, stock_title)], shown=2, total=2)}
        browser = Browser(pages)
        browser.open, browser.close = Mock(), Mock()
        def gql(operation, variables):
            browser.calls.append((operation, variables))
            pid = variables.get("ref") or variables.get("id")
            if operation == "GetComparisonTableRecommendations":
                payload = specs(pid, name=stock_title if pid == stock_id else candy_title,
                                extra={"Modelkennung": "HHPD-M9K3A+++" if pid == stock_id else "31102194"})
                return {"status": 200, "data": with_status(payload) if pid == stock_id else payload}
            return {"status": 200, "data": ratings(0, (0, 0, 0, 0, 0))}
        browser.gql = gql
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("common.uc.UcSession", return_value=browser), \
                 patch.object(run, "inspect_or_insert", return_value={"inserted": 2, "already_loaded": False}) as db, \
                 patch.object(notify, "env_value", side_effect=lambda key, default=None: "1" if key == "SEG_EMAIL_NOTIFY" else "0"), \
                 patch.object(notify, "_send", return_value=(True, 1, None)) as send, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = run.main(["--output-dir", str(root), "--log-dir", str(root / "logs"), "--render-settle", "0", "--sleep", "0"])
            self.assertEqual(0, code)
            self.assertEqual(4, len(browser.calls))
            rows = database.load_validated_output(root / "mmkt_dry_output.csv")
            by_id = {r["item"]: r for r in rows}
            self.assertEqual({stock_id, candy_id}, set(by_id))
            self.assertEqual(UNAVAILABLE_PRICE, by_id[stock_id]["final_sku_price"])
            self.assertEqual("CSOE H8A2DE-S", by_id[candy_id]["sku"])
            self.assertEqual(("1", "2"), (by_id[stock_id]["main_rank"], by_id[stock_id]["bsr_rank"]))
            self.assertEqual(config.PRODUCTION_TABLE, db.call_args.args[0])
            manifest = json.loads((root / "mmkt_dry_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(0, manifest["product_failure_count"])
            self.assertEqual(2, manifest["main"]["excluded"]["washer_dryer"])
            self.assertEqual("last_page", manifest["main"]["stop_reason"])
            send.assert_called_once()
            subject, body = send.call_args.args
            self.assertIn("수집·적재 완료", subject)
            self.assertFalse(subject.startswith("[CHECK]"))
            self.assertIn("MAIN: 2개 / 목표 300개", body)
            self.assertIn("English details", body)
            log = [json.loads(line) for file in (root / "logs").glob("*.log") for line in file.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(x["event"] == "product_excluded" and (x.get("evidence") or {}).get("basis") == "capacity_pair_and_spin_speed" for x in log))
            price_events = [x for x in log if x["event"] == "field_selected" and x.get("field") == "final_sku_price"]
            self.assertEqual(1, len(price_events))
            self.assertTrue(price_events[0]["derived"])
            self.assertTrue(any(x.get("field") == "sku" and x.get("conflict") is True for x in log))
