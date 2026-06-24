"""Step01: parse OTTO Topseller listing from the canonical captured page."""
from __future__ import annotations

from collections import Counter

from step00_config import (
    BSR_CAPTURE_HTML,
    BSR_TARGET_RANK,
    CANONICAL_LISTING_HTML,
    LISTING_PAGES_TO_COLLECT,
    LISTING_POSITIONS_PER_PAGE,
    MAIN_CAPTURE_HTML,
    OUTPUT_ROOT,
    TOPSELLER_URL,
    ensure_dirs,
    write_csv,
    write_json,
)
from step00_parsers import parse_listing_html

LISTING_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_rows.csv"
ORGANIC_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_organic_rows.csv"
SPONSORED_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_sponsored_rows.csv"
MAIN_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_main_capture_rows.csv"
BSR_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_bsr_capture_rows.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step01_listing_manifest.json"

LISTING_FIELD_COVERAGE_FIELDS = [
    "product_url",
    "retailer_sku_name",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "sku_popularity",
    "sku_status",
    "discount_type",
]


def is_sponsored(row: dict) -> bool:
    return row.get("exposure_type") == "sponsored" or row.get("origin") == "sponsored"


def is_organic(row: dict) -> bool:
    return not is_sponsored(row)


def split_exposure_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    organic_rows = [row for row in rows if is_organic(row)]
    sponsored_rows = [row for row in rows if is_sponsored(row)]
    return organic_rows, sponsored_rows


def listing_composition(rows: list[dict]) -> dict:
    organic_rows, sponsored_rows = split_exposure_rows(rows)
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


def top_rank_stats(rows: list[dict], rank_limit: int) -> dict:
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


def field_coverage(rows: list[dict], fields: list[str]) -> dict:
    total = len(rows)
    coverage = {}
    for field in fields:
        present = sum(1 for row in rows if row.get(field) not in (None, ""))
        coverage[field] = {"present": present, "missing": total - present, "total": total}
    return coverage


def render_state_stats(rows: list[dict]) -> dict:
    by_state = Counter(row.get("render_state") or "unknown" for row in rows)
    by_exposure_state = Counter(
        f"{row.get('exposure_type') or 'unknown'}_{row.get('render_state') or 'unknown'}" for row in rows
    )
    ranges = {}
    for row in rows:
        state = row.get("render_state") or "unknown"
        display_rank = row.get("display_rank")
        if not isinstance(display_rank, int):
            continue
        ranges.setdefault(state, []).append(display_rank)
    return {
        "by_state": dict(sorted(by_state.items())),
        "by_exposure_state": dict(sorted(by_exposure_state.items())),
        "display_rank_ranges": {key: [min(values), max(values)] for key, values in sorted(ranges.items())},
    }


def compare_captures(main_rows: list[dict], bsr_rows: list[dict], rank_limit: int) -> dict:
    main_by_rank = {row.get("organic_rank"): row for row in main_rows if is_organic(row) and row.get("organic_rank") is not None}
    bsr_by_rank = {row.get("organic_rank"): row for row in bsr_rows if is_organic(row) and row.get("organic_rank") is not None}
    mismatches = []
    same = 0
    for rank in range(1, rank_limit + 1):
        main = main_by_rank.get(rank)
        bsr = bsr_by_rank.get(rank)
        main_vid = main.get("variation_id") if main else None
        bsr_vid = bsr.get("variation_id") if bsr else None
        if main_vid and main_vid == bsr_vid:
            same += 1
        else:
            mismatches.append({
                "organic_rank": rank,
                "main_variation_id": main_vid,
                "bsr_variation_id": bsr_vid,
                "main_name": main.get("retailer_sku_name") if main else None,
                "bsr_name": bsr.get("retailer_sku_name") if bsr else None,
            })
    return {"rank_basis": "organic_rank", "same_rank_count": same, "mismatch_count": len(mismatches), "mismatches": mismatches}


