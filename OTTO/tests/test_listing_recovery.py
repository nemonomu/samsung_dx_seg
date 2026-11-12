from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import listing  # noqa: E402
from common.io_util import read_csv  # noqa: E402


def topseller_html(offset, cards, has_next=False):
    tiles = "".join(
        f'<div class="new-layout" data-qa="reptile-product-tile" data-list-position="{rank}" '
        f'data-product-id="{pid}" data-variation-id="{vid}" {origin}>'
        f'<a href="/p/item-{pid}/">Test TV</a></div>'
        for rank, pid, vid, origin in cards
    )
    disabled = "" if has_next else " disabled"
    return (f'<main data-qa="reptile-search-result" '
            f'data-page=\'{{"sortiertnach":"topseller","o":"{offset}"}}\'>'
            f'{tiles}<div data-qa="reptile-paging-next"><button{disabled}></button></div></main>').encode()


def variation_ids_from_url(url: str) -> list[str]:
    value = parse_qs(urlsplit(url).query)["variationIds"][0]
    return value.split(",") if value else []


class StubListingFetchError(listing.ListingFetchError):
    """Interface-only error used to isolate crocotile splitting from urllib."""

    def __init__(self, status: int | None, body_excerpt: str = "test response") -> None:
        RuntimeError.__init__(self, f"HTTP {status}: {body_excerpt}")
        self.status = status
        self.body_excerpt = body_excerpt

    def as_dict(self) -> dict[str, object]:
        return {"http_status": self.status, "response_body_excerpt": self.body_excerpt}


