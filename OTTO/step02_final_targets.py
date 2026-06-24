"""Step02: build final OTTO TV targets from listing positions."""
from __future__ import annotations

from collections import defaultdict
import re

from step00_config import BSR_TARGET_RANK, MAIN_TARGET_UNIQUE, OUTPUT_ROOT, UNIQUE_KEY, read_csv, write_csv, write_json

INPUT_CSV = OUTPUT_ROOT / "otto_listing_topseller_rows.csv"
OUTPUT_CSV = OUTPUT_ROOT / "otto_final_targets.csv"
EXCLUDED_OUTPUT_CSV = OUTPUT_ROOT / "otto_excluded_non_tv_rows.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step02_final_targets_manifest.json"

TV_POSITIVE_KEYWORDS = (
    "fernseher",
    "smart-tv",
    "smart tv",
    "oled-tv",
    "oled tv",
    "qled-tv",
    "qled tv",
    "led-tv",
    "led tv",
    "lcd-tv",
    "lcd tv",
)

TV_PRODUCT_PATTERNS = (
    r"\b(?:mini-led|lcd-led|dled|qled|oled|led|lcd)-fernseher\b",
    r"\b(?:mini-led|lcd-led|dled|qled|oled|led|lcd) fernseher\b",
    r"\b(?:oled|qled|led|lcd)-tv\b",
)

HARD_NON_TV_EXCLUDE_KEYWORDS = (
    "wandhalter",
    "halterung",
    "tv-schrank",
    "fernsehschrank",
    "schrank",
    "lowboard",
    "tv-st\u00e4nder",
    "tv staender",
    "tv-staender",
    "st\u00e4nder",
    "staender",
    "tv-board",
    "tv board",
    "led stripe",
    "hintergrundbeleuchtung",
    "beleuchtung",
    "projektor",
    "beamer",
    "leinwand",
    "monitor",
    "receiver",
    "antenne",
    "kabel",
    "streaming-stick",
    "streaming stick",
    "streaming-box",
    "streaming box",
    "ci+-modul",
)

ACCESSORY_EXCLUDE_KEYWORDS = (
    "fernbedienung",
    "soundbar",
)

NON_TV_EXCLUDE_KEYWORDS = HARD_NON_TV_EXCLUDE_KEYWORDS + ACCESSORY_EXCLUDE_KEYWORDS
TV_PRODUCT_REGEXES = tuple(re.compile(pattern) for pattern in TV_PRODUCT_PATTERNS)


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalized_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned.casefold() or None


def has_tv_product_signature(key: str) -> bool:
    return any(regex.search(key) for regex in TV_PRODUCT_REGEXES)


def classify_tv_sku(row: dict) -> tuple[bool, str]:
    name = row.get("retailer_sku_name") or ""
    key = normalized_name(name)
    if not key:
        return False, "missing_retailer_sku_name"

    product_signature = has_tv_product_signature(key)
    hard_exclude_hits = [term for term in HARD_NON_TV_EXCLUDE_KEYWORDS if term in key]
    if hard_exclude_hits:
        return False, "exclude_keyword:" + ",".join(hard_exclude_hits)

    accessory_hits = [term for term in ACCESSORY_EXCLUDE_KEYWORDS if term in key]
    if accessory_hits and not product_signature:
        return False, "exclude_accessory_keyword:" + ",".join(accessory_hits)

    if product_signature:
        if accessory_hits:
            return True, "tv_product_signature_with_accessory_bundle"
        return True, "tv_product_signature"

    if any(term in key for term in TV_POSITIVE_KEYWORDS):
        return True, "tv_keyword"
    return False, "missing_tv_positive_keyword"


def is_sponsored(row: dict) -> bool:
    return row.get("exposure_type") == "sponsored" or row.get("origin") == "sponsored"


def is_organic(row: dict) -> bool:
    return not is_sponsored(row)


def display_rank_for(row: dict) -> int | None:
    return to_int(row.get("display_rank")) or to_int(row.get("list_position")) or to_int(row.get("row_index"))


def sort_listing_rows(rows: list[dict]) -> list[dict]:
    return sorted(
        rows,
        key=lambda row: (
            display_rank_for(row) is None,
            display_rank_for(row) or 999999,
            to_int(row.get("row_index")) or 999999,
        ),
    )


def build_name_exposure_index(rows: list[dict]) -> dict[str, dict[str, list]]:
    exposure_index: dict[str, dict[str, list]] = defaultdict(
        lambda: {"all": [], "organic": [], "sponsored": [], "variation_ids": [], "product_urls": []}
    )
    for row in rows:
        key = normalized_name(row.get("retailer_sku_name"))
        if not key:
            continue
        display_rank = display_rank_for(row)
        if display_rank is not None:
            exposure_index[key]["all"].append(display_rank)
            if is_sponsored(row):
                exposure_index[key]["sponsored"].append(display_rank)
            else:
                exposure_index[key]["organic"].append(display_rank)
        variation_id = row.get("variation_id")
        if variation_id and variation_id not in exposure_index[key]["variation_ids"]:
            exposure_index[key]["variation_ids"].append(variation_id)
        product_url = row.get("product_url")
        if product_url and product_url not in exposure_index[key]["product_urls"]:
            exposure_index[key]["product_urls"].append(product_url)
    return {
        key: {
            "all": sorted(values["all"]),
            "organic": sorted(values["organic"]),
            "sponsored": sorted(values["sponsored"]),
            "variation_ids": values["variation_ids"],
            "product_urls": values["product_urls"],
        }
        for key, values in exposure_index.items()
    }


