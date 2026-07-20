"""Step09: join listing + BSR + PDP detail into the final SEG 19-field output.

Offline join (no network) of the three collected CSVs:
  - mmkt_listing_main.csv   : Main fields + main_rank + run meta (batch_id m_…)
  - mmkt_listing_bsr.csv     : bsr_rank (Topseller position) by sku_id
  - mmkt_pdp_detail.csv      : PDP detail fields (No.37-48) by sku_id

Output columns match the shared DB table dx_seg.dx_seg_tv_retail_com plus the two
MMKT-only columns (pick_up_availability, model_year). MMKT does not collect
sku_popularity / recommendation_intent (OTTO-only) — those stay empty → NULL.
Written so REF/LDY product lines can reuse it via config (PRODUCT/ACCOUNT_NAME).

  python MMKT/step09_full_output.py
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import importlib

from common.config import ACCOUNT_NAME, COUNTRY, PAGE_TYPE, ensure_dirs, read_csv, write_json


def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")

# Final column order (mirrors OTTO full output + pick_up_availability + model_year).
def full_columns(cfg):
    return [
        "account_name", "product", "country", "page_type",
        "crawl_strdatetime", "calendar_week", "batch_id",
        "main_rank", "bsr_rank", "item", "product_url", "retailer_sku_name",
        "final_sku_price", "original_sku_price", "savings", "sku_popularity",
        "sku_status", "discount_type",
        "delivery_availability", "pick_up_availability", "sku",
        *cfg.SPEC_FIELDS,
        "retailer_sku_name_similar", "star_rating",
        "count_of_star_ratings", "count_of_reviews",
        "recommendation_intent", "summarized_review_content", "detailed_review_content",
    ]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Join MMKT listing + BSR + detail into the final output.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    p.add_argument("--listing", default="")
    p.add_argument("--bsr", default="")
    p.add_argument("--detail", default="")
    p.add_argument("--output", default="")
    return p.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def first(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, ""):
            return v
    return None


def resolve_rating_fields(
    detail: dict[str, Any],
    main_listing: dict[str, Any] | None,
    bsr_listing: dict[str, Any] | None,
) -> tuple[Any, Any, Any]:
    """Resolve ratings without mixing rating-count and review-count semantics.

    Listing JSON-LD's legacy count_of_reviews column is AggregateRating's
    reviewCount/ratingCount, so it is only a fallback for count_of_star_ratings.
    GetProductReviews.totalResults remains the sole count_of_reviews source.
    """
    m = main_listing or {}
    b = bsr_listing or {}
    listing_star = first(m.get("star_rating"), b.get("star_rating"))
    listing_rating_count = first(
        m.get("count_of_star_ratings"), m.get("count_of_reviews"),
        b.get("count_of_star_ratings"), b.get("count_of_reviews"),
    )
    star = first(detail.get("star_rating"), listing_star) or "0.0"
    n_star = first(detail.get("count_of_star_ratings"), listing_rating_count) or 0
    n_rev = first(detail.get("count_of_reviews")) or 0
    return star, n_star, n_rev


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    cfg = load_cfg(args.product)
    ensure_dirs(cfg.OUTPUT_ROOT)
    o = cfg.OUTPUT_ROOT
    args.listing = args.listing or str(o / "mmkt_listing_main.csv")
    args.bsr = args.bsr or str(o / "mmkt_listing_bsr.csv")
    args.detail = args.detail or str(o / "mmkt_pdp_detail.csv")
    args.output = args.output or str(o / "mmkt_full_output.csv")

    listing = read_csv(Path(args.listing))
    bsr = read_csv(Path(args.bsr))
    detail = read_csv(Path(args.detail))
    if not listing:
        print(f"[step09] no listing rows in {args.listing}; run step01 first.")
        return 1

    detail_by_id = {(d.get("sku_id") or "").strip(): d for d in detail if d.get("sku_id")}
    main_by_id = {(l.get("sku_id") or "").strip(): l for l in listing if l.get("sku_id")}
    bsr_by_id = {(b.get("sku_id") or "").strip(): b for b in bsr if b.get("sku_id")}

    # One batch per dataset: use the main listing run's meta for every row
    # (the BSR-only row was a separate pass but belongs to the same crawl batch).
    run_meta = {
        "crawl_strdatetime": listing[0].get("crawl_strdatetime"),
        "calendar_week": listing[0].get("calendar_week"),
        "batch_id": listing[0].get("batch_id"),
    }

    # Output is the UNION of main and BSR SKUs:
    #   in both   -> main_rank + bsr_rank
    #   main only -> main_rank only
    #   BSR only  -> bsr_rank only (still a row; listing fields come from the BSR CSV)
    union_ids = list(main_by_id)  # main first, in display order
    union_ids += [sid for sid in bsr_by_id if sid not in main_by_id]  # then BSR-only

    rows: list[dict[str, Any]] = []
    missing_detail = 0
    bsr_only = 0
    for sku_id in union_ids:
        m = main_by_id.get(sku_id)
        b = bsr_by_id.get(sku_id)
        l = m or b  # listing/Main fields: prefer the main row, else the BSR row
        if not m:
            bsr_only += 1
        d = detail_by_id.get(sku_id, {})
        if not d:
            missing_detail += 1
        star, n_star, n_rev = resolve_rating_fields(d, m, b)
        rows.append({
            "account_name": ACCOUNT_NAME, "product": cfg.PRODUCT, "country": COUNTRY,
            "page_type": PAGE_TYPE,
            **run_meta,
            "main_rank": first(m.get("rank"), m.get("position")) if m else "",
            "bsr_rank": (b.get("position") or "").strip() if b else "",
            "item": sku_id, "product_url": l.get("product_url"),
            "retailer_sku_name": l.get("retailer_sku_name"),
            "final_sku_price": l.get("final_sku_price"),
            "original_sku_price": l.get("original_sku_price"),
            "savings": l.get("savings"),
            "sku_popularity": "",            # OTTO-only field
            "sku_status": l.get("sku_status"),                # "Sponsored" (English)
            # [수집 후 번역 필요] fields -> English (_en) per spec + user
            "discount_type": first(l.get("discount_type_en"), l.get("discount_type")),
            "delivery_availability": first(d.get("delivery_availability_en"), d.get("delivery_availability")),
            "pick_up_availability": first(d.get("pick_up_availability_en"), d.get("pick_up_availability")),
            "sku": d.get("sku"),
            **{f: d.get(f) for f in cfg.SPEC_FIELDS},
            "retailer_sku_name_similar": d.get("retailer_sku_name_similar"),
            "star_rating": star,
            "count_of_star_ratings": n_star,
            "count_of_reviews": n_rev,
            "recommendation_intent": "",      # OTTO-only field
            "summarized_review_content": d.get("summarized_review_content"),
            "detailed_review_content": d.get("detailed_review_content"),
        })

    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=full_columns(cfg), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    batch_ids = sorted({(r.get("batch_id") or "").strip() for r in rows if r.get("batch_id")})
    with_bsr = sum(1 for r in rows if r.get("bsr_rank"))
    spec0 = cfg.SPEC_FIELDS[0]
    with_specs = sum(1 for r in rows if r.get(spec0))
    manifest = {
        "run_type": "mmkt_step09_full_output",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "product": cfg.PRODUCT, "account_name": ACCOUNT_NAME,
        "batch_ids": batch_ids,
        "output_rows": len(rows),
        "rows_with_bsr_rank": with_bsr,
        "rows_with_specs": with_specs,
        "rows_bsr_only": bsr_only,
        "rows_missing_detail": missing_detail,
        "output_csv": str(out_path),
    }
    write_json(cfg.OUTPUT_ROOT / "step09_full_output_manifest.json", manifest)
    print(f"[step09] rows={len(rows)} bsr={with_bsr} specs={with_specs} "
          f"bsr_only={bsr_only} missing_detail={missing_detail} batch={batch_ids} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
