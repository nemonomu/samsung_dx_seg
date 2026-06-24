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
        f"Listing transport: {listing.get('transport', '')}",
        f"Listing rows: {listing.get('listing_rows', 0)}",
        f"Listing positions per page: {listing.get('listing_positions_per_page', 0)}",
        f"Listing pages collected: {listing.get('listing_pages_collected', 0)}",
        f"Crocotile enrichment: {listing.get('crocotile_returned_unique_variation_ids', 0)}/{listing.get('crocotile_requested_unique_variation_ids', 0)}",
        f"TV listing positions after filter: {targets.get('tv_position_rows_after_filter', 0)}",
        f"Excluded non-TV positions: {targets.get('excluded_non_tv_rows', 0)}",
        f"Available unique retailer_sku_name TV targets: {targets.get('tv_unique_retailer_sku_names_available', 0)}",
        f"Final targets: {targets.get('final_target_rows', 0)}",
        f"Final target shortfall: {targets.get('main_target_shortfall', 0)}",
        f"BSR tagged rows: {targets.get('bsr_tagged_rows', 0)}",
        f"Detail top reviews from sample PDP HTML: {detail.get('detail_top_review_rows', 0)}",
        f"Review page rows from sample: {detail.get('review_page_rows', 0)}",
        f"Detailed review count from sample: {detail.get('detailed_review_count', 0)}",
        f"Compare variation ids in sample: {detail.get('compare_variation_id_count', 0)}",
        f"DB save dry-run: {db.get('dry_run', True)}",
        "",
        "Known gaps:",
        "- Step08 is still sample detail/review/compare parsing, not final-target batch collection.",
        "- summarized_review_content was not present in the current sample.",
        "- Need final DB table/field mapping before live insert.",
    ]
    REPORT_OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {"run_type": "step15_email_notify", "sent": False, "report": str(REPORT_OUTPUT)}
    write_json(MANIFEST_OUTPUT, manifest)
    print(f"[step15] report={REPORT_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
