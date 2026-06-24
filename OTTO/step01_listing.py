"""Step01: collect OTTO Topseller listing through direct REST/XHR APIs."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from step00_config import (
    BSR_TARGET_RANK,
    LISTING_PAGES_TO_COLLECT,
    LISTING_POSITIONS_PER_PAGE,
    MAIN_TARGET_UNIQUE,
    OUTPUT_ROOT,
    SEARCH_TERM,
    TOPSELLER_URL,
    ensure_dirs,
    write_csv,
    write_json,
)

EVERGLADES_URL = "https://www.otto.de/everglades/products"
CROCOTILE_URL = "https://www.otto.de/crocotile/tile/data"
OTTO_BASE_URL = "https://www.otto.de"
EVERGLADES_RULE = f"(und.(suchbegriff.{SEARCH_TERM}).(~.(v.1)))"

LISTING_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_rows.csv"
ORGANIC_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_organic_rows.csv"
SPONSORED_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_sponsored_rows.csv"
MAIN_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_main_capture_rows.csv"
BSR_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_bsr_capture_rows.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step01_listing_manifest.json"

DEFAULT_TIMEOUT = int(os.getenv("OTTO_LISTING_TIMEOUT", "45"))
REQUEST_SLEEP_SECONDS = float(os.getenv("OTTO_LISTING_REQUEST_SLEEP", "0.25"))
CROCOTILE_BATCH_SIZE = int(os.getenv("OTTO_CROCOTILE_BATCH_SIZE", "80"))
SPONSORED_SLOT_POSITIONS = tuple(
    int(value)
    for value in os.getenv("OTTO_SPONSORED_SLOT_POSITIONS", "1,2,8,16,24,32,40,48,56,64,72").split(",")
    if value.strip()
)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": TOPSELLER_URL,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

LISTING_FIELD_COVERAGE_FIELDS = [
    "product_url",
    "retailer_sku_name",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "sku_popularity",
    "sku_status",
    "discount_type",
    "delivery_availability_raw",
    "count_of_reviews_listing",
    "average_rating_listing",
]

LABEL_TRANSLATIONS = {
    "Sehr beliebt": "Very popular",
    "Gesponsert": "Sponsored",
    "Deal des Monats": "Deal of the month",
    "nur f\u00fcr kurze Zeit": "Only for a short time",
}

AVAILABILITY_TRANSLATIONS = {
    "lieferbar - am n\u00e4chsten Werktag bei dir": "Available - at your door the next working day",
}


def build_offsets(page_count: int) -> list[int]:
    organic_positions_per_page = LISTING_POSITIONS_PER_PAGE - len(SPONSORED_SLOT_POSITIONS)
    return [index * organic_positions_per_page for index in range(page_count)]


def build_everglades_url(offset: int) -> str:
    params = [
        ("rule", EVERGLADES_RULE),
        ("intents", "ranked"),
        ("intents", "sponsored"),
        ("intents", "context"),
        ("ranked.offset", str(offset)),
    ]
    return EVERGLADES_URL + "?" + urlencode(params)


def build_crocotile_url(variation_ids: list[str]) -> str:
    encoded_ids = ",".join(quote(value, safe="") for value in variation_ids)
    return f"{CROCOTILE_URL}?variationIds={encoded_ids}"


def fetch_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[Any, dict[str, Any]]:
    request = Request(url, headers=DEFAULT_HEADERS, method="GET")
    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            meta = {
                "url": url,
                "http_status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "body_bytes": len(body),
                "body_sha1": hashlib.sha1(body).hexdigest(),
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            }
            return json.loads(body.decode("utf-8")), meta
    except HTTPError as exc:
        body = exc.read()
        preview = body[:500].decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {preview}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc!r}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON for {url}: {exc!r}") from exc


def products_for_intent(data: Any, intent_name: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    for intent in data.get("intents") or []:
        if isinstance(intent, dict) and intent.get("intent") == intent_name:
            return [product for product in (intent.get("products") or []) if isinstance(product, dict)]
    return []


def product_id_without_prefix(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.startswith("C") and text[1:].isdigit() else text


def absolute_otto_url(path: Any) -> str | None:
    if not path:
        return None
    text = str(path)
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return urljoin(OTTO_BASE_URL, text)


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def format_euro(value: Any) -> str | None:
    text = text_or_none(value)
    if not text:
        return None
    if "EUR" in text or "\u20ac" in text:
        return text
    return f"{text} \u20ac"


def translate_label(value: Any) -> str | None:
    text = text_or_none(value)
    if not text:
        return None
    return LABEL_TRANSLATIONS.get(text, text)


def translate_availability(value: Any) -> str | None:
    text = text_or_none(value)
    if not text:
        return None
    if text in AVAILABILITY_TRANSLATIONS:
        return AVAILABILITY_TRANSLATIONS[text]
    match = re.fullmatch(r"lieferbar - in ([0-9]+(?:-[0-9]+)?) Werktagen bei dir", text)
    if match:
        return f"Available - at your door in {match.group(1)} working days"
    return text


def crocotile_name(tile: dict[str, Any], fallback: Any) -> str | None:
    title = tile.get("title") if isinstance(tile.get("title"), dict) else {}
    name = text_or_none(title.get("full")) or text_or_none(tile.get("name")) or text_or_none(fallback)
    brand = text_or_none(tile.get("brand"))
    if name and brand and not name.casefold().startswith(brand.casefold()):
        return f"{brand} {name}"
    return name


def selected_energy_label(tile: dict[str, Any]) -> dict[str, Any]:
    labels = tile.get("energyLabels")
    if isinstance(labels, list) and labels and isinstance(labels[0], dict):
        return labels[0]
    return {}


def selected_image(tile: dict[str, Any]) -> dict[str, Any]:
    image = tile.get("image")
    return image if isinstance(image, dict) else {}


def top_info_value(tile: dict[str, Any], label: str) -> str | None:
    infos = tile.get("topInfos")
    if not isinstance(infos, list):
        return None
    for item in infos:
        if isinstance(item, dict) and item.get("label") == label:
            return text_or_none(item.get("value"))
    return None


def crocotile_row_fields(tile: dict[str, Any], fallback_name: Any) -> dict[str, Any]:
    price = tile.get("price") if isinstance(tile.get("price"), dict) else {}
    sale_tags = tile.get("saleTags") if isinstance(tile.get("saleTags"), dict) else {}
    deal = tile.get("deal") if isinstance(tile.get("deal"), dict) else {}
    social = tile.get("socialProof") if isinstance(tile.get("socialProof"), dict) else {}
    reviews = tile.get("customerReviews") if isinstance(tile.get("customerReviews"), dict) else {}
    energy = selected_energy_label(tile)
    image = selected_image(tile)
    title = tile.get("title") if isinstance(tile.get("title"), dict) else {}
    availability = tile.get("availability") if isinstance(tile.get("availability"), dict) else {}
    discount_type_raw = text_or_none(deal.get("highlight"))
    popularity_raw = "Sehr beliebt" if social.get("popular") is True else None
    availability_raw = text_or_none(availability.get("detail"))

    return {
        "retailer_sku_name": crocotile_name(tile, fallback_name),
        "retailer_sku_name_without_brand": text_or_none(title.get("withoutBrand")),
        "retailer_sku_name_shortened": text_or_none(title.get("shortened")),
        "product_url": absolute_otto_url(tile.get("detailPageLink") or tile.get("canonicalLink")),
        "canonical_url": absolute_otto_url(tile.get("canonicalLink")),
        "final_sku_price": format_euro(price.get("retailPrice")),
        "original_sku_price": format_euro(price.get("suggestedRetailPrice")),
        "savings": text_or_none(sale_tags.get("discount")),
        "final_sku_price_text": format_euro(price.get("retailPrice")),
        "original_sku_price_text": format_euro(price.get("suggestedRetailPrice")),
        "savings_text": text_or_none(sale_tags.get("discount")),
        "price_sale_flag": price.get("sale"),
        "price_is_starting_price": price.get("isStartingPrice"),
        "sku_popularity_raw": popularity_raw,
        "sku_popularity": translate_label(popularity_raw),
        "discount_type_raw": discount_type_raw,
        "discount_type": translate_label(discount_type_raw),
        "delivery_availability_raw": availability_raw,
        "delivery_availability": translate_availability(availability_raw),
        "delivery_availability_state": text_or_none(availability.get("state")),
        "count_of_reviews_listing": reviews.get("amount"),
        "average_rating_listing": reviews.get("averageRating"),
        "energy_efficiency_class": text_or_none(energy.get("category")),
        "energy_label_uri": text_or_none(energy.get("labelUri")),
        "energy_datasheet_uri": text_or_none(energy.get("datasheetUri")),
        "image_url": text_or_none(image.get("jpeg") or image.get("webp")),
        "brand": text_or_none(tile.get("brand")),
        "pbk": text_or_none(tile.get("pbk")),
        "top_info_diagonal": top_info_value(tile, "Diagonale"),
        "top_info_screen_technology": top_info_value(tile, "Bildschirmtechnologie"),
        "top_info_resolution": top_info_value(tile, "Aufl\u00f6sung"),
        "top_info_refresh_rate": top_info_value(tile, "Bildwiederholfrequenz"),
        "otto_up": tile.get("ottoUp"),
    }


def compose_display_rows(page_number: int, offset: int, data: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ranked_products = products_for_intent(data, "ranked")
    sponsored_products = products_for_intent(data, "sponsored")
    sponsored_by_position = {
        position: sponsored_products[index]
        for index, position in enumerate(SPONSORED_SLOT_POSITIONS)
        if index < len(sponsored_products)
    }
    ranked_index = 0
    rows: list[dict[str, Any]] = []

    for local_position in range(1, LISTING_POSITIONS_PER_PAGE + 1):
        if local_position in sponsored_by_position:
            product = sponsored_by_position[local_position]
            exposure_type = "sponsored"
        else:
            if ranked_index >= len(ranked_products):
                break
            product = ranked_products[ranked_index]
            ranked_index += 1
            exposure_type = "organic"

        variation_id = text_or_none(product.get("bestVariationId") or product.get("id"))
        product_id = text_or_none(product.get("id"))
        global_position = (page_number - 1) * LISTING_POSITIONS_PER_PAGE + local_position
        row = {
            "source": "topseller_api",
            "listing_area": "everglades/products + crocotile/tile/data",
            "page_number": page_number,
            "page_offset": offset,
            "row_index": global_position,
            "display_rank": global_position,
            "local_display_rank": local_position,
            "target_rank": global_position,
            "exposure_type": exposure_type,
            "render_state": "api_listing_only",
            "is_fully_loaded_card": False,
            "is_placeholder_card": False,
            "is_listing_target": True,
            "article_number": text_or_none(product.get("articleNumber")),
            "product_id": product_id,
            "product_id_numeric": product_id_without_prefix(product_id),
            "variation_id": variation_id,
            "best_variation_id": variation_id,
            "list_position": global_position,
            "local_list_position": local_position,
            "origin": "sponsored" if exposure_type == "sponsored" else None,
            "product_url": absolute_otto_url(product.get("variationPath")),
            "retailer_sku_name": text_or_none(product.get("name")),
            "source_api_product_name": text_or_none(product.get("name")),
        }
        rows.append(row)

    summary = {
        "page_number": page_number,
        "offset": offset,
        "ranked_products": len(ranked_products),
        "sponsored_products": len(sponsored_products),
        "display_rows": len(rows),
        "organic_display_rows": sum(1 for row in rows if row["exposure_type"] == "organic"),
        "sponsored_display_rows": sum(1 for row in rows if row["exposure_type"] == "sponsored"),
        "ranked_consumed_for_organic_slots": ranked_index,
        "sponsored_slot_positions": list(SPONSORED_SLOT_POSITIONS),
    }
    return rows, summary


def unique_values(rows: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = text_or_none(row.get(key))
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_crocotile_tiles(variation_ids: list[str]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    tile_by_variation_id: dict[str, dict[str, Any]] = {}
    batch_meta: list[dict[str, Any]] = []

    for batch_index, batch_ids in enumerate(chunked(variation_ids, CROCOTILE_BATCH_SIZE), start=1):
        data, meta = fetch_json(build_crocotile_url(batch_ids))
        products = data if isinstance(data, list) else []
        for item in products:
            if isinstance(item, dict):
                variation_id = text_or_none(item.get("variationId"))
                if variation_id:
                    tile_by_variation_id[variation_id] = item
        batch_meta.append(
            {
                "batch_index": batch_index,
                "requested": len(batch_ids),
                "returned": len(products),
                "http_status": meta.get("http_status"),
                "body_bytes": meta.get("body_bytes"),
                "body_sha1": meta.get("body_sha1"),
                "elapsed_seconds": meta.get("elapsed_seconds"),
            }
        )
        if REQUEST_SLEEP_SECONDS > 0:
            time.sleep(REQUEST_SLEEP_SECONDS)
    return tile_by_variation_id, batch_meta


def assign_exposure_ranks(rows: list[dict[str, Any]]) -> None:
    organic_rank = 0
    sponsored_rank = 0
    for row in rows:
        if row.get("exposure_type") == "sponsored":
            sponsored_rank += 1
            row["organic_rank"] = None
            row["sponsored_rank"] = sponsored_rank
        else:
            organic_rank += 1
            row["organic_rank"] = organic_rank
            row["sponsored_rank"] = None


def enrich_rows(rows: list[dict[str, Any]], tile_by_variation_id: dict[str, dict[str, Any]]) -> None:
    for row in rows:
        tile = tile_by_variation_id.get(str(row.get("variation_id") or ""))
        if not tile:
            row["render_state"] = "api_listing_only"
            row["is_fully_loaded_card"] = False
            continue
        fallback_name = row.get("retailer_sku_name")
        row.update({key: value for key, value in crocotile_row_fields(tile, fallback_name).items() if value not in (None, "")})
        if row.get("exposure_type") == "sponsored":
            row["sku_status_raw"] = "Gesponsert"
            row["sku_status"] = "Sponsored"
        else:
            row["sku_status_raw"] = None
            row["sku_status"] = None
        row["render_state"] = "api_enriched"
        row["is_fully_loaded_card"] = True


def is_sponsored(row: dict[str, Any]) -> bool:
    return row.get("exposure_type") == "sponsored" or row.get("origin") == "sponsored"


def is_organic(row: dict[str, Any]) -> bool:
    return not is_sponsored(row)


def listing_composition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    organic_rows = [row for row in rows if is_organic(row)]
    sponsored_rows = [row for row in rows if is_sponsored(row)]
    organic_ids = {row.get("variation_id") for row in organic_rows if row.get("variation_id")}
    sponsored_ids = {row.get("variation_id") for row in sponsored_rows if row.get("variation_id")}
    return {
        "total_rows": len(rows),
        "organic_rows": len(organic_rows),
        "sponsored_rows": len(sponsored_rows),
        "organic_unique_variation_ids": len(organic_ids),
        "sponsored_unique_variation_ids": len(sponsored_ids),
        "sponsored_variation_ids_also_in_organic": len(organic_ids & sponsored_ids),
    }


def field_coverage(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, int]]:
    total = len(rows)
    coverage = {}
    for field in fields:
        present = sum(1 for row in rows if row.get(field) not in (None, ""))
        coverage[field] = {"present": present, "missing": total - present, "total": total}
    return coverage


def render_state_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_state = Counter(row.get("render_state") or "unknown" for row in rows)
    by_exposure_state = Counter(
        f"{row.get('exposure_type') or 'unknown'}_{row.get('render_state') or 'unknown'}" for row in rows
    )
    return {
        "by_state": dict(sorted(by_state.items())),
        "by_exposure_state": dict(sorted(by_exposure_state.items())),
    }


def top_rank_stats(rows: list[dict[str, Any]], rank_limit: int) -> dict[str, Any]:
    top_rows = [row for row in rows if isinstance(row.get("display_rank"), int) and row["display_rank"] <= rank_limit]
    counts = Counter(row.get("variation_id") for row in top_rows if row.get("variation_id"))
    sponsored_rows = [row for row in top_rows if is_sponsored(row)]
    duplicates = {key: value for key, value in sorted(counts.items()) if value > 1}
    return {
        "rank_basis": "display_rank_including_sponsored",
        "target_position_rows": len(top_rows),
        "unique_variation_ids": len(counts),
        "sponsored_position_rows": len(sponsored_rows),
        "duplicate_variation_ids": duplicates,
    }


def main() -> int:
    ensure_dirs()
    page_count = LISTING_PAGES_TO_COLLECT
    offsets = build_offsets(page_count)
    rows: list[dict[str, Any]] = []
    everglades_meta: list[dict[str, Any]] = []
    page_summaries: list[dict[str, Any]] = []

    for page_number, offset in enumerate(offsets, start=1):
        data, meta = fetch_json(build_everglades_url(offset))
        page_rows, summary = compose_display_rows(page_number, offset, data)
        rows.extend(page_rows)
        everglades_meta.append(
            {
                "page_number": page_number,
                "offset": offset,
                "http_status": meta.get("http_status"),
                "body_bytes": meta.get("body_bytes"),
                "body_sha1": meta.get("body_sha1"),
                "elapsed_seconds": meta.get("elapsed_seconds"),
            }
        )
        page_summaries.append(summary)
        if REQUEST_SLEEP_SECONDS > 0:
            time.sleep(REQUEST_SLEEP_SECONDS)

    assign_exposure_ranks(rows)
    variation_ids = unique_values(rows, "variation_id")
    tile_by_variation_id, crocotile_meta = fetch_crocotile_tiles(variation_ids)
    enrich_rows(rows, tile_by_variation_id)

    organic_rows = [row for row in rows if is_organic(row)]
    sponsored_rows = [row for row in rows if is_sponsored(row)]
    bsr_rows = [row for row in rows if isinstance(row.get("display_rank"), int) and row["display_rank"] <= BSR_TARGET_RANK]

    write_csv(LISTING_OUTPUT, rows)
    write_csv(ORGANIC_OUTPUT, organic_rows)
    write_csv(SPONSORED_OUTPUT, sponsored_rows)
    write_csv(MAIN_CAPTURE_OUTPUT, rows)
    write_csv(BSR_CAPTURE_OUTPUT, bsr_rows)

    unique_count = len({row.get("variation_id") for row in rows if row.get("variation_id")})
    manifest = {
        "run_type": "step01_listing",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "transport": "direct_rest_xhr",
        "source_url": TOPSELLER_URL,
        "everglades_endpoint": EVERGLADES_URL,
        "crocotile_endpoint": CROCOTILE_URL,
        "everglades_rule": EVERGLADES_RULE,
        "page_offsets": offsets,
        "sponsored_slot_positions": list(SPONSORED_SLOT_POSITIONS),
        "listing_pages_collected": page_count,
        "listing_positions_per_page": LISTING_POSITIONS_PER_PAGE,
        "listing_position_budget": page_count * LISTING_POSITIONS_PER_PAGE,
        "listing_rows": len(rows),
        "unique_variation_ids": unique_count,
        "crocotile_requested_unique_variation_ids": len(variation_ids),
        "crocotile_returned_unique_variation_ids": len(tile_by_variation_id),
        "crocotile_missing_variation_ids": [value for value in variation_ids if value not in tile_by_variation_id],
        "main_target_unique": MAIN_TARGET_UNIQUE,
        "bsr_rank_limit": BSR_TARGET_RANK,
        "listing_composition": {
            "all_pages": listing_composition(rows),
            "bsr_top_positions": listing_composition(bsr_rows),
        },
        "target_selection_rule": (
            "All product positions in the target listing area are eligible targets, including sponsored slots. "
            "Organic/sponsored exposure is retained as metadata; final target dedupe happens in step02 by retailer_sku_name."
        ),
        "position_reconstruction_rule": (
            "Each everglades page returns ranked and sponsored products separately. "
            "The displayed 120 positions are reconstructed by placing sponsored products into the observed sponsored "
            "slot positions and filling the remaining positions with ranked products in order."
        ),
        "bsr_rank_rule": (
            "Main and BSR both use the same Topseller listing order; BSR is the first 100 display positions "
            "before step02 TV filtering and retailer_sku_name dedupe."
        ),
        "field_sources": {
            "rank_identity_exposure": "everglades/products",
            "price_savings_availability_reviews_images_energy": "crocotile/tile/data",
        },
        "page_summaries": page_summaries,
        "everglades_requests": everglades_meta,
        "crocotile_requests": crocotile_meta,
        "render_state_stats": render_state_stats(rows),
        "field_coverage": field_coverage(rows, LISTING_FIELD_COVERAGE_FIELDS),
        "top_rank_stats": top_rank_stats(rows, BSR_TARGET_RANK),
        "outputs": {
            "listing_rows": str(LISTING_OUTPUT),
            "organic_rows": str(ORGANIC_OUTPUT),
            "sponsored_rows": str(SPONSORED_OUTPUT),
            "main_capture_rows": str(MAIN_CAPTURE_OUTPUT),
            "bsr_capture_rows": str(BSR_CAPTURE_OUTPUT),
        },
    }
    write_json(MANIFEST_OUTPUT, manifest)

    composition = manifest["listing_composition"]["all_pages"]
    coverage = manifest["field_coverage"]
    print(
        "[step01] transport=direct_rest_xhr pages={pages} rows={rows} organic={organic} "
        "sponsored={sponsored} unique_variations={unique} crocotile={returned}/{requested}".format(
            pages=page_count,
            rows=len(rows),
            organic=composition["organic_rows"],
            sponsored=composition["sponsored_rows"],
            unique=unique_count,
            returned=len(tile_by_variation_id),
            requested=len(variation_ids),
        )
    )
    print(
        "[step01] coverage price={price}/{total} original={original}/{total} savings={savings}/{total} "
        "discount_type={discount}/{total}".format(
            price=coverage["final_sku_price"]["present"],
            original=coverage["original_sku_price"]["present"],
            savings=coverage["savings"]["present"],
            discount=coverage["discount_type"]["present"],
            total=len(rows),
        )
    )
    print(f"[step01] output={LISTING_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
