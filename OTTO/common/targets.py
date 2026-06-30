"""Step02 (shared): build final targets from a listing using cfg.classify."""
from __future__ import annotations

import os
import re
from typing import Any

from common.io_util import category_output_root, read_csv, write_csv, write_json

MAIN_TARGET_UNIQUE = int(os.getenv("OTTO_MAIN_TARGET_UNIQUE", "300"))
BSR_TARGET_RANK = int(os.getenv("OTTO_BSR_TARGET_RANK", "100"))


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().casefold() or None


def _rank(row: dict) -> int:
    return _to_int(row.get("display_rank")) or _to_int(row.get("list_position")) or _to_int(row.get("row_index")) or 999999


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT.lower())
    rows = read_csv(out / "otto_listing_topseller_rows.csv")
    rows_sorted = sorted(rows, key=_rank)

    tv_rows, excluded = [], []
    for row in rows_sorted:
        is_target, reason = cfg.classify(row.get("retailer_sku_name"))
        annotated = dict(row, classify_reason=reason, is_target=is_target)
        (tv_rows if is_target else excluded).append(annotated)

    final_rows: list[dict] = []
    seen: set[str] = set()
    for row in tv_rows:
        key = _norm(row.get("retailer_sku_name"))
        if not key or key in seen:
            continue
        seen.add(key)
        rank = len(final_rows) + 1
        out_row = dict(row)
        out_row["main_rank"] = rank
        out_row["bsr_rank"] = rank if rank <= BSR_TARGET_RANK else ""
        out_row["page_type"] = "main"  # single topseller listing: every target is in main
        final_rows.append(out_row)
        if len(final_rows) >= MAIN_TARGET_UNIQUE:
            break

    final_csv = out / "otto_final_targets.csv"
    write_csv(final_csv, final_rows)
    write_csv(out / "otto_excluded_rows.csv", excluded)
    shortfall = max(0, MAIN_TARGET_UNIQUE - len(final_rows))
    manifest = {
        "run_type": "final_targets", "product": cfg.PRODUCT,
        "input_rows": len(rows), "excluded_rows": len(excluded),
        "final_target_rows": len(final_rows), "main_target_unique": MAIN_TARGET_UNIQUE,
        "main_target_shortfall": shortfall, "bsr_rank_limit": BSR_TARGET_RANK,
        "bsr_tagged_rows": sum(1 for r in final_rows if r.get("bsr_rank") not in (None, "")),
        "output": str(final_csv),
    }
    write_json(out / "step02_final_targets_manifest.json", manifest)
    print(f"[targets/{cfg.PRODUCT}] input={len(rows)} excluded={len(excluded)} final={len(final_rows)} shortfall={shortfall}")
    return manifest
