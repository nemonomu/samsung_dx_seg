"""Step02 (shared): build final targets from a listing using cfg.classify."""
from __future__ import annotations

import os
from typing import Any

from common.io_util import category_output_root, read_csv, read_json, write_csv, write_json

MAIN_TARGET_UNIQUE = int(os.getenv("OTTO_MAIN_TARGET_UNIQUE", "300"))
BSR_TARGET_RANK = int(os.getenv("OTTO_BSR_TARGET_RANK", "100"))


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rank(row: dict) -> int:
    return _to_int(row.get("display_rank")) or _to_int(row.get("list_position")) or _to_int(row.get("row_index")) or 999999


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT.lower())
    # A failed collection can leave a valid CSV from an older run in the output directory.
    listing_manifest = out / "step01_listing_manifest.json"
    source = read_json(listing_manifest) if listing_manifest.exists() else {}
    if source.get("success") is False or source.get("status") == "running":
        raise RuntimeError("Latest OTTO listing did not complete; rerun listing before selecting targets")
    rows = read_csv(out / "otto_listing_topseller_rows.csv")
    if source.get("run_id") and rows and {r.get("discount_sticker_run_id") for r in rows} != {source["run_id"]}:
        raise RuntimeError("OTTO listing CSV does not match the latest listing manifest")
    rows_sorted = sorted(rows, key=_rank)

    main: dict[tuple[str, str], dict] = {}
    bsr: dict[tuple[str, str], int] = {}
    excluded = []
    for row in rows_sorted:
        is_target, reason = cfg.classify(row.get("retailer_sku_name"))
        # Different options can share a title or product ID and still have different specifications.
        key = ((row.get("product_id") or "").strip(), (row.get("variation_id") or "").strip())
        if is_target and not all(key):
            is_target, reason = False, "missing_product_or_variation_id"
        annotated = dict(row, classify_reason=reason, is_target=is_target)
        if not is_target:
            excluded.append(annotated)
            continue
        if len(main) < MAIN_TARGET_UNIQUE:
            main.setdefault(key, annotated)
        # Keep the first MAIN exposure, but rank the same option if it also appears organically on screen.
        if (len(bsr) < BSR_TARGET_RANK and row.get("source") == "topseller_tilelist"
                and row.get("exposure_type") == "organic" and row.get("sku_status") != "Sponsored"):
            bsr.setdefault(key, len(bsr) + 1)

    final_rows = [
        dict(row, main_rank=rank, bsr_rank=bsr.get(key, ""), page_type="main")
        for rank, (key, row) in enumerate(main.items(), 1)
    ]

    final_csv = out / "otto_final_targets.csv"
    write_csv(final_csv, final_rows)
    write_csv(out / "otto_excluded_rows.csv", excluded)
    shortfall = max(0, MAIN_TARGET_UNIQUE - len(final_rows))
    manifest = {
        "run_type": "final_targets", "product": cfg.PRODUCT,
        "input_rows": len(rows), "excluded_rows": len(excluded),
        "final_target_rows": len(final_rows), "main_target_unique": MAIN_TARGET_UNIQUE,
        "main_target_shortfall": shortfall, "bsr_rank_limit": BSR_TARGET_RANK,
        "bsr_ranked_rows": len(bsr), "bsr_outside_main": len(bsr.keys() - main.keys()),
        "bsr_tagged_rows": len(bsr.keys() & main.keys()),
        "output": str(final_csv),
    }
    write_json(out / "step02_final_targets_manifest.json", manifest)
    print(f"[targets/{cfg.PRODUCT}] input={len(rows)} excluded={len(excluded)} final={len(final_rows)} shortfall={shortfall}")
    return manifest
