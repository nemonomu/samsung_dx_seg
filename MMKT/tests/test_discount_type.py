"""Offline discount translation, CSV/DB parity, and email alert contracts."""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from html import escape
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.discount_types import TEXT_TRANSLATIONS, translate_discount_type
from common.parsers import extract_rendered_listing_rows, parse_listing_html


def read_csv(path):
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# Load isolated copies without reading deployment settings, .env, or secrets.
settings = ModuleType("common.config")
settings.ACCOUNT_NAME = "Mediamarkt"
settings.COUNTRY = "SEG"
settings.PAGE_TYPE = "main"
settings.ensure_dirs = lambda *paths: [Path(p).mkdir(parents=True, exist_ok=True) for p in paths]
settings.read_csv = read_csv
settings.write_json = write_json
settings.env_value = lambda key, default=None: default
settings.db_config = Mock(side_effect=AssertionError("Real DB settings must not be read"))


def load_isolated(name):
    spec = importlib.util.spec_from_file_location(f"discount_test_{name}", MMKT_ROOT / "common" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"common.config": settings}):
        spec.loader.exec_module(module)
    return module


full_output = load_isolated("full_output")
db_save = load_isolated("db_save")
notify = load_isolated("notify")


def cfg_for(root, product="tv"):
    return SimpleNamespace(OUTPUT_ROOT=root, PRODUCT=product.upper(), SPEC_FIELDS=["test_spec"],
                           MAIN_TARGET_UNIQUE=1, BSR_TARGET_RANK=1, DB_TABLE=("test", "retail"))


def save_with_fake_db(cfg):
    """Exercise the actual insert mapping and CSV rewrite, never a real socket."""
    driver = ModuleType("psycopg2")
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    driver.connect = Mock(return_value=connection)
    columns = ["item", "discount_type"]
    args = SimpleNamespace(product=cfg.PRODUCT.lower(), input="", dry_run=False)
    with patch.dict(sys.modules, {"psycopg2": driver}), \
            patch.object(db_save, "parse_args", return_value=args), \
            patch.object(db_save, "load_cfg", return_value=cfg), \
            patch.object(db_save, "db_config", return_value={"host": "example.invalid"}), \
            patch.object(db_save, "table_columns", return_value=columns), \
            patch.object(db_save, "safe_backfill_from_retail_history", return_value={
                "recovered_rows": 0, "recovered_fields": {}, "error": ""}), \
            patch("sys.stdout", new=io.StringIO()):
        result = db_save.main()
    return result, cursor.executemany.call_args.args[1]


class DiscountTranslationTests(unittest.TestCase):
    def test_all_existing_dictionary_labels_and_english_are_supported(self):
        for raw, english in TEXT_TRANSLATIONS.items():
            with self.subTest(raw=raw):
                self.assertEqual((english, []), translate_discount_type(raw))
                self.assertEqual((english, []), translate_discount_type(english))

    def test_variable_amounts_preserve_numbers_and_are_idempotent(self):
        cases = {
            "-50€ mit Kalibrierung": "-50€ with calibration",
            "-1.200,50 € mit Kalibrierung": "-1.200,50 € with calibration",
            "3,9% Finanzierung": "3,9% financing",
            "15 % Rabatt": "15 % discount",
            "100€ Rabatt": "100€ discount",
            "Bis zu 25% Rabatt": "Up to 25% discount",
        }
        for raw, english in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual((english, []), translate_discount_type(raw))
                self.assertEqual((english, []), translate_discount_type(english))

    def test_partial_unknown_deduplication_and_whitespace(self):
        self.assertEqual(
            ("Price champion ||| Free shipping", ["Sonderaktion"]),
            translate_discount_type(" PREISHELD ||| Sonderaktion ||| Preisheld ||| Gratis\u00a0 Versand ||| Sonderaktion"),
        )

    def test_all_unknown_empty_and_unapproved_english(self):
        for raw in (None, "", "   ", " ||| "):
            self.assertEqual((None, []), translate_discount_type(raw))
        for raw in ("Sonderaktion", "Surprise offer", "15% Rabatt nur heute"):
            self.assertEqual((None, [raw]), translate_discount_type(raw))

    def test_both_parsers_preserve_original_labels(self):
        for labels, english in [(["Preisheld", "Sonderaktion"], "Price champion"),
                                (["Sonderaktion"], None)]:
            with self.subTest(labels=labels):
                apollo = {
                    "page": {"__typename": "ProductListPage", "products": [{"productId": "111"}]},
                    "product": {"__typename": "GraphqlProduct", "id": "111", "title": "Test TV"},
                    "badges": {"__typename": "CofrBadgesFeature", "id": "111",
                               "computedBadges": [{"name": label} for label in labels]},
                }
                html = f'<script>window.__PRELOADED_STATE__ = {json.dumps({"apolloState": apollo})};</script>'
                state_row = parse_listing_html(html)[0]
                badges = "".join(f'<span data-test="mms-badge">{escape(label)}</span>' for label in labels)
                dom = ('<div id="mms-search-productlist"><ul role="list"><li><article>'
                       '<a href="/de/product/_test-111.html">Test TV</a>'
                       f'{badges}</article></li></ul></div>')
                dom_row = extract_rendered_listing_rows(dom)[0][0]
                for row in (state_row, dom_row):
                    self.assertEqual(" ||| ".join(labels), row["discount_type"])
                    self.assertEqual(english, row["discount_type_en"])


