"""Step01 (shared): collect an OTTO topseller listing via everglades + crocotile.

Category-driven: the everglades suchbegriff comes from cfg.SUCHBEGRIFF (umlauts
transliterated). All crocotile topInfos are stored generically as a top_infos JSON
column so each category can pick its spec fields (TV diagonal, REF/LDY capacity).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from common import translate
from common.io_util import category_output_root, ensure_dirs, write_csv, write_json

EVERGLADES_URL = "https://www.otto.de/everglades/products"
CROCOTILE_URL = "https://www.otto.de/crocotile/tile/data"
OTTO_BASE_URL = "https://www.otto.de"

LISTING_PAGES_TO_COLLECT = int(os.getenv("OTTO_LISTING_PAGES_TO_COLLECT", "5"))
LISTING_POSITIONS_PER_PAGE = int(os.getenv("OTTO_LISTING_POSITIONS_PER_PAGE", "120"))
DEFAULT_TIMEOUT = int(os.getenv("OTTO_LISTING_TIMEOUT", "45"))
REQUEST_SLEEP = float(os.getenv("OTTO_LISTING_REQUEST_SLEEP", "0.25"))
CROCOTILE_BATCH_SIZE = int(os.getenv("OTTO_CROCOTILE_BATCH_SIZE", "80"))
CROCOTILE_SINGLETON_FAILURE_LIMIT = int(os.getenv("OTTO_CROCOTILE_SINGLETON_FAILURE_LIMIT", "3"))
ERROR_BODY_EXCERPT_CHARS = int(os.getenv("OTTO_ERROR_BODY_EXCERPT_CHARS", "2000"))
CROCOTILE_HEADERS = {
    "crocotile-version": "2",
    "otto-feature": "tilelist@RepTile-Dundee",
}
SPONSORED_SLOT_POSITIONS = tuple(
    int(v) for v in os.getenv("OTTO_SPONSORED_SLOT_POSITIONS", "1,2,8,16,24,32,40,48,56,64,72").split(",") if v.strip()
)
LABEL_TRANSLATIONS = {"Sehr beliebt": "Very popular", "Gesponsert": "Sponsored", "Deal des Monats": "Deal of the month", "nur für kurze Zeit": "Only for a short time"}


class ListingFetchError(RuntimeError):
    """Structured listing request failure with a bounded response-body excerpt."""

    def __init__(
        self,
        url: str,
        *,
        status: int | None = None,
        body: bytes | str = b"",
        headers: dict[str, str] | None = None,
        cause: BaseException | None = None,
        attempts: int = 1,
    ) -> None:
        raw_body = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
        self.url = url
        self.endpoint = urlsplit(url)._replace(query="", fragment="").geturl()
        self.status = status
        self.headers = headers or {}
        self.cause = cause
        self.attempts = attempts
        self.body_excerpt = raw_body.decode("utf-8", errors="replace")[:ERROR_BODY_EXCERPT_CHARS]
        self.body_bytes = len(raw_body)
        self.body_sha1 = hashlib.sha1(raw_body).hexdigest() if raw_body else None
        detail = f"listing fetch failed status={status!r} endpoint={self.endpoint} attempts={attempts}"
        if self.body_excerpt:
            detail += f" response={self.body_excerpt!r}"
        elif cause is not None:
            detail += f" error={cause!r}"
        super().__init__(detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "http_status": self.status,
            "endpoint": self.endpoint,
            "url_length": len(self.url),
            "attempts": self.attempts,
            "error_type": type(self.cause).__name__ if self.cause is not None else type(self).__name__,
            "error": str(self.cause) if self.cause is not None else str(self),
            "response_content_type": self.headers.get("Content-Type") or self.headers.get("content-type"),
            "response_body_bytes": self.body_bytes,
            "response_body_sha1": self.body_sha1,
            "response_body_excerpt": self.body_excerpt or None,
        }


def _headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    return s or None


def _euro(value: Any) -> str | None:
    s = _text(value)
    if not s:
        return None
    return s if ("EUR" in s or "€" in s) else f"{s} €"


def _translate(value: Any) -> str | None:
    s = _text(value)
    return LABEL_TRANSLATIONS.get(s, s) if s else None


def _abs_url(path: Any) -> str | None:
    if not path:
        return None
    s = str(path)
    return s if s.startswith("http") else urljoin(OTTO_BASE_URL, s)


def fetch_json(
    url: str,
    referer: str,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 2,
    *,
    extra_headers: dict[str, str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    last_failure: ListingFetchError | None = None
    for attempt in range(retries + 1):
        started = time.perf_counter()
        body = b""
        status: int | None = None
        response_headers: dict[str, str] = {}
        try:
            request_headers = {**_headers(referer), **(extra_headers or {})}
            with urlopen(Request(url, headers=request_headers, method="GET"), timeout=timeout) as resp:
                body = resp.read()
                status = resp.status
                response_headers = dict(resp.headers.items())
        except HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            status = exc.code
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            last_failure = ListingFetchError(
                url,
                status=status,
                body=body,
                headers=response_headers,
                cause=exc,
                attempts=attempt + 1,
            )
        except URLError as exc:
            last_failure = ListingFetchError(url, cause=exc, attempts=attempt + 1)
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError as exc:
                last_failure = ListingFetchError(
                    url,
                    status=status,
                    body=body,
                    headers=response_headers,
                    cause=exc,
                    attempts=attempt + 1,
                )
            else:
                meta = {
                    "http_status": status,
                    "body_bytes": len(body),
                    "body_sha1": hashlib.sha1(body).hexdigest(),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
                return payload, meta

        assert last_failure is not None
        retryable = (
            last_failure.status is None
            or last_failure.status in (408, 425, 429)
            or last_failure.status >= 500
            or isinstance(last_failure.cause, json.JSONDecodeError)
        )
        if attempt >= retries or not retryable:
            break
        time.sleep(2 * (attempt + 1))

    assert last_failure is not None
    raise last_failure from last_failure.cause


def build_everglades_url(suchbegriff: str, offset: int) -> str:
    rule = f"(und.(suchbegriff.{suchbegriff}).(~.(v.1)))"
    params = [("rule", rule), ("intents", "ranked"), ("intents", "sponsored"), ("intents", "context"), ("ranked.offset", str(offset))]
    return EVERGLADES_URL + "?" + urlencode(params)


def products_for_intent(data: Any, intent: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for it in data.get("intents") or []:
        if isinstance(it, dict) and it.get("intent") == intent:
            return [p for p in (it.get("products") or []) if isinstance(p, dict)]
    return []


def compose_rows(page: int, offset: int, data: Any) -> list[dict[str, Any]]:
    ranked = products_for_intent(data, "ranked")
    sponsored = products_for_intent(data, "sponsored")
    sponsored_by_pos = {pos: sponsored[i] for i, pos in enumerate(SPONSORED_SLOT_POSITIONS) if i < len(sponsored)}
    rows: list[dict[str, Any]] = []
    ri = 0
    for local in range(1, LISTING_POSITIONS_PER_PAGE + 1):
        if local in sponsored_by_pos:
            product, exposure = sponsored_by_pos[local], "sponsored"
        else:
            if ri >= len(ranked):
                break
            product, exposure = ranked[ri], "organic"
            ri += 1
        vid = _text(product.get("bestVariationId") or product.get("id"))
        gpos = (page - 1) * LISTING_POSITIONS_PER_PAGE + local
        rows.append({
            "source": "topseller_api", "page_number": page, "page_offset": offset,
            "display_rank": gpos, "list_position": gpos, "row_index": gpos, "target_rank": gpos,
            "exposure_type": exposure, "is_listing_target": True,
            "product_id": _text(product.get("id")), "variation_id": vid,
            "origin": "sponsored" if exposure == "sponsored" else None,
            "product_url": _abs_url(product.get("variationPath")),
            "retailer_sku_name": _text(product.get("name")),
        })
    return rows


def _crocotile_url(variation_ids: list[str]) -> str:
    return f"{CROCOTILE_URL}?variationIds=" + ",".join(quote(v, safe="") for v in variation_ids)


def fetch_crocotile_batch(
    variation_ids: list[str],
    referer: str,
    diagnostics: list[dict[str, Any]],
    *,
    _split_depth: int = 0,
    _state: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch one Crocotile batch, bisecting HTTP 400 batches to isolate the cause.

    A reduced server-side batch limit recovers as two successful child requests. A
    single rejected variation ID is skipped and reported so other products remain
    collectable. Repeated singleton failures abort to avoid amplifying a systemic
    endpoint/header change into hundreds of requests.
    """
    ids = [str(v) for v in variation_ids if str(v)]
    if not ids:
        return [], []
    state = _state if _state is not None else {"singleton_400s": 0}
    url = _crocotile_url(ids)
    attempted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        data, meta = fetch_json(url, referer, extra_headers=CROCOTILE_HEADERS)
    except ListingFetchError as exc:
        diagnostic = {
            "attempted_at": attempted_at,
            "variation_ids": ids,
            "requested": len(ids),
            "returned": 0,
            "split_depth": _split_depth,
            **exc.as_dict(),
        }
        if exc.status != 400:
            diagnostic["outcome"] = "failed"
            diagnostics.append(diagnostic)
            raise
        if len(ids) > 1:
            diagnostic["outcome"] = "split"
            diagnostics.append(diagnostic)
            midpoint = len(ids) // 2
            print(
                f"[listing/crocotile][WARN] HTTP 400 requested={len(ids)} "
                f"split={midpoint}+{len(ids) - midpoint} depth={_split_depth}",
                flush=True,
            )
            left_items, left_failed = fetch_crocotile_batch(
                ids[:midpoint], referer, diagnostics, _split_depth=_split_depth + 1, _state=state
            )
            right_items, right_failed = fetch_crocotile_batch(
                ids[midpoint:], referer, diagnostics, _split_depth=_split_depth + 1, _state=state
            )
            return left_items + right_items, left_failed + right_failed

        state["singleton_400s"] += 1
        diagnostic["outcome"] = "skipped_id"
        diagnostics.append(diagnostic)
        print(
            f"[listing/crocotile][WARN] HTTP 400 isolated variation_id={ids[0]} "
            f"response={exc.body_excerpt[:200]!r}",
            flush=True,
        )
        if state["singleton_400s"] >= CROCOTILE_SINGLETON_FAILURE_LIMIT:
            diagnostic["outcome"] = "aborted_systemic_400"
            raise ListingFetchError(
                url,
                status=400,
                body=(
                    f"aborted after {state['singleton_400s']} singleton HTTP 400 responses; "
                    "the Crocotile endpoint or request contract may have changed. "
                    f"Last response: {exc.body_excerpt}"
                ),
                cause=exc,
            ) from exc
        return [], ids

    items = [item for item in (data if isinstance(data, list) else []) if isinstance(item, dict)]
    diagnostics.append({
        "attempted_at": attempted_at,
        "variation_ids": ids,
        "requested": len(ids),
        "returned": len(items),
        "split_depth": _split_depth,
        "outcome": "success",
        **{k: meta.get(k) for k in ("http_status", "body_bytes", "body_sha1", "elapsed_seconds")},
    })
    return items, []


