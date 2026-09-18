"""Offline coverage of sticker extraction, retention, output and notification."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import discount_stickers as stickers
from common import db_save, full_output, listing, notify, parsers, targets
from common.io_util import read_csv, write_json
from common.translate import translate_discount_type


KNOWN = "https://i.otto.de/i/otto/mpp360_130782_162312"
UNKNOWN = "https://i.otto.de/i/otto/test_new_sticker"


class DiscountStickerTests(unittest.TestCase):
    def test_verified_assets_and_periods(self):
        expected = {
            "mpp360_429990_995702": "Deal & Win", "mpp360_413505_922953": "Deal & Win",
            "mpp360_413504_922954": "Deal & Win", "mpp360_130782_162312": "Deal of the month",
            "mpp360_130783_162313": "Deal of the month", "mpp360_130598_161988": "Deal of the week",
            "mpp360_134491_167816": "Our Hero", "mpp360_435551_1031782": "Our Hero",
            "mpp360_258249_414645": "Premium Hero", "mpp360_292558_476572": "Tech Highlights",
        }
        for asset, english in expected.items():
            with self.subTest(asset=asset):
                result = listing.crocotile_fields({"deal": {"image": {
                    "src": f"https://i.otto.de/i/otto/{asset}?w=80"},
                    "highlight": "nur bis Dienstag", "dealId": "deal-test"}}, "TV")
                self.assertEqual(english, result["discount_type"])
                self.assertEqual(stickers.STICKER_LABELS[asset], result["discount_type_raw"])
                self.assertEqual("deal-test", result["discount_type_deal_id"])
        for period in ("nur bis Dienstag", "nur diesen Monat", "nur für kurze Zeit", "Neues Angebot"):
            self.assertIsNone(translate_discount_type(period))
            self.assertIsNone(listing.crocotile_fields({"deal": {"highlight": period}}, None)["discount_type"])

    def test_unknown_and_absent_are_distinct_and_untrusted_not_downloaded(self):
        self.assertEqual("none", stickers.sticker_fields(None)["discount_sticker_status"])
        unknown = stickers.sticker_fields(UNKNOWN, "id")
        self.assertIsNone(unknown["discount_type"])
        self.assertEqual(UNKNOWN, unknown["discount_type_image_url"])
        with patch.object(stickers, "urlopen") as fetch:
            self.assertEqual((None, "unsupported_image_url"),
                             stickers.archive_unknown_image("https://example.com/image", Path("unused")))
            fetch.assert_not_called()

    def test_cache_deduplicates_images_across_queries_and_runs(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "image/png"}
        response.read.return_value = b"fake-test-image"
        with tempfile.TemporaryDirectory() as directory, patch.object(stickers, "urlopen", return_value=response) as fetch:
            rows = [dict(variation_id=sku, **stickers.sticker_fields(url)) for sku, url in
                    [("a", UNKNOWN), ("b", UNKNOWN + "?w=80"), ("a", UNKNOWN)]]
            result = stickers.sticker_diagnostics(rows, cache_dir=Path(directory))
            again = stickers.sticker_diagnostics(rows, cache_dir=Path(directory))
            self.assertEqual(1, result["unknown_image_count"])
            self.assertEqual(2, result["unknown_sku_count"])
            self.assertEqual(result, again)
            self.assertEqual(b"fake-test-image", Path(rows[0]["discount_type_image_file"]).read_bytes())
            fetch.assert_called_once()

    def test_download_failure_and_invalid_content_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(stickers, "urlopen", side_effect=TimeoutError):
            result = stickers.sticker_diagnostics([dict(variation_id="a", **stickers.sticker_fields(UNKNOWN))],
                                                  cache_dir=Path(directory))
            self.assertEqual("TimeoutError", result["unknown_images"][0]["image_error"])
        for mime, body, expected in [("text/html", b"html", "not_an_image"),
                                      ("image/png", b"", "empty_or_oversized_image"),
                                      ("image/png", b"x" * (stickers._MAX_IMAGE_BYTES + 1), "empty_or_oversized_image")]:
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers = {"Content-Type": mime}
            response.read.return_value = body
            with tempfile.TemporaryDirectory() as directory, patch.object(stickers, "urlopen", return_value=response):
                self.assertEqual((None, expected), stickers.archive_unknown_image(UNKNOWN, Path(directory)))

    def test_legacy_values_and_mapping_refresh(self):
        row = {"discount_type_raw": "nur bis Dienstag", "discount_type": "Only until Tuesday"}
        stickers.refresh_sticker_fields(row)
        self.assertIsNone(row["discount_type"])
        self.assertEqual("nur bis Dienstag", row["discount_type_legacy_raw"])
        self.assertEqual("legacy_missing_image", row["discount_sticker_status"])
        row["discount_type_image_url"] = KNOWN
        stickers.refresh_sticker_fields(row)
        self.assertEqual("Deal of the month", row["discount_type"])

    def test_html_parser_uses_image_without_alt_text(self):
        from bs4 import BeautifulSoup
        tile = BeautifulSoup(f'<div><img class="reptile-tile__deal-badge-image" src="{KNOWN}">'
                             '<span>nur bis Dienstag</span></div>', "html.parser").div
        self.assertEqual("Deal of the month", parsers.extract_listing_labels(tile)["discount_type"])

    def test_report_legacy_and_mismatched_listing_run(self):
        cfg = SimpleNamespace(PRODUCT="TV", SPEC_FIELDS=[])
        rows = [{"batch_id": "test", "main_rank": "1", "bsr_rank": "1"}]
        with tempfile.TemporaryDirectory() as directory, patch.object(notify, "category_output_root", return_value=Path(directory)):
            out = Path(directory)
            write_json(out / "step09_full_output_manifest.json", {
                "batch_id": "test", "discount_sticker_run_ids": ["new"],
                "discount_stickers": {"legacy_items": [{"sku_id": "legacy-sku", "legacy_raw": "nur heute"}]}})
            write_json(out / "step01_listing_manifest.json", {
                "success": True, "run_id": "old", "discount_stickers": stickers.sticker_diagnostics([
                    dict(variation_id="old-sku", **stickers.sticker_fields(UNKNOWN))])})
            body = notify.build_report(cfg, rows)[1]
            self.assertIn("listing 재수집 필요", body)
            self.assertIn("legacy-sku", body)
            self.assertNotIn("old-sku", body)

    def test_pipeline_preserves_raw_and_emits_english_or_null_with_one_report(self):
        cfg = SimpleNamespace(PRODUCT="TV", ACCOUNT_NAME="OTTO", COUNTRY="DE", SPEC_FIELDS=[],
                              SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL="https://www.otto.de/suche/fernseher/",
                              USE_DATASHEET=False, classify=lambda name: (True, "test"),
                              extract_spec=lambda *args, **kwargs: {})
        products = [{"id": "p" + str(i), "bestVariationId": "v" + str(i),
                     "variationPath": f"/p/test-{i}/", "name": f"Test TV {i}"} for i in range(4)]
        tiles = [{"variationId": "v" + str(i), "deal": {"image": {"src": url}, "highlight": "nur diesen Monat"}}
                 for i, url in enumerate([KNOWN, UNKNOWN, UNKNOWN, None])]
        def fetch(url, *args, **kwargs):
            return ({"intents": [{"intent": "ranked", "products": products}]} if url.startswith(listing.EVERGLADES_URL)
                    else tiles), {"http_status": 200}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            out = Path(directory)
            stack.enter_context(patch.object(listing, "ensure_dirs", return_value=out))
            stack.enter_context(patch.object(listing, "REFERENCES_ROOT", out))
            api = stack.enter_context(patch.object(listing, "fetch_json", side_effect=fetch))
            stack.enter_context(patch.object(listing, "LISTING_PAGES_TO_COLLECT", 1))
            stack.enter_context(patch.object(listing, "LISTING_POSITIONS_PER_PAGE", 4))
            stack.enter_context(patch.object(listing, "SPONSORED_SLOT_POSITIONS", ()))
            stack.enter_context(patch.object(listing, "REQUEST_SLEEP", 0))
            image = stack.enter_context(patch.object(stickers, "urlopen", side_effect=TimeoutError))
            for module in (targets, full_output, notify, db_save):
                stack.enter_context(patch.object(module, "category_output_root", return_value=out))
            stack.enter_context(patch.object(targets, "MAIN_TARGET_UNIQUE", 4))
            stack.enter_context(patch.object(targets, "BSR_TARGET_RANK", 4))
            stack.enter_context(patch.object(full_output, "fetch_similar_product_names", return_value={}))
            stack.enter_context(patch.object(full_output, "collect_review", return_value={}))
            stack.enter_context(patch.object(notify, "env_value", side_effect=lambda name, default=None:
                                           "1" if name == "SEG_EMAIL_NOTIFY" else "0"))
            send = stack.enter_context(patch.object(notify, "_send", return_value=(True, 1, None)))
            log = stack.enter_context(redirect_stdout(io.StringIO()))
            listing.run(cfg)
            self.assertEqual(2, api.call_count)
            image.assert_called_once()
            targets.run(cfg)
            saved = read_csv(out / "otto_final_targets.csv")
            self.assertEqual("Deal des Monats", saved[0]["discount_type_raw"])
            self.assertEqual(UNKNOWN, saved[1]["discount_type_image_url"])
            full_output.run(cfg, pdp_supplement="none", detail_sleep=0)
            final = read_csv(out / "otto_full_output.csv")
            self.assertEqual(["Deal of the month", "", "", ""], [r["discount_type"] for r in final])
            self.assertEqual(["Deal of the month", None, None, None],
                             [db_save._empty_to_none(r["discount_type"], "discount_type") for r in final])
            cfg.DB_TABLE = "public.test_table"
            database = MagicMock()
            connection = database.connect.return_value
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [("discount_type",)]
            with patch.dict(sys.modules, {"psycopg2": database}), \
                    patch.object(db_save, "db_config", return_value={"host": "test.invalid"}), \
                    patch.object(db_save, "safe_backfill_from_retail_history", return_value={
                        "recovered_rows": 0, "recovered_fields": 0, "error": None}):
                db_save.run(cfg, dry_run=False)
            self.assertEqual([("Deal of the month",), (None,), (None,), (None,)],
                             cursor.executemany.call_args.args[1])
            notify.run(cfg)
            send.assert_called_once()
            subject, body = send.call_args.args
            self.assertIn("[확인필요]", subject)
            self.assertIn("미등록 할인 스티커 1종 / 영향 상품 2개", body)
            self.assertIn("SKU=v1", body)
            self.assertIn("SKU=v2", body)
            self.assertIn(UNKNOWN, body)
            self.assertIn("TimeoutError", body)
            self.assertIn("[WARN][discount_type]", log.getvalue())

            # Resolved mappings must not resurrect old listing warnings.
            with patch.dict(stickers.STICKER_LABELS, {"test_new_sticker": "Unser Hero"}):
                full_output.run(cfg, pdp_supplement="none", detail_sleep=0)
                refreshed = read_csv(out / "otto_full_output.csv")
                self.assertEqual("Our Hero", refreshed[1]["discount_type"])
                self.assertNotIn("미등록 할인 스티커", notify.build_report(cfg, refreshed)[1])

            # Old full-output batch diagnostics are not included in another batch.
            for row in final:
                row["batch_id"] = "different-batch"
            self.assertNotIn("미등록 할인 스티커", notify.build_report(cfg, final)[1])


if __name__ == "__main__":
    unittest.main()
