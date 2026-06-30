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
from urllib.parse import quote, urlencode, urljoin
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
SPONSORED_SLOT_POSITIONS = tuple(
    int(v) for v in os.getenv("OTTO_SPONSORED_SLOT_POSITIONS", "1,2,8,16,24,32,40,48,56,64,72").split(",") if v.strip()
)
LABEL_TRANSLATIONS = {"Sehr beliebt": "Very popular", "Gesponsert": "Sponsored", "Deal des Monats": "Deal of the month", "nur für kurze Zeit": "Only for a short time"}


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


def fetch_json(url: str, referer: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 2) -> tuple[Any, dict[str, Any]]:
    last_err = None
    for attempt in range(retries + 1):
        started = time.perf_counter()
        try:
            with urlopen(Request(url, headers=_headers(referer), method="GET"), timeout=timeout) as resp:
                body = resp.read()
                meta = {"http_status": resp.status, "body_bytes": len(body), "body_sha1": hashlib.sha1(body).hexdigest(), "elapsed_seconds": round(time.perf_counter() - started, 3)}
                return json.loads(body.decode("utf-8")), meta
        except (HTTPError, URLError, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"listing fetch failed for {url}: {last_err!r}")


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
    rows: list[dict[str, Any]] = []
    everglades_meta = []
    organic_per_page = LISTING_POSITIONS_PER_PAGE - len(SPONSORED_SLOT_POSITIONS)
    for page in range(1, LISTING_PAGES_TO_COLLECT + 1):
        offset = (page - 1) * organic_per_page
        data, meta = fetch_json(build_everglades_url(cfg.SUCHBEGRIFF, offset), cfg.WARMUP_LISTING_URL)
        rows.extend(compose_rows(page, offset, data))
        everglades_meta.append({"page": page, "offset": offset, **{k: meta.get(k) for k in ("http_status", "body_bytes", "elapsed_seconds")}})
        if REQUEST_SLEEP > 0:
            time.sleep(REQUEST_SLEEP)
    assign_exposure(rows)

    vids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        v = _text(r.get("variation_id"))
        if v and v not in seen:
            seen.add(v); vids.append(v)
    tiles: dict[str, dict[str, Any]] = {}
    croc_meta = []
    for i in range(0, len(vids), CROCOTILE_BATCH_SIZE):
        batch = vids[i:i + CROCOTILE_BATCH_SIZE]
        url = f"{CROCOTILE_URL}?variationIds=" + ",".join(quote(v, safe="") for v in batch)
        data, meta = fetch_json(url, cfg.WARMUP_LISTING_URL)
        for item in (data if isinstance(data, list) else []):
            if isinstance(item, dict) and _text(item.get("variationId")):
                tiles[_text(item.get("variationId"))] = item
        croc_meta.append({"requested": len(batch), "returned": len(data) if isinstance(data, list) else 0})
        if REQUEST_SLEEP > 0:
            time.sleep(REQUEST_SLEEP)

    for r in rows:
        tile = tiles.get(str(r.get("variation_id") or ""))
        if tile:
            r.update({k: v for k, v in crocotile_fields(tile, r.get("retailer_sku_name")).items() if v not in (None, "")})

    listing_csv = out / "otto_listing_topseller_rows.csv"
    write_csv(listing_csv, rows)
    manifest = {
        "run_type": "listing", "product": cfg.PRODUCT, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "suchbegriff": cfg.SUCHBEGRIFF, "listing_pages_collected": LISTING_PAGES_TO_COLLECT,
        "listing_positions_per_page": LISTING_POSITIONS_PER_PAGE, "listing_rows": len(rows),
        "unique_variation_ids": len(vids), "crocotile_returned": len(tiles),
        "everglades_requests": everglades_meta, "crocotile_requests": croc_meta,
        "output": str(listing_csv),
    }
    write_json(out / "step01_listing_manifest.json", manifest)
    print(f"[listing/{cfg.PRODUCT}] rows={len(rows)} unique={len(vids)} crocotile={len(tiles)} output={listing_csv}")
    return manifest