def main() -> int:
    ensure_dirs()
    canonical_rows = parse_listing_html(CANONICAL_LISTING_HTML, "topseller")
    main_rows = parse_listing_html(MAIN_CAPTURE_HTML, "main_capture") if MAIN_CAPTURE_HTML.exists() else []
    bsr_rows = parse_listing_html(BSR_CAPTURE_HTML, "bsr_capture") if BSR_CAPTURE_HTML.exists() else []

    canonical_organic_rows, canonical_sponsored_rows = split_exposure_rows(canonical_rows)

    write_csv(LISTING_OUTPUT, canonical_rows)
    write_csv(ORGANIC_OUTPUT, canonical_organic_rows)
    write_csv(SPONSORED_OUTPUT, canonical_sponsored_rows)
    write_csv(MAIN_CAPTURE_OUTPUT, main_rows)
    write_csv(BSR_CAPTURE_OUTPUT, bsr_rows)

    unique_count = len({row.get("variation_id") for row in canonical_rows if row.get("variation_id")})
    manifest = {
        "run_type": "step01_listing",
        "source_url": TOPSELLER_URL,
        "canonical_html": str(CANONICAL_LISTING_HTML),
        "canonical_rows": len(canonical_rows),
        "canonical_unique_variation_ids": unique_count,
        "production_listing_pages_to_collect": LISTING_PAGES_TO_COLLECT,
        "production_listing_positions_per_page": LISTING_POSITIONS_PER_PAGE,
        "production_listing_position_budget": LISTING_PAGES_TO_COLLECT * LISTING_POSITIONS_PER_PAGE,
        "listing_composition": {
            "canonical": listing_composition(canonical_rows),
            "main_capture": listing_composition(main_rows) if main_rows else None,
            "bsr_capture": listing_composition(bsr_rows) if bsr_rows else None,
        },
        "target_selection_rule": "All product positions inside the target listing area are eligible targets, including sponsored positions. Organic/sponsored is retained as exposure metadata, not used for exclusion.",
        "listing_area_rule": "Parse product article rows only inside #reptile-search-result section.reptile-tile-list. Non-product slots inside the section, such as benefit cinema, comparison banner, and SDA ad slots, are excluded.",
        "render_state_stats": {
            "canonical": render_state_stats(canonical_rows),
            "main_capture": render_state_stats(main_rows) if main_rows else None,
            "bsr_capture": render_state_stats(bsr_rows) if bsr_rows else None,
        },
        "field_coverage": {
            "canonical": field_coverage(canonical_rows, LISTING_FIELD_COVERAGE_FIELDS),
            "main_capture": field_coverage(main_rows, LISTING_FIELD_COVERAGE_FIELDS) if main_rows else None,
            "bsr_capture": field_coverage(bsr_rows, LISTING_FIELD_COVERAGE_FIELDS) if bsr_rows else None,
        },
        "bsr_rank_rule": "main and bsr both use the same Topseller display-position order from Deloitte workbook; sponsored positions are included in rank positions.",
        "capture_limitations": [
            "Current local listing captures contain only the first SERP document; the o=120 capture redirected back to /suche/fernseher/ and is not a confirmed second page.",
            "The captured SERP HTML has variation price/savings data only for the initially loaded tile subset; later tiles require pagination/scroll variation XHR capture or the underlying pageFetcher product response.",
        ],
        "top_rank_stats": top_rank_stats(canonical_rows, BSR_TARGET_RANK),
        "capture_drift": compare_captures(main_rows, bsr_rows, BSR_TARGET_RANK) if main_rows and bsr_rows else None,
        "outputs": {
            "listing_rows": str(LISTING_OUTPUT),
            "organic_rows": str(ORGANIC_OUTPUT),
            "sponsored_rows": str(SPONSORED_OUTPUT),
            "main_capture_rows": str(MAIN_CAPTURE_OUTPUT),
            "bsr_capture_rows": str(BSR_CAPTURE_OUTPUT),
        },
    }
    write_json(MANIFEST_OUTPUT, manifest)
    composition = manifest["listing_composition"]["canonical"]
    render_counts = manifest["render_state_stats"]["canonical"]["by_state"]
    print(
        f"[step01] rows={len(canonical_rows)} organic={composition['organic_rows']} "
        f"sponsored={composition['sponsored_rows']} loaded={render_counts.get('fully_loaded_card', 0)} "
        f"placeholder={render_counts.get('placeholder_link_only', 0)} unique={unique_count} output={LISTING_OUTPUT}"
    )
    print(f"[step01] position_top100_unique={manifest['top_rank_stats']['unique_variation_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