def crocotile_fields(tile: dict[str, Any], fallback_name: Any) -> dict[str, Any]:
    price = tile.get("price") if isinstance(tile.get("price"), dict) else {}
    sale = tile.get("saleTags") if isinstance(tile.get("saleTags"), dict) else {}
    deal = tile.get("deal") if isinstance(tile.get("deal"), dict) else {}
    social = tile.get("socialProof") if isinstance(tile.get("socialProof"), dict) else {}
    reviews = tile.get("customerReviews") if isinstance(tile.get("customerReviews"), dict) else {}
    availability = tile.get("availability") if isinstance(tile.get("availability"), dict) else {}
    title = tile.get("title") if isinstance(tile.get("title"), dict) else {}
    energy = (tile.get("energyLabels") or [{}])[0] if isinstance(tile.get("energyLabels"), list) and tile.get("energyLabels") else {}
    top_infos = {i.get("label"): i.get("value") for i in (tile.get("topInfos") or []) if isinstance(i, dict) and i.get("label")}
    # title.shortened = clean "model type" without the trailing spec parenthetical
    # (e.g. "GU65U7099FU LED-Fernseher"); prepend the brand for the displayed name.
    name = _text(title.get("shortened")) or _text(tile.get("name")) or _text(fallback_name)
    brand = _text(tile.get("brand"))
    if name and brand and not name.casefold().startswith(brand.casefold()):
        name = f"{brand} {name}"
    popularity_raw = "Sehr beliebt" if social.get("popular") is True else None
    discount_raw = _text(deal.get("highlight"))
    return {
        "retailer_sku_name": name,
        "brand": brand,
        "final_sku_price": _euro(price.get("retailPrice")),
        # original price = UVP (suggestedRetailPrice) or, when absent, the former/
        # comparison price (comparativePrice) that the discount is computed against.
        "original_sku_price": _euro(price.get("suggestedRetailPrice") or price.get("comparativePrice")),
        "savings": _text(sale.get("discount")),
        "sku_popularity_raw": popularity_raw,
        "sku_popularity": translate.translate_popularity(popularity_raw),
        "discount_type_raw": discount_raw,
        "discount_type": translate.translate_discount_type(discount_raw),
        "delivery_availability_raw": _text(availability.get("detail")),
        "delivery_availability": translate.translate_delivery(availability.get("detail")),
        "count_of_reviews_listing": reviews.get("amount"),
        "average_rating_listing": reviews.get("averageRating"),
        "energy_efficiency_class": _text(energy.get("category")),
        "energy_label_uri": _text(energy.get("labelUri")),
        "energy_datasheet_uri": _text(energy.get("datasheetUri")),
        "top_infos": json.dumps(top_infos, ensure_ascii=False) if top_infos else None,
    }