def build_final_targets(rows: list[dict], main_limit: int, bsr_rank_limit: int) -> tuple[list[dict], list[dict]]:
    sorted_rows = sort_listing_rows(rows)
    tv_rows: list[dict] = []
    excluded_rows: list[dict] = []
    for row in sorted_rows:
        is_tv, reason = classify_tv_sku(row)
        annotated = dict(row)
        annotated["tv_filter_reason"] = reason
        annotated["is_tv_sku"] = is_tv
        if is_tv:
            tv_rows.append(annotated)
        else:
            excluded_rows.append(annotated)

    exposure_index = build_name_exposure_index(tv_rows)
    final_rows: list[dict] = []
    seen_names: set[str] = set()
    for row in tv_rows:
        name_key = normalized_name(row.get("retailer_sku_name"))
        if not name_key or name_key in seen_names:
            continue
        seen_names.add(name_key)
        final_rank = len(final_rows) + 1
        ranks = exposure_index.get(name_key, {"all": [], "organic": [], "sponsored": [], "variation_ids": [], "product_urls": []})
        out = dict(row)
        out["retailer_sku_name_key"] = name_key
        out["final_target_rank"] = final_rank
        out["target_rank"] = final_rank
        out["main_rank"] = final_rank
        out["bsr_rank"] = final_rank if final_rank <= bsr_rank_limit else ""
        out["selection_source"] = "topseller_tv_name_main_bsr" if out["bsr_rank"] != "" else "topseller_tv_name_main"
        out["all_display_ranks"] = ",".join(str(rank) for rank in ranks.get("all", []))
        out["organic_display_ranks"] = ",".join(str(rank) for rank in ranks.get("organic", []))
        out["sponsored_display_ranks"] = ",".join(str(rank) for rank in ranks.get("sponsored", []))
        out["variation_ids_for_name"] = ",".join(ranks.get("variation_ids", []))
        out["product_urls_for_name"] = " ||| ".join(ranks.get("product_urls", []))
        out["has_sponsored_exposure"] = bool(ranks.get("sponsored"))
        out["has_organic_exposure"] = bool(ranks.get("organic"))
        final_rows.append(out)
        if len(final_rows) >= main_limit:
            break
    return final_rows, excluded_rows


def main() -> int:
    rows = read_csv(INPUT_CSV)
    final_rows, excluded_rows = build_final_targets(rows, MAIN_TARGET_UNIQUE, BSR_TARGET_RANK)
    organic_rows = [row for row in rows if is_organic(row)]
    sponsored_rows = [row for row in rows if is_sponsored(row)]
    tv_position_rows = [row for row in rows if classify_tv_sku(row)[0]]
    tv_name_keys = {normalized_name(row.get("retailer_sku_name")) for row in tv_position_rows if normalized_name(row.get("retailer_sku_name"))}
    target_shortfall = max(0, MAIN_TARGET_UNIQUE - len(final_rows))

    write_csv(OUTPUT_CSV, final_rows)
    write_csv(EXCLUDED_OUTPUT_CSV, excluded_rows)
    manifest = {
        "run_type": "step02_final_targets",
        "input_rows": len(rows),
        "input_target_position_rows": len(rows),
        "input_organic_rows": len(organic_rows),
        "input_sponsored_rows": len(sponsored_rows),
        "tv_position_rows_after_filter": len(tv_position_rows),
        "excluded_non_tv_rows": len(excluded_rows),
        "tv_unique_retailer_sku_names_available": len(tv_name_keys),
        "final_target_rows": len(final_rows),
        "main_target_unique": MAIN_TARGET_UNIQUE,
        "main_target_shortfall": target_shortfall,
        "main_target_shortfall_reason": (
            f"Only {len(final_rows)} unique TV retailer_sku_name values were found inside the configured listing pages."
            if target_shortfall
            else None
        ),
        "bsr_rank_limit": BSR_TARGET_RANK,
        "bsr_tagged_rows": sum(1 for row in final_rows if row.get("bsr_rank") not in (None, "")),
        "unique_key": UNIQUE_KEY,
        "rank_basis": "sequential_after_tv_filter_and_retailer_sku_name_dedupe",
        "target_rule": "Collect configured listing pages, exclude non-TV SKUs, dedupe by retailer_sku_name, then assign final target ranks up to 1..300 in listing order.",
        "tv_positive_keywords": TV_POSITIVE_KEYWORDS,
        "tv_product_patterns": TV_PRODUCT_PATTERNS,
        "hard_non_tv_exclude_keywords": HARD_NON_TV_EXCLUDE_KEYWORDS,
        "accessory_exclude_keywords": ACCESSORY_EXCLUDE_KEYWORDS,
        "outputs": {
            "final_targets": str(OUTPUT_CSV),
            "excluded_non_tv_rows": str(EXCLUDED_OUTPUT_CSV),
        },
    }
    write_json(MANIFEST_OUTPUT, manifest)
    print(
        f"[step02] input_positions={len(rows)} tv_positions={len(tv_position_rows)} "
        f"excluded_non_tv={len(excluded_rows)} final_targets={len(final_rows)} "
        f"shortfall={target_shortfall} bsr_tagged={manifest['bsr_tagged_rows']} output={OUTPUT_CSV}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
