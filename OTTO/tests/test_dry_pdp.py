"""Offline-only dryer PDP supplementation and partial-failure contracts."""
import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_dry as base
from dry import client, notify, pipeline, storage


def pdp_html(product="1", variation="v1", canonical="", rows=None):
    rows = rows or {"Modellbezeichnung": "MODEL900", "Fassungsvermögen": "9 kg", "Beladung": "Frontlader"}
    table = "".join(f"<tr><td>{label}</td><td>{value}</td></tr>" for label, value in rows.items())
    return (f'<link rel="canonical" href="{canonical}">'
            f'<div class="pdp-frame" data-product-id="{product}" data-variation-id="{variation}">'
            f'<div class="pdp_details__characteristics-html"><table class="dv_characteristicsTable">{table}</table></div>'
            '</div>').encode()


def response(body):
    return dict(status=200, detail_present=True, body=body)


class DryerPdpTests(base.Offline):
    def test_same_product_and_option_reuse_one_session(self):
        c = client.Client(base.Log())
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.fetch_pdp.return_value = response(pdp_html())
            first = c.pdp(base.target(1), {"sku", "capacity", "loading_type"})
            second = c.pdp(base.target(1), {"loading_type"})
            c.close()
        self.assertEqual(first, {"sku": "MODEL900", "capacity": "9 kg", "loading_type": "Frontlader"})
        self.assertEqual(second, {"loading_type": "Frontlader"})
        browser.assert_called_once()
        browser.return_value.open.assert_called_once()
        self.assertEqual(browser.return_value.fetch_pdp.call_count, 2)
        browser.return_value.close.assert_called_once()

    def test_unverified_wrong_and_ambiguous_identity_are_rejected(self):
        for body in (pdp_html(product="2"), pdp_html(variation="v2"),
                     pdp_html().replace(b'data-product-id="1"', b''),
                     pdp_html().replace(b'data-variation-id="v1"', b''), pdp_html() + pdp_html(),
                     pdp_html() + b'<div class="pdp-frame"></div>'):
            with self.subTest(body=body[:100]), patch("common.browser.BrowserSession") as browser:
                browser.return_value.fetch_pdp.return_value = response(body)
                c = client.Client(base.Log())
                value = c.pdp(base.target(1), {"loading_type"})
                c.close()
                self.assertEqual(value.get("_error_kind"), "extraction")
                self.assertNotIn("loading_type", value)

    def test_group_product_alias_requires_matching_canonical(self):
        target = base.target(1, pid="C123")
        for canonical, accepted in (("https://www.otto.de/p/item-C123/", True),
                                    ("https://www.otto.de/p/item-C999/", False)):
            with self.subTest(canonical=canonical), patch("common.browser.BrowserSession") as browser:
                browser.return_value.fetch_pdp.return_value = response(pdp_html("123", "v1", canonical))
                c = client.Client(base.Log())
                value = c.pdp(target, {"loading_type"})
                c.close()
                self.assertEqual(value.get("loading_type"), "Frontlader" if accepted else None)

    def test_connection_failure_is_cached_closed_and_does_not_expose_secret(self):
        log = base.Log()
        c = client.Client(log)
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.open.side_effect = RuntimeError("SECRET_TOKEN")
            browser.return_value.close.side_effect = RuntimeError("SECRET_TOKEN")
            values = [c.pdp(base.target(i), {"loading_type"}) for i in (1, 2, 3)]
            c.close()
        self.assertEqual(browser.call_count, 2)
        self.assertEqual(browser.return_value.close.call_count, 2)
        self.assertTrue(all(v.get("_error_kind") == "request" for v in values))
        self.assertNotIn("SECRET_TOKEN", repr((values, log.records)))

    def test_first_open_failure_retries_for_the_next_product(self):
        log = base.Log()
        first, second = MagicMock(), MagicMock()
        first.open.side_effect = RuntimeError("SECRET_TOKEN")
        second.fetch_pdp.return_value = response(pdp_html("2", "v2"))
        with patch("common.browser.BrowserSession", side_effect=[first, second]) as browser:
            c = client.Client(log)
            failed = c.pdp(base.target(1), {"loading_type"})
            recovered = c.pdp(base.target(2), {"loading_type"})
            c.close()
        self.assertEqual(failed["_error_kind"], "request")
        self.assertEqual(recovered["loading_type"], "Frontlader")
        self.assertEqual(browser.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertNotIn("SECRET_TOKEN", repr((failed, recovered, log.records)))

    def test_combined_appliance_needs_an_explicit_drying_capacity(self):
        generic = {"Produkttyp": "Waschtrockner", "Fassungsvermögen": "9 kg"}
        for rows, expected in ((generic, None),
                               ({**generic, "Nennkapazität (Trocknen)": "6 kg"}, "6 kg")):
            with self.subTest(rows=rows), patch("common.browser.BrowserSession") as browser:
                browser.return_value.fetch_pdp.return_value = response(pdp_html(rows=rows))
                c = client.Client(base.Log())
                result = c.pdp(base.target(1), {"capacity"})
                c.close()
                self.assertEqual(result.get("capacity"), expected)
                self.assertNotIn("_error", result)

    def test_combined_in_search_or_same_option_comparison_title_blocks_generic_capacity(self):
        for source in ("selection_name", "comparison_name"):
            with self.subTest(source=source), patch("common.browser.BrowserSession") as browser:
                browser.return_value.fetch_pdp.return_value = response(pdp_html(rows={"Fassungsvermögen": "9 kg"}))
                target = base.target(1)
                target["retailer_sku_name"] = "Trockner MODEL900"
                if source == "selection_name":
                    target[source] = "Trockner MODEL900 Waschtrockner"
                c = client.Client(base.Log())
                result = c.pdp(target, {"capacity"}, comparison_name="Trockner MODEL900 Waschtrockner" if source == "comparison_name" else None)
                c.close()
                self.assertNotIn("capacity", result)
                self.assertNotIn("_error", result)

    def test_missing_source_value_is_not_failure(self):
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.fetch_pdp.return_value = response(pdp_html(rows={"Bauart": "freistehend"}))
            c = client.Client(base.Log())
            result = c.pdp(base.target(1), {"loading_type", "capacity"})
            c.close()
        self.assertNotIn("_error", result)
        self.assertNotIn("capacity", result)
        self.assertNotIn("loading_type", result)

    def test_interrupt_during_browser_open_still_closes_session(self):
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.open.side_effect = KeyboardInterrupt()
            c = client.Client(base.Log())
            with self.assertRaises(KeyboardInterrupt):
                c.pdp(base.target(1), {"loading_type"})
        browser.return_value.close.assert_called_once()

    def test_invalid_product_url_is_reported_without_connection(self):
        target = base.target(1)
        target["product_url"] = "https://[invalid"
        with patch("common.browser.BrowserSession") as browser:
            result = client.Client(base.Log()).pdp(target, {"loading_type"})
        self.assertEqual(result["_error"], "pdp_identity_missing")
        browser.assert_not_called()

    def test_conflicting_same_label_is_not_used(self):
        body = pdp_html(rows={"Beladung": "Frontlader"}).replace(
            b"</table>", b'</table><table class="dv_characteristicsTable"><tr><td>Beladung</td><td>Toplader</td></tr></table>')
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.fetch_pdp.return_value = response(body)
            c = client.Client(base.Log())
            result = c.pdp(base.target(1), {"loading_type"})
            c.close()
        self.assertEqual(result.get("_error"), "pdp_ambiguous_characteristic")
        self.assertNotIn("loading_type", result)

    def test_duplicate_label_with_blank_or_equivalent_value_keeps_the_valid_value(self):
        for second in ("-", "Front loader"):
            with self.subTest(second=second), patch("common.browser.BrowserSession") as browser:
                body = pdp_html(rows={"Beladung": "Frontlader"}).replace(
                    b"</table>", f'</table><table class="dv_characteristicsTable"><tr><td>Beladung</td><td>{second}</td></tr></table>'.encode())
                browser.return_value.fetch_pdp.return_value = response(body)
                c = client.Client(base.Log())
                result = c.pdp(base.target(1), {"loading_type"})
                c.close()
                self.assertNotIn("_error", result)
                self.assertEqual(client.loading(result["loading_type"]), "Front loader")

    def test_later_characteristics_table_is_checked(self):
        body = pdp_html(rows={"Sonstiges": "unrelated"}).replace(
            b"</table>", b'</table><table class="dv_characteristicsTable"><tr><td>Beladung</td><td>Frontlader</td></tr></table>')
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.fetch_pdp.return_value = response(body)
            c = client.Client(base.Log())
            result = c.pdp(base.target(1), {"loading_type"})
            c.close()
        self.assertEqual(result["loading_type"], "Frontlader")

    def test_conflicting_loading_labels_keep_priority_and_record_evidence(self):
        with patch("common.browser.BrowserSession") as browser:
            browser.return_value.fetch_pdp.return_value = response(pdp_html(rows={
                "Beladung": "Toplader", "Bauart": "Frontlader"}))
            c = client.Client(base.Log())
            result = c.pdp(base.target(1), {"loading_type"})
            c.close()
        self.assertEqual(result["loading_type"], "Toplader")
        self.assertEqual([v["normalized"] for v in result["_conflicts"]["loading_type"]],
                         ["Top-loading", "Front loader"])
        self.assertNotIn("_error", result)


class DryerPdpPipelineTests(base.Offline):
    def test_complete_fields_do_not_open_a_pdp(self):
        source = base.Source([[base.target(1)]])
        source.pdp = MagicMock(side_effect=AssertionError("UNNECESSARY_PDP"))
        state = dict(target=1, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-23 12:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda rows: None)
        self.assertEqual(rows[0]["loading_type"], "Front loader")
        source.pdp.assert_not_called()

    def test_pdp_failure_continues_save_and_reports_warning(self):
        source = base.Source([[base.target(1), base.target(2)]], {"v1": {"Modellbezeichnung": "MODEL900"}})
        source.pdp = MagicMock(return_value={"_error": "pdp_product_or_option_mismatch", "_error_kind": "extraction"})
        source.close = MagicMock()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(side_effect=lambda table, events, rows=None:
                           {"status": "loaded", "inserted": len(rows or []), "duplicates": 0})
            code, state = pipeline.execute(limit=2, output_root=root, client_factory=lambda e: source,
                                           database=db, backfill=lambda table, events, rows: {})
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(code, 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["sku"], "MODEL900")
            self.assertEqual(rows[0]["loading_type"], "")
            self.assertEqual(state["counts"]["extraction_error_items"], 1)
            self.assertEqual(db.call_args.args[2][1]["item"], "2")
        source.close.assert_called_once()

    def test_first_pdp_failure_does_not_prevent_later_product_recovery(self):
        source = base.Source([[base.target(1), base.target(2)]],
                             {"v1": {"Modellbezeichnung": "MODEL100"}, "v2": {"Modellbezeichnung": "MODEL200"}})
        source.pdp = MagicMock(side_effect=[{"_error": "pdp_connect_failed:RuntimeError", "_error_kind": "request"},
                                            {"loading_type": "Frontlader"}])
        source.close = MagicMock()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(side_effect=lambda table, events, rows=None:
                           {"status": "loaded", "inserted": len(rows or []), "duplicates": 0})
            code, state = pipeline.execute(limit=2, output_root=root, client_factory=lambda e: source,
                                           database=db, backfill=lambda table, events, rows: {})
            rows, _ = storage.read_output(state["csv"])
        self.assertEqual(code, 2)
        self.assertEqual([r["loading_type"] for r in rows], ["", "Front loader"])
        self.assertEqual(state["counts"]["request_error_items"], 1)
        self.assertEqual(db.call_args.args[2][1]["item"], "2")
        source.close.assert_called_once()

    def test_pdp_conflict_is_warning_and_valid_rows_are_stored(self):
        source = base.Source([[base.target(1)]], {"v1": {"Modellbezeichnung": "MODEL900"}})
        details = [{"label": "Beladung", "raw": "Toplader", "normalized": "Top-loading"},
                   {"label": "Bauart", "raw": "Frontlader", "normalized": "Front loader"}]
        source.pdp = MagicMock(return_value={"loading_type": "Toplader", "_conflicts": {"loading_type": details}})
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(side_effect=lambda table, events, rows=None:
                           {"status": "loaded", "inserted": len(rows or []), "duplicates": 0})
            code, state = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: source,
                                           database=db, backfill=lambda table, events, rows: {})
            rows, _ = storage.read_output(state["csv"])
            diagnostics = Path(state["diagnostics"]).read_text(encoding="utf-8")
        self.assertEqual(code, 2)
        self.assertEqual(rows[0]["loading_type"], "Top-loading")
        self.assertEqual(state["counts"]["pdp_conflict_items"], 1)
        self.assertEqual(state["counts"]["extraction_error_items"], 0)
        subject, report = notify.build_report(state, rows)
        self.assertIn("확인 필요", subject)
        self.assertIn("상세 사양 충돌 상품 1건", report)
        self.assertIn("pdp_conflicting_characteristics", diagnostics)
        self.assertIn("Frontlader", diagnostics)

    def test_only_missing_fields_are_filled_and_existing_values_win(self):
        source = base.Source([[base.target(1)]], {"v1": {"Modellbezeichnung": "MODEL900"}})
        source.pdp = MagicMock(return_value={"sku": "WRONG100", "loading_type": "Frontlader"})
        state = dict(target=1, batch_id="batch", calendar_week="w43", crawl_datetime="2026-10-23 12:00:00")
        log = base.Log()
        rows = pipeline.collect(source, log, state, lambda rows: None)
        self.assertEqual((rows[0]["sku"], rows[0]["capacity"], rows[0]["loading_type"]),
                         ("MODEL900", "9 kg", "Front loader"))
        self.assertEqual(source.pdp.call_args.args[1], {"loading_type"})
        self.assertIsNone(source.pdp.call_args.kwargs["comparison_name"])
        record = next(v for event, v in log.records if event == "field" and v["field"] == "loading_type")
        self.assertEqual(record["source"], "product_pdp")

    def test_collection_failure_still_closes_client(self):
        source = base.Source([[base.target(1)]], failure=1)
        source.close = MagicMock()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, _ = pipeline.execute(limit=1, output_root=root, client_factory=lambda e: source,
                                       database=MagicMock(return_value={"status": "loaded"}))
        self.assertEqual(code, 3)
        source.close.assert_called_once()
