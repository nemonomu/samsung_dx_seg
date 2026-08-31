from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

OTTO_ROOT = Path(__file__).resolve().parents[1]
if str(OTTO_ROOT) not in sys.path:
    sys.path.insert(0, str(OTTO_ROOT))

from common import listing  # noqa: E402


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

        everglades_payload = {
            "intents": [
                {
                    "intent": "ranked",
                    "products": [
                        {
                            "id": "product-v1",
                            "bestVariationId": "v1",
                            "variationPath": "/p/example/product-v1/",
                            "name": "Example TV",
                        }
                    ],
                }
            ]
        }
        crocotile_error = listing.ListingFetchError(
            f"{listing.CROCOTILE_URL}?variationIds=v1",
            status=503,
            body=b'{"message":"crocotile upstream maintenance"}',
        )

        def fake_fetch(url: str, _referer: str, **_kwargs):
            if url.startswith(listing.EVERGLADES_URL):
                return everglades_payload, {
                    "http_status": 200,
                    "body_bytes": 100,
                    "elapsed_seconds": 0.01,
                }
            raise crocotile_error

        output = OTTO_ROOT / "tests" / "_tmp_listing_recovery_output"
        self.assertFalse(output.exists(), f"test output path already exists: {output}")
        output.mkdir()
        try:
            with patch.object(listing, "ensure_dirs", return_value=output), \
                    patch.object(listing, "fetch_json", side_effect=fake_fetch), \
                    patch.object(listing, "LISTING_PAGES_TO_COLLECT", 1), \
                    patch.object(listing, "LISTING_POSITIONS_PER_PAGE", 1), \
                    patch.object(listing, "SPONSORED_SLOT_POSITIONS", ()), \
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
