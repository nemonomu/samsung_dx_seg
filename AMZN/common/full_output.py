"""Step09: join Amazon listing targets and detail rows into DB-loadable output."""
from __future__ import annotations

from common.config import run_meta
from common.io_util import ACCOUNT_NAME, COUNTRY, category_output_root, read_csv, write_csv, write_json
from common.translations import translate_record_fields

BASE_FIELDS = [
    "account_name", "product", "country", "page_type", "crawl_strdatetime", "calendar_week", "batch_id",
    "main_rank", "bsr_rank", "item", "product_url", "redirect", "retailer_sku_name",
    "final_sku_price", "original_sku_price", "savings", "sku_popularity",
    "number_of_units_purchased_past_month", "sku_status", "discount_type",
    "available_quantity_for_purchase", "delivery_availability", "fastest_delivery", "inventory_status",
    "sku_assurance",
    "screen_size", "model_year", "sku", "estimated_annual_electricity_use",
    "retailer_sku_name_similar", "star_rating", "count_of_star_ratings", "count_of_reviews",
    "summarized_review_content", "detailed_review_content",
    "ref_refrigerator_type", "ref_capacity",
]


def first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def run(cfg) -> dict:
    out = category_output_root(cfg.PRODUCT)
    targets = read_csv(out / "amzn_final_targets.csv")
    details = read_csv(out / "amzn_detail.csv")
    detail_by_asin = {(r.get("asin") or r.get("item") or "").strip(): r for r in details}
    meta = run_meta("a")
    rows = []
    for target in targets:
        asin = (target.get("asin") or target.get("item") or "").strip()
        detail = detail_by_asin.get(asin, {})
        row = {
            "account_name": ACCOUNT_NAME,
            "product": cfg.PRODUCT,
            "country": COUNTRY,
            "page_type": "main",
            **meta,
            "main_rank": target.get("main_rank"),
            "bsr_rank": target.get("bsr_rank"),
            "item": first(detail.get("item"), asin),
            "product_url": first(detail.get("product_url"), target.get("product_url")),
            "redirect": detail.get("redirect"),
            "retailer_sku_name": first(detail.get("retailer_sku_name"), target.get("retailer_sku_name")),
            "final_sku_price": first(detail.get("final_sku_price"), target.get("final_sku_price")),
            "original_sku_price": first(detail.get("original_sku_price"), target.get("original_sku_price")),
            "savings": target.get("savings"),
            "sku_popularity": target.get("sku_popularity"),
            "number_of_units_purchased_past_month": target.get("number_of_units_purchased_past_month"),
            "sku_status": target.get("sku_status"),
            "discount_type": target.get("discount_type"),
            "available_quantity_for_purchase": detail.get("available_quantity_for_purchase"),
            "delivery_availability": detail.get("delivery_availability"),
            "fastest_delivery": detail.get("fastest_delivery"),
            "inventory_status": detail.get("inventory_status"),
            "sku_assurance": detail.get("sku_assurance"),
            "screen_size": detail.get("screen_size"),
            "model_year": detail.get("model_year"),
            "sku": detail.get("sku"),
            "estimated_annual_electricity_use": detail.get("estimated_annual_electricity_use"),
            "retailer_sku_name_similar": detail.get("retailer_sku_name_similar"),
            "star_rating": first(detail.get("star_rating"), target.get("star_rating")),
            "count_of_star_ratings": first(detail.get("count_of_star_ratings"), target.get("count_of_star_ratings")),
            "count_of_reviews": detail.get("count_of_reviews"),
            "summarized_review_content": detail.get("summarized_review_content"),
            "detailed_review_content": detail.get("detailed_review_content"),
            "ref_refrigerator_type": detail.get("ref_refrigerator_type"),
            "ref_capacity": detail.get("ref_capacity"),
        }
        translate_record_fields(row)
        rows.append(row)
    path = out / "amzn_full_output.csv"
    write_csv(path, rows, BASE_FIELDS)
    manifest = {
        "run_type": "full_output",
        "product": cfg.PRODUCT,
        "targets": len(targets),
        "detail_rows": len(details),
        "output_rows": len(rows),
        "batch_id": meta["batch_id"],
        "output": str(path),
    }
    write_json(out / "step09_full_output_manifest.json", manifest)
    print(f"[full/{cfg.PRODUCT}] rows={len(rows)} detail_rows={len(details)}")
    return manifest