def assign_exposure(rows: list[dict[str, Any]]) -> None:
    org = spo = 0
    for r in rows:
        if r.get("exposure_type") == "sponsored":
            spo += 1; r["sku_status_raw"] = "Gesponsert"; r["sku_status"] = "Sponsored"
        else:
            org += 1; r["sku_status_raw"] = None; r["sku_status"] = None


def run(cfg) -> dict[str, Any]:
    category = cfg.PRODUCT.lower()
    out = ensure_dirs(category)
    manifest_path = out / "step01_listing_manifest.json"
    diagnostics_path = out / "step01_listing_request_diagnostics.json"
    listing_csv = out / "otto_listing_topseller_rows.csv"
    started = datetime.now().astimezone()
    run_id = f"{cfg.PRODUCT.lower()}-listing-{started:%Y%m%dT%H%M%S%z}"
    base_manifest: dict[str, Any] = {
        "run_type": "listing",
        "run_id": run_id,
        "product": cfg.PRODUCT,
        "created_at": started.isoformat(timespec="seconds"),
        "suchbegriff": cfg.SUCHBEGRIFF,
        "listing_pages_requested": LISTING_PAGES_TO_COLLECT,
        "listing_positions_per_page": LISTING_POSITIONS_PER_PAGE,
        "crocotile_batch_size": CROCOTILE_BATCH_SIZE,
        "output": str(listing_csv),
        "request_diagnostics": str(diagnostics_path),
    }
    write_json(manifest_path, {**base_manifest, "status": "running", "success": None})
    write_json(diagnostics_path, {"run_id": run_id, "status": "running", "attempts": []})

    rows: list[dict[str, Any]] = []
    vids: list[str] = []
    tiles: dict[str, dict[str, Any]] = {}
    failed_ids: list[str] = []
    everglades_meta: list[dict[str, Any]] = []
    crocotile_diagnostics: list[dict[str, Any]] = []
    phase = "everglades"
    try:
        organic_per_page = LISTING_POSITIONS_PER_PAGE - len(SPONSORED_SLOT_POSITIONS)
        for page in range(1, LISTING_PAGES_TO_COLLECT + 1):
            offset = (page - 1) * organic_per_page
            data, meta = fetch_json(build_everglades_url(cfg.SUCHBEGRIFF, offset), cfg.WARMUP_LISTING_URL)
            rows.extend(compose_rows(page, offset, data))
            everglades_meta.append({
                "page": page,
                "offset": offset,
                **{k: meta.get(k) for k in ("http_status", "body_bytes", "elapsed_seconds")},
            })
            if REQUEST_SLEEP > 0:
                time.sleep(REQUEST_SLEEP)
        assign_exposure(rows)

        seen: set[str] = set()
        for row in rows:
            variation_id = _text(row.get("variation_id"))
            if variation_id and variation_id not in seen:
                seen.add(variation_id)
                vids.append(variation_id)

        phase = "crocotile"
        for i in range(0, len(vids), CROCOTILE_BATCH_SIZE):
            batch = vids[i:i + CROCOTILE_BATCH_SIZE]
            items, batch_failed_ids = fetch_crocotile_batch(
                batch, cfg.WARMUP_LISTING_URL, crocotile_diagnostics
            )
            failed_ids.extend(batch_failed_ids)
            for item in items:
                variation_id = _text(item.get("variationId"))
                if variation_id:
                    tiles[variation_id] = item
            if REQUEST_SLEEP > 0:
                time.sleep(REQUEST_SLEEP)

        phase = "write_output"
        for row in rows:
            tile = tiles.get(str(row.get("variation_id") or ""))
            if tile:
                row.update({
                    key: value
                    for key, value in crocotile_fields(tile, row.get("retailer_sku_name")).items()
                    if value not in (None, "")
                })

        write_csv(listing_csv, rows)
        finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        status = "completed_with_warnings" if failed_ids else "completed"
        write_json(diagnostics_path, {
            "run_id": run_id,
            "status": status,
            "attempts": crocotile_diagnostics,
        })
        successful_crocotile_requests = [
            {
                key: attempt.get(key)
                for key in ("requested", "returned", "http_status", "body_bytes", "elapsed_seconds", "split_depth")
            }
            for attempt in crocotile_diagnostics
            if attempt.get("outcome") == "success"
        ]
        manifest = {
            **base_manifest,
            "status": status,
            "success": True,
            "finished_at": finished_at,
            "listing_pages_collected": len(everglades_meta),
            "listing_rows": len(rows),
            "unique_variation_ids": len(vids),
            "crocotile_returned": len(tiles),
            "crocotile_complete": not failed_ids,
            "crocotile_failed_ids": failed_ids,
            "crocotile_attempt_count": len(crocotile_diagnostics),
            "crocotile_http_400_count": sum(
                1 for attempt in crocotile_diagnostics if attempt.get("http_status") == 400
            ),
            "everglades_requests": everglades_meta,
            "crocotile_requests": successful_crocotile_requests,
        }
        write_json(manifest_path, manifest)
        print(
            f"[listing/{cfg.PRODUCT}] status={status} rows={len(rows)} unique={len(vids)} "
            f"crocotile={len(tiles)} failed_ids={len(failed_ids)} output={listing_csv}",
            flush=True,
        )
        return manifest
    except Exception as exc:
        finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json(diagnostics_path, {
            "run_id": run_id,
            "status": "failed",
            "phase": phase,
            "attempts": crocotile_diagnostics,
        })
        error_detail = exc.as_dict() if isinstance(exc, ListingFetchError) else {
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        failure_manifest = {
            **base_manifest,
            "status": "failed",
            "success": False,
            "finished_at": finished_at,
            "failed_phase": phase,
            "error": error_detail,
            "listing_pages_collected": len(everglades_meta),
            "listing_rows_in_memory": len(rows),
            "unique_variation_ids": len(vids),
            "crocotile_returned": len(tiles),
            "crocotile_failed_ids": failed_ids,
            "crocotile_attempt_count": len(crocotile_diagnostics),
            "crocotile_http_400_count": sum(
                1 for attempt in crocotile_diagnostics if attempt.get("http_status") == 400
            ),
            "everglades_requests": everglades_meta,
        }
        write_json(manifest_path, failure_manifest)
        print(
            f"[listing/{cfg.PRODUCT}] FAILED phase={phase} error={type(exc).__name__}: {exc} "
            f"manifest={manifest_path}",
            flush=True,
        )
        raise
