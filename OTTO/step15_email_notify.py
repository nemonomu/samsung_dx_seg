"""Step15: build a local OTTO notification report."""
from __future__ import annotations

from step00_config import OUTPUT_ROOT, read_json, write_json

REPORT_OUTPUT = OUTPUT_ROOT / "otto_email_report.txt"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step15_email_notify_manifest.json"


def safe_read(path_name: str) -> dict:
    path = OUTPUT_ROOT / path_name
    return read_json(path) if path.exists() else {}


def main() -> int:
    listing = safe_read("step01_listing_manifest.json")
    targets = safe_read("step02_final_targets_manifest.json")
    detail = safe_read("step08_detail_review_compare_manifest.json")
    db = safe_read("step14_db_save_manifest.json")
    lines = [
        "OTTO SEG TV crawler report",
        "",
        f"Listing rows: {listing.get('canonical_rows', 0)}",
        f"Listing positions per page: {listing.get('production_listing_positions_per_page', 0)}",
        f"Production listing pages: {listing.get('production_listing_pages_to_collect', 0)}",
        f"TV listing positions after filter: {targets.get('tv_position_rows_after_filter', 0)}",
        f"Excluded non-TV positions: {targets.get('excluded_non_tv_rows', 0)}",
        f"Available unique retailer_sku_name TV targets: {targets.get('tv_unique_retailer_sku_names_available', 0)}",
        f"Final targets: {targets.get('final_target_rows', 0)}",
        f"BSR tagged rows: {targets.get('bsr_tagged_rows', 0)}",
        f"Detail top reviews from PDP HTML: {detail.get('detail_top_review_rows', 0)}",
        f"Compare variation ids in sample: {detail.get('compare_variation_id_count', 0)}",
        f"DB save dry-run: {db.get('dry_run', True)}",
        "",
        "Known gaps:",
        "- Need live Topseller pagination/scroll capture through 4 pages to reach 300 unique TV retailer_sku_name targets.",
        "- Need review page or interaction HAR to collect up to 20 reviews per SKU.",
        "- Need final DB field mapping before live insert.",
    ]
    REPORT_OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {"run_type": "step15_email_notify", "sent": False, "report": str(REPORT_OUTPUT)}
    write_json(MANIFEST_OUTPUT, manifest)
    print(f"[step15] report={REPORT_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