class OttoListingRecoveryTests(unittest.TestCase):
    REFERER = "https://www.otto.de/suche/fernseher/"

    def test_api_fallback_after_screen_request_fails(self):
        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        error = listing.ListingFetchError("https://www.otto.de/", status=503)
        api = {"intents": [{"intent": "ranked", "products": [
            {"id": "123", "bestVariationId": "v1", "name": "Test TV", "variationPath": "/p/item-123/"}]}]}
        def fetch(url, *args, **kwargs):
            if url.startswith(listing.EVERGLADES_URL):
                return api, {"http_status": 200}
            return [{"variationId": "v1"}], {"http_status": 200}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(listing, "ensure_dirs", return_value=out), \
                    patch.object(listing, "fetch_topseller_page", side_effect=error), \
                    patch.object(listing, "fetch_json", side_effect=fetch), \
                    patch.object(listing, "LISTING_PAGES_TO_COLLECT", 1), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
            rows = read_csv(out / "otto_listing_topseller_rows.csv")
        self.assertEqual([(r["source"], r["variation_id"]) for r in rows], [("topseller_api_fallback", "v1")])
        self.assertEqual(manifest["status"], "completed_with_warnings")
        self.assertEqual(manifest["listing_source"], "topseller_api_fallback")
        self.assertEqual(manifest["listing_pages_collected"], 1)
        self.assertTrue(any("final fallback" in w for w in manifest["listing_warnings"]))

    def test_api_fallback_keeps_ranked_and_sponsored_and_partial_page(self):
        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        api = {"intents": [
            {"intent": "ranked", "products": [{"id": "1", "bestVariationId": "v1", "name": "TV 1"},
                                               {"id": "2", "bestVariationId": "v2", "name": "TV 2"}]},
            {"intent": "sponsored", "products": [{"id": "3", "bestVariationId": "v3", "name": "TV 3"}]},
        ]}
        offsets = []
        def fetch(url, *args, **kwargs):
            if url.startswith(listing.EVERGLADES_URL):
                offsets.append(int(parse_qs(urlsplit(url).query)["ranked.offset"][0]))
                if len(offsets) == 2:
                    raise listing.ListingFetchError(url, status=503)
                return api, {"http_status": 200}
            return [{"variationId": vid} for vid in ("v1", "v2", "v3")], {"http_status": 200}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(listing, "ensure_dirs", return_value=out), \
                    patch.object(listing, "fetch_topseller_page", side_effect=listing.ListingFetchError("https://www.otto.de/", status=503)), \
                    patch.object(listing, "fetch_json", side_effect=fetch), \
                    patch.object(listing, "LISTING_PAGES_TO_COLLECT", 2), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
            rows = read_csv(out / "otto_listing_topseller_rows.csv")
        self.assertEqual(offsets, [0, 2])
        self.assertEqual([(r["variation_id"], r["exposure_type"]) for r in rows],
                         [("v1", "organic"), ("v2", "organic"), ("v3", "sponsored")])
        self.assertTrue(all(not r.get("display_rank") for r in rows))
        self.assertTrue(any("fallback page 2" in w for w in manifest["listing_warnings"]))

    def test_later_page_http_failure_keeps_earlier_products(self):
        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        row = {"display_rank": 1, "product_id": "a", "variation_id": "v1",
               "exposure_type": "organic", "retailer_sku_name": "Test TV"}
        error = listing.ListingFetchError("https://www.otto.de/", status=503)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(listing, "ensure_dirs", return_value=out), \
                    patch.object(listing, "fetch_topseller_page", side_effect=[(([row], True), {}), error]), \
                    patch.object(listing, "fetch_json", return_value=([{"variationId": "v1"}], {"http_status": 200})), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
            saved = read_csv(out / "otto_listing_topseller_rows.csv")
        self.assertEqual([r["variation_id"] for r in saved], ["v1"])
        self.assertEqual(manifest["status"], "completed_with_warnings")
        self.assertTrue(any("HTTP 503" in warning for warning in manifest["listing_warnings"]))

    def test_empty_or_unrecognized_page_is_retried(self):
        for html in (b"<html></html>", topseller_html(0, [])):
            with self.subTest(html=html):
                rows, retry = listing.parse_topseller_page(html, 0)
                self.assertEqual(rows, [])
                self.assertTrue(retry)

    def test_sponsored_only_page_warns_and_retries_without_stopping(self):
        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        ad = {"display_rank": 1, "product_id": "a", "variation_id": "v1",
              "exposure_type": "sponsored", "retailer_sku_name": "Test TV"}
        organic = {"display_rank": 2, "product_id": "b", "variation_id": "v2",
                   "exposure_type": "organic", "retailer_sku_name": "Test TV"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(listing, "ensure_dirs", return_value=Path(directory)), \
                    patch.object(listing, "fetch_topseller_page", side_effect=[(([ad], True), {}), (([organic], False), {})]) as pages, \
                    patch.object(listing, "fetch_json", return_value=([], {"http_status": 200})), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
        self.assertEqual(pages.call_count, 2)
        self.assertEqual(manifest["listing_rows"], 2)
        self.assertTrue(any("cannot advance" in warning for warning in manifest["listing_warnings"]))

    def test_run_uses_display_positions_and_advances_by_organic_cards(self):
        ad = 'data-origin="sponsored"'
        pages = {
            0: topseller_html(0, [(1, "a", "v1", ""), (2, "b", "v2", ad),
                                  (3, "c", "v3", ad), (4, "d", "v4", "")], has_next=True),
            2: topseller_html(2, [(5, "b", "v2", ""), (6, "e", "v5", ad)]),
        }
        offsets = []

        def fetch(request, **kwargs):
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["sortiertnach"], ["topseller"])
            self.assertEqual(request.get_header("Accept-encoding"), "gzip")
            offset = int(query["o"][0])
            offsets.append(offset)
            return nullcontext(SimpleNamespace(read=lambda: gzip.compress(pages[offset]), status=200,
                                               headers={"Content-Encoding": "gzip"}))

        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(listing, "ensure_dirs", return_value=out), \
                    patch.object(listing, "urlopen", side_effect=fetch), \
                    patch.object(listing, "fetch_json", return_value=([], {"http_status": 200})) as details, \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
            rows = read_csv(out / "otto_listing_topseller_rows.csv")
        self.assertEqual("completed_with_warnings", manifest["status"])
        self.assertTrue(manifest["success"])
        self.assertFalse(manifest["crocotile_complete"])
        self.assertEqual(["v1", "v2", "v3", "v4", "v5"], manifest["crocotile_failed_ids"])
        self.assertEqual(offsets, [0, 2])
        self.assertEqual([(row["display_rank"], row["variation_id"], row["sku_status"]) for row in rows],
                         [("1", "v1", ""), ("2", "v2", "Sponsored"), ("3", "v3", "Sponsored"),
                          ("4", "v4", ""), ("5", "v2", ""), ("6", "v5", "Sponsored")])
        self.assertTrue(rows[4]["product_url"].endswith("?variationId=v2"))
        self.assertEqual(variation_ids_from_url(details.call_args.args[0]), ["v1", "v2", "v3", "v4", "v5"])

    def test_topseller_logs_position_and_offset_issues_and_keeps_cards(self):
        html = topseller_html(0, [(1, "a", "v1", "")])
        with patch("builtins.print") as warning:
            rows, _ = listing.parse_topseller_page(html, 109)
            self.assertEqual(rows[0]["variation_id"], "v1")
            rows, _ = listing.parse_topseller_page(html.replace(b'data-list-position="1"', b''), 0)
            self.assertEqual(rows[0]["variation_id"], "v1")
            self.assertIsNone(rows[0]["display_rank"])
        self.assertGreaterEqual(warning.call_count, 2)

    def test_unexpected_option_is_reported_but_requested_option_is_kept(self):
        cfg = SimpleNamespace(PRODUCT="TV", SUCHBEGRIFF="fernseher", WARMUP_LISTING_URL=self.REFERER)
        page_rows = [{"display_rank": 1, "product_id": "p", "variation_id": "v1",
                      "exposure_type": "organic", "retailer_sku_name": "Test TV"}]
        response = [{"variationId": "v1", "price": {"retailPrice": 100}}, {"variationId": "other"}]
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(listing, "ensure_dirs", return_value=out), \
                    patch.object(listing, "fetch_topseller_page", return_value=((page_rows, False), {})), \
                    patch.object(listing, "fetch_json", return_value=(response, {"http_status": 200})), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch.object(listing, "sticker_diagnostics", return_value={"unknown_image_count": 0}):
                manifest = listing.run(cfg)
            rows = read_csv(out / "otto_listing_topseller_rows.csv")
        self.assertTrue(manifest["success"])
        self.assertTrue(manifest["crocotile_complete"])
        self.assertEqual("completed_with_warnings", manifest["status"])
        self.assertEqual(["other"], manifest["crocotile_response_anomalies"][0]["unexpected_ids"])
        self.assertTrue(rows[0]["final_sku_price"])

    def test_missing_next_control_logs_and_attempts_next_page(self):
        html = topseller_html(0, [(1, "a", "v1", "")])
        html = html.replace(b'reptile-paging-next', b'renamed-pager')
        with patch("builtins.print") as warning:
            rows, has_next = listing.parse_topseller_page(html, 0)
        self.assertTrue(has_next)
        self.assertEqual(rows[0]["variation_id"], "v1")
        warning.assert_called()
        last = html.replace(b'renamed-pager', b'reptile-paging-current')
        self.assertFalse(listing.parse_topseller_page(last, 0)[1])

    def assert_diagnostic_shape(self, diagnostic: dict[str, object]) -> None:
        self.assertTrue(
            {
                "requested",
                "returned",
                "http_status",
                "outcome",
                "split_depth",
                "variation_ids",
            }.issubset(diagnostic),
            diagnostic,
        )

    def test_fetch_json_http_400_preserves_body_and_does_not_retry(self) -> None:
        url = f"{listing.CROCOTILE_URL}?variationIds=v1,v2"
        body = b'{"message":"variationIds accepts a maximum of 50 values"}'
        http_error = HTTPError(
            url,
            400,
            "Bad Request",
            {"Content-Type": "application/json; charset=utf-8"},
            io.BytesIO(body),
        )

        with patch.object(listing, "urlopen", side_effect=http_error) as mocked_open, \
                patch.object(listing.time, "sleep") as mocked_sleep, \
                self.assertRaises(listing.ListingFetchError) as raised:
            listing.fetch_json(url, self.REFERER, retries=3)

        error = raised.exception
        self.assertEqual(400, error.status)
        self.assertIn("maximum of 50", error.body_excerpt)
        self.assertEqual(400, error.as_dict()["http_status"])
        self.assertIn("maximum of 50", str(error.as_dict()["response_body_excerpt"]))
        mocked_open.assert_called_once()
        mocked_sleep.assert_not_called()

    def test_crocotile_http_400_splits_four_into_two_and_recovers_all(self) -> None:
        ids = ["v1", "v2", "v3", "v4"]
        requested_batches: list[list[str]] = []
        request_headers: list[dict[str, str] | None] = []

        def fake_fetch(url: str, _referer: str, **_kwargs):
            batch = variation_ids_from_url(url)
            requested_batches.append(batch)
            request_headers.append(_kwargs.get("extra_headers"))
            if len(batch) > 2:
                raise StubListingFetchError(400, "too many variationIds")
            return ([{"variationId": variation_id} for variation_id in batch], {"http_status": 200})

        diagnostics: list[dict[str, object]] = []
        with patch.object(listing, "fetch_json", side_effect=fake_fetch):
            items, failed_ids = listing.fetch_crocotile_batch(ids, self.REFERER, diagnostics)

        self.assertEqual([ids, ids[:2], ids[2:]], requested_batches)
        self.assertEqual(ids, [item["variationId"] for item in items])
        self.assertEqual([], failed_ids)
        self.assertEqual([4, 2, 2], [entry["requested"] for entry in diagnostics])
        self.assertEqual([0, 1, 1], [entry["split_depth"] for entry in diagnostics])
        self.assertEqual(["split", "success", "success"], [entry["outcome"] for entry in diagnostics])
        self.assertEqual([listing.CROCOTILE_HEADERS] * 3, request_headers)
        for diagnostic in diagnostics:
            self.assert_diagnostic_shape(diagnostic)

    def test_crocotile_rejects_incomplete_and_ambiguous_success_responses(self) -> None:
        cases = (
            ([{"variationId": "a"}], ["a"], ["b"]),
            ([], [], ["a", "b"]),
            ([{"variationId": "a"}, {"variationId": "a"}], ["a", "a"], ["b"]),
            ([{"variationId": "a"}, {"variationId": "x"}], ["a"], ["b"]),
            ([{"variationId": "a"}, {"variationId": "b"}, {"variationId": "x"}], ["a", "b"], []),
            ([{"variationId": "a"}, None, {"variationId": "b"}], ["a", "b"], []),
        )
        for response, valid, failed in cases:
            with self.subTest(response=response), patch.object(listing, "fetch_json", return_value=(response, {"http_status": 200})):
                diagnostics = []
                items, failed_ids = listing.fetch_crocotile_batch(["a", "b"], self.REFERER, diagnostics)
                self.assertEqual(valid, [item["variationId"] for item in items])
                self.assertEqual(failed, failed_ids)
                self.assertNotEqual("success", diagnostics[-1]["outcome"])
                self.assertEqual(["x"] if {"variationId": "x"} in response else [],
                                 diagnostics[-1]["unexpected_ids"])
                self.assertEqual(1 if None in response else 0, diagnostics[-1]["invalid_rows"])

    def test_crocotile_isolates_bad_singleton_without_losing_good_ids(self) -> None:
        ids = ["good-1", "bad", "good-2", "good-3"]
        requested_batches: list[list[str]] = []

        def fake_fetch(url: str, _referer: str, **_kwargs):
            batch = variation_ids_from_url(url)
            requested_batches.append(batch)
            if "bad" in batch:
                raise StubListingFetchError(400, "invalid variationId: bad")
            return ([{"variationId": variation_id} for variation_id in batch], {"http_status": 200})

        diagnostics: list[dict[str, object]] = []
        with patch.object(listing, "fetch_json", side_effect=fake_fetch):
            items, failed_ids = listing.fetch_crocotile_batch(ids, self.REFERER, diagnostics)

        self.assertEqual(
            [ids, ids[:2], ["good-1"], ["bad"], ids[2:]],
            requested_batches,
        )
        self.assertEqual(["good-1", "good-2", "good-3"], [item["variationId"] for item in items])
        self.assertEqual(["bad"], failed_ids)
        self.assertEqual([0, 1, 2, 2, 1], [entry["split_depth"] for entry in diagnostics])
        singleton_failure = next(entry for entry in diagnostics if entry["variation_ids"] == ["bad"])
        self.assertEqual(400, singleton_failure["http_status"])
        self.assertEqual(0, singleton_failure["returned"])
        self.assertEqual("skipped_id", singleton_failure["outcome"])
        for diagnostic in diagnostics:
            self.assert_diagnostic_shape(diagnostic)

    def test_crocotile_non_400_error_is_not_split_and_is_propagated(self) -> None:
        ids = ["v1", "v2", "v3", "v4"]
        error = StubListingFetchError(503, "service unavailable")
        diagnostics: list[dict[str, object]] = []

        with patch.object(listing, "fetch_json", side_effect=error) as mocked_fetch, \
                self.assertRaises(StubListingFetchError) as raised:
            listing.fetch_crocotile_batch(ids, self.REFERER, diagnostics)

        self.assertIs(error, raised.exception)
        mocked_fetch.assert_called_once()
        self.assertEqual(1, len(diagnostics))
        self.assert_diagnostic_shape(diagnostics[0])
        self.assertEqual(ids, diagnostics[0]["variation_ids"])
        self.assertEqual(503, diagnostics[0]["http_status"])
        self.assertEqual(0, diagnostics[0]["split_depth"])
        self.assertNotEqual("split", diagnostics[0]["outcome"])

    def test_run_persists_failed_manifest_and_request_diagnostics(self) -> None:
        class FakeConfig:
            PRODUCT = "TV"
            SUCHBEGRIFF = "fernseher"
            WARMUP_LISTING_URL = "https://www.otto.de/suche/fernseher/"

        page_rows = [{"display_rank": 1, "product_id": "product-v1", "variation_id": "v1",
                      "exposure_type": "organic", "retailer_sku_name": "Example TV"}]
        crocotile_error = listing.ListingFetchError(
            f"{listing.CROCOTILE_URL}?variationIds=v1",
            status=503,
            body=b'{"message":"crocotile upstream maintenance"}',
        )

        output = OTTO_ROOT / "tests" / "_tmp_listing_recovery_output"
        self.assertFalse(output.exists(), f"test output path already exists: {output}")
        output.mkdir()
        try:
            with patch.object(listing, "ensure_dirs", return_value=output), \
                    patch.object(listing, "fetch_topseller_page", return_value=((page_rows, False), {"http_status": 200})), \
                    patch.object(listing, "fetch_json", side_effect=crocotile_error), \
                    patch.object(listing, "LISTING_PAGES_TO_COLLECT", 1), \
                    patch.object(listing, "CROCOTILE_BATCH_SIZE", 4), \
                    patch.object(listing, "REQUEST_SLEEP", 0), \
                    patch("sys.stdout", new=io.StringIO()), \
                    self.assertRaises(listing.ListingFetchError):
                listing.run(FakeConfig())

            manifest = json.loads(
                (output / "step01_listing_manifest.json").read_text(encoding="utf-8")
            )
            request_diagnostics = json.loads(
                (output / "step01_listing_request_diagnostics.json").read_text(encoding="utf-8")
            )

            self.assertEqual("failed", manifest["status"])
            self.assertFalse(manifest["success"])
            self.assertEqual("crocotile", manifest["failed_phase"])
            self.assertEqual(503, manifest["error"]["http_status"])
            self.assertIn(
                "crocotile upstream maintenance",
                manifest["error"]["response_body_excerpt"],
            )
            self.assertEqual("failed", request_diagnostics["status"])
            self.assertEqual("crocotile", request_diagnostics["phase"])
            self.assertEqual(1, len(request_diagnostics["attempts"]))
            self.assertEqual("failed", request_diagnostics["attempts"][0]["outcome"])
            self.assertEqual(503, request_diagnostics["attempts"][0]["http_status"])
        finally:
            (output / "step01_listing_manifest.json").unlink(missing_ok=True)
            (output / "step01_listing_request_diagnostics.json").unlink(missing_ok=True)
            output.rmdir()

    def test_all_400s_abort_at_singleton_failure_limit(self) -> None:
        ids = [f"v{index}" for index in range(1, 9)]
        requested_batches: list[list[str]] = []

        def fake_fetch(url: str, _referer: str, **_kwargs):
            batch = variation_ids_from_url(url)
            requested_batches.append(batch)
            raise StubListingFetchError(400, "request contract rejected")

        diagnostics: list[dict[str, object]] = []
        with patch.object(listing, "fetch_json", side_effect=fake_fetch), \
                patch.object(listing, "CROCOTILE_SINGLETON_FAILURE_LIMIT", 3), \
                patch("sys.stdout", new=io.StringIO()), \
                self.assertRaises(listing.ListingFetchError) as raised:
            listing.fetch_crocotile_batch(ids, self.REFERER, diagnostics)

        self.assertEqual(7, len(requested_batches))
        self.assertLess(len(requested_batches), (2 * len(ids)) - 1)
        self.assertEqual(len(requested_batches), len(diagnostics))
        self.assertEqual("aborted_systemic_400", diagnostics[-1]["outcome"])
        self.assertEqual(["v3"], diagnostics[-1]["variation_ids"])
        self.assertEqual(400, diagnostics[-1]["http_status"])
        self.assertIn("aborted after 3 singleton", raised.exception.body_excerpt)
        for diagnostic in diagnostics:
            self.assert_diagnostic_shape(diagnostic)


if __name__ == "__main__":
    unittest.main()
