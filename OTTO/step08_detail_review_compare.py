"""Step08: parse sample OTTO detail, review, and compare documents."""
from __future__ import annotations

from step00_config import (
    COMPARE_SAMPLE_HTML,
    DETAIL_SAMPLE_HTML,
    DETAIL_SAMPLE_URL,
    OUTPUT_ROOT,
    REVIEW_SAMPLE_HTML,
    REVIEW_SAMPLE_URL,
    write_csv,
    write_json,
)
from step00_parsers import parse_compare_html, parse_detail_html, parse_review_html

DETAIL_SUMMARY_OUTPUT = OUTPUT_ROOT / "otto_detail_probe_summary.json"
DETAIL_REVIEWS_OUTPUT = OUTPUT_ROOT / "otto_detail_top_reviews.csv"
REVIEW_SUMMARY_OUTPUT = OUTPUT_ROOT / "otto_review_probe_summary.json"
REVIEW_ROWS_OUTPUT = OUTPUT_ROOT / "otto_review_rows.csv"
COMPARE_SUMMARY_OUTPUT = OUTPUT_ROOT / "otto_compare_probe_summary.json"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step08_detail_review_compare_manifest.json"


def main() -> int:
    detail = parse_detail_html(DETAIL_SAMPLE_HTML)
    review = parse_review_html(REVIEW_SAMPLE_HTML) if REVIEW_SAMPLE_HTML.exists() else {}
    compare = parse_compare_html(COMPARE_SAMPLE_HTML)

    if review.get("summarized_review_content") and not detail.get("summarized_review_content"):
        detail["summarized_review_content"] = review.get("summarized_review_content")
        detail["summarized_review_source"] = "review_page"
    elif detail.get("summarized_review_content"):
        detail["summarized_review_source"] = "detail_page"
    else:
        detail["summarized_review_source"] = None

    if review.get("detailed_review_content"):
        detail["detailed_review_content"] = review.get("detailed_review_content")
        detail["detailed_review_count"] = review.get("detailed_review_count")
        detail["review_page_rows"] = review.get("review_rows")
        detail["review_page_text_rows"] = review.get("review_text_rows")
        detail["review_page_html"] = str(REVIEW_SAMPLE_HTML)

    write_json(DETAIL_SUMMARY_OUTPUT, detail)
    write_csv(DETAIL_REVIEWS_OUTPUT, detail.get("top_reviews") or [])
    write_json(REVIEW_SUMMARY_OUTPUT, review)
    write_csv(REVIEW_ROWS_OUTPUT, review.get("reviews") or [])
    write_json(COMPARE_SUMMARY_OUTPUT, compare)

    manifest = {
        "run_type": "step08_detail_review_compare",
        "detail_html": str(DETAIL_SAMPLE_HTML),
        "next_detail_sample_url": DETAIL_SAMPLE_URL,
        "next_review_sample_url": REVIEW_SAMPLE_URL,
        "review_html": str(REVIEW_SAMPLE_HTML),
        "compare_html": str(COMPARE_SAMPLE_HTML),
        "detail_jsonld_count": detail.get("jsonld_count"),
        "detail_top_review_rows": detail.get("top_review_rows"),
        "review_page_rows": review.get("review_rows"),
        "review_page_text_rows": review.get("review_text_rows"),
        "detailed_review_count": review.get("detailed_review_count"),
        "summarized_review_present": bool(detail.get("summarized_review_content")),
        "summarized_review_source": detail.get("summarized_review_source"),
        "delivery_availability": detail.get("delivery_availability"),
        "sku": detail.get("sku"),
        "screen_size": detail.get("screen_size"),
        "estimated_annual_electricity_use": detail.get("estimated_annual_electricity_use"),
        "count_of_star_ratings": detail.get("count_of_star_ratings"),
        "count_of_reviews": detail.get("count_of_reviews"),
        "compare_variation_id_count": compare.get("variation_id_count"),
        "known_gap": None if review.get("detailed_review_count") else "Review page sample missing or has no non-empty review text.",
        "outputs": {
            "detail_summary": str(DETAIL_SUMMARY_OUTPUT),
            "detail_top_reviews": str(DETAIL_REVIEWS_OUTPUT),
            "review_summary": str(REVIEW_SUMMARY_OUTPUT),
            "review_rows": str(REVIEW_ROWS_OUTPUT),
            "compare_summary": str(COMPARE_SUMMARY_OUTPUT),
        },
    }
    write_json(MANIFEST_OUTPUT, manifest)
    print(
        "[step08] detail_jsonld={jsonld} pdp_reviews={pdp_reviews} "
        "review_page_rows={review_rows} detailed_reviews={detailed_reviews} "
        "sku={sku} screen_size={screen_size} electricity={electricity} compare_ids={compare}".format(
            jsonld=detail.get("jsonld_count"),
            pdp_reviews=detail.get("top_review_rows"),
            review_rows=review.get("review_rows"),
            detailed_reviews=review.get("detailed_review_count"),
            sku=detail.get("sku"),
            screen_size=detail.get("screen_size"),
            electricity=detail.get("estimated_annual_electricity_use"),
            compare=compare.get("variation_id_count"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
