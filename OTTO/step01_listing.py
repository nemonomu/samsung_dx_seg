"""Step01: parse OTTO Topseller listing from the canonical captured page."""
from __future__ import annotations

from collections import Counter

from step00_config import (
    BSR_CAPTURE_HTML,
    BSR_TARGET_RANK,
    CANONICAL_LISTING_HTML,
    MAIN_CAPTURE_HTML,
    OUTPUT_ROOT,
    TOPSELLER_URL,
    ensure_dirs,
    write_csv,
    write_json,
)
from step00_parsers import parse_listing_html

LISTING_OUTPUT = OUTPUT_ROOT / "otto_listing_topseller_rows.csv"
MAIN_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_main_capture_rows.csv"
BSR_CAPTURE_OUTPUT = OUTPUT_ROOT / "otto_listing_bsr_capture_rows.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step01_listing_manifest.json"


def top_rank_stats(rows: list[dict], rank_limit: int) -> dict:
    top_rows = [row for row in rows if isinstance(row.get("list_position"), int) and row["list_position"] <= rank_limit]
    counts = Counter(row.get("variation_id") for row in top_rows if row.get("variation_id"))
    sponsored = Counter(row.get("variation_id") for row in top_rows if row.get("variation_id") and row.get("origin") == "sponsored")
    duplicates = {key: value for key, value in sorted(counts.items()) if value > 1}
    return {
        "raw_rank_rows": len(top_rows),
        "unique_variation_ids": len(counts),
        "duplicate_variation_ids": duplicates,
        "sponsored_duplicate_variation_ids": {key: sponsored.get(key, 0) for key in duplicates if sponsored.get(key, 0)},
    }


def compare_captures(main_rows: list[dict], bsr_rows: list[dict], rank_limit: int) -> dict:
    main_by_rank = {row.get("list_position"): row for row in main_rows if row.get("list_position") is not None}
    bsr_by_rank = {row.get("list_position"): row for row in bsr_rows if row.get("list_position") is not None}
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
                "rank": rank,
                "main_variation_id": main_vid,
                "bsr_variation_id": bsr_vid,
                "main_name": main.get("retailer_sku_name") if main else None,
                "bsr_name": bsr.get("retailer_sku_name") if bsr else None,
            })
    return {"same_rank_count": same, "mismatch_count": len(mismatches), "mismatches": mismatches}


def main() -> int:
    ensure_dirs()
    canonical_rows = parse_listing_html(CANONICAL_LISTING_HTML, "topseller")
    main_rows = parse_listing_html(MAIN_CAPTURE_HTML, "main_capture") if MAIN_CAPTURE_HTML.exists() else []
    bsr_rows = parse_listing_html(BSR_CAPTURE_HTML, "bsr_capture") if BSR_CAPTURE_HTML.exists() else []

    write_csv(LISTING_OUTPUT, canonical_rows)
    write_csv(MAIN_CAPTURE_OUTPUT, main_rows)
    write_csv(BSR_CAPTURE_OUTPUT, bsr_rows)

    unique_count = len({row.get("variation_id") for row in canonical_rows if row.get("variation_id")})
    manifest = {
        "run_type": "step01_listing",
        "source_url": TOPSELLER_URL,
        "canonical_html": str(CANONICAL_LISTING_HTML),
        "canonical_rows": len(canonical_rows),
        "canonical_unique_variation_ids": unique_count,
        "bsr_rank_rule": "main and bsr both use the same Topseller exposure order from Deloitte workbook.",
        "top_rank_stats": top_rank_stats(canonical_rows, BSR_TARGET_RANK),
        "capture_drift": compare_captures(main_rows, bsr_rows, BSR_TARGET_RANK) if main_rows and bsr_rows else None,
        "outputs": {
            "listing_rows": str(LISTING_OUTPUT),
            "main_capture_rows": str(MAIN_CAPTURE_OUTPUT),
            "bsr_capture_rows": str(BSR_CAPTURE_OUTPUT),
        },
    }
    write_json(MANIFEST_OUTPUT, manifest)
    print(f"[step01] rows={len(canonical_rows)} unique={unique_count} output={LISTING_OUTPUT}")
    print(f"[step01] top100_unique={manifest['top_rank_stats']['unique_variation_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