class DiscountPipelineTests(unittest.TestCase):
    def test_listing_to_csv_database_and_warning_email_for_all_products(self):
        for product in ("tv", "ref", "ldy"):
            with self.subTest(product=product), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cfg = cfg_for(root, product)
                sources = ["Preisheld", "Gratis Versand ||| Sonderaktion", "Sonderaktion", "", None]
                listing = [dict(sku_id=str(i), retailer_sku_name="Test Waschmaschine", rank=str(i),
                                position=str(i), discount_type=raw, discount_type_en="Stale German text",
                                product_url=f"https://example.invalid/{i}", batch_id="test")
                           for i, raw in enumerate(sources, 1)]
                listing[3]["discount_type_en"] = ""
                listing[4]["discount_type_en"] = "Deal of the day"  # Legacy _en-only row.
                bsr = [dict(listing[2], discount_type="Preisheld", position="1"),
                       dict(listing[2], sku_id="6", position="2", discount_type="Neue Aktion")]
                write_csv(root / "mmkt_listing_main.csv", listing)
                write_csv(root / "mmkt_listing_bsr.csv", bsr)
                write_csv(root / "mmkt_pdp_detail.csv", [
                    {"sku_id": str(i), "sku": f"MODEL{i}", "test_spec": "present"} for i in range(1, 7)])
                original_bytes = (root / "mmkt_listing_main.csv").read_bytes()
                for sort, count in (("main", 5), ("bsr", 2)):
                    write_json(root / f"mmkt_step01_listing_{sort}_manifest.json", {
                        "success": True, "stop_reason": "last_page", "written_rows": count})
                args = SimpleNamespace(product=product, listing="", bsr="", detail="", output="")
                with patch.object(full_output, "parse_args", return_value=args), \
                        patch.object(full_output, "load_cfg", return_value=cfg), \
                        patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(0, full_output.main())
                output = read_csv(root / "mmkt_full_output.csv")
                expected = ["Price champion", "Free shipping", "", "", "Deal of the day", ""]
                self.assertEqual(expected, [row["discount_type"] for row in output])
                diagnostics = json.loads((root / "step09_full_output_manifest.json").read_text(
                    encoding="utf-8"))["discount_translation"]
                self.assertEqual((3, 2), (diagnostics["rows_with_untranslated"], diagnostics["rows_all_untranslated"]))
                self.assertEqual(["2", "3", "6"], [item["sku_id"] for item in diagnostics["items"]])
                result, inserted = save_with_fake_db(cfg)
                self.assertEqual(0, result)
                self.assertEqual([(str(i), value or None) for i, value in enumerate(expected, 1)], inserted)
                self.assertEqual(expected, [r["discount_type"] for r in read_csv(root / "mmkt_full_output.csv")])
                self.assertEqual(original_bytes, (root / "mmkt_listing_main.csv").read_bytes())
                with patch.object(notify, "env_value", side_effect=lambda key, default=None:
                                  "1" if key == "SEG_EMAIL_NOTIFY" else default), \
                        patch.object(notify, "_send", return_value=(True, 1, None)) as send, \
                        patch("sys.stdout", new=io.StringIO()):
                    manifest = notify.run(cfg)
                self.assertTrue(manifest["sent"])
                send.assert_called_once()
                subject, body = send.call_args.args
                self.assertTrue(subject.startswith("[CHECK]"))
                self.assertIn("discount_type all untranslated -> NULL: 2 SKU(s)", body)
                self.assertIn("SKU=3 / NULL (전부 미번역) / 미번역 원문: Sonderaktion", body)
                self.assertIn("SKU=6 / NULL (전부 미번역) / 미번역 원문: Neue Aktion", body)
                self.assertIn("SKU=2 / 일부 미번역 제외", body)

    def test_direct_legacy_db_load_also_normalizes_and_reports_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = cfg_for(root)
            write_csv(root / "mmkt_full_output.csv", [
                {"item": "1", "discount_type": "Preisheld ||| Sonderaktion"},
                {"item": "2", "discount_type": "Neue Aktion"}])
            result, inserted = save_with_fake_db(cfg)
            self.assertEqual(0, result)
            self.assertEqual([("1", "Price champion"), ("2", None)], inserted)
            self.assertEqual(["Price champion", ""], [r["discount_type"] for r in read_csv(root / "mmkt_full_output.csv")])
            manifest = json.loads((root / "step14_db_save_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("Neue Aktion", manifest["discount_translation"]["items"][1]["raw"])
            subject, body = notify.build_report(cfg, read_csv(root / "mmkt_full_output.csv"))
            self.assertTrue(subject.startswith("[CHECK]"))
            self.assertIn("SKU=2 / NULL (전부 미번역)", body)

    def test_no_false_alert_for_no_badges_or_partially_translated_sku(self):
        for has_unknown in (False, True):
            with self.subTest(partial=has_unknown), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cfg = cfg_for(root)
                rows = [{"item": "1", "main_rank": "1", "bsr_rank": "1", "sku": "MODEL",
                         "test_spec": "present", "discount_type": "Price champion" if has_unknown else ""}]
                items = [{"sku_id": "1", "untranslated": ["Sonderaktion"], "all_untranslated": False}] if has_unknown else []
                # A stale SKU not in the current CSV must not generate an alert.
                items.append({"sku_id": "old", "untranslated": ["Alt"], "all_untranslated": True})
                for step in ("09_full_output", "14_db_save"):
                    write_json(root / f"step{step}_manifest.json", {"discount_translation": {"items": items}})
                subject, body = notify.build_report(cfg, rows)
                self.assertFalse(subject.startswith("[CHECK]"))
                self.assertNotIn("SKU=old", body)
                if has_unknown:
                    self.assertEqual(1, body.count("SKU=1 /"))

    def test_previous_batch_does_not_override_current_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = cfg_for(root)
            rows = [{"item": "1", "batch_id": "current", "main_rank": "1", "bsr_rank": "1",
                     "sku": "MODEL", "test_spec": "present", "discount_type": "Price champion"}]
            write_json(root / "step14_db_save_manifest.json", {
                "batch_ids": ["previous"], "discount_translation": {"items": [
                    {"sku_id": "1", "untranslated": ["Sonderaktion"], "all_untranslated": True}]}})
            subject, body = notify.build_report(cfg, rows)
            self.assertFalse(subject.startswith("[CHECK]"))
            self.assertNotIn("Sonderaktion", body)

    def test_dry_run_never_sends_email(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = cfg_for(Path(directory))
            with patch.object(notify, "env_value", side_effect=lambda key, default=None:
                              "1" if key in {"SEG_EMAIL_NOTIFY", "SEG_EMAIL_DRY_RUN"} else default), \
                    patch.object(notify, "_send") as send, patch("sys.stdout", new=io.StringIO()):
                manifest = notify.run(cfg)
            self.assertFalse(manifest["sent"])
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
