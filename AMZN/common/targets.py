"""Step02: merge main and BSR rows into unique detail targets."""
from __future__ import annotations

from typing import Any

from common.config import BSR_TARGET, LISTING_TARGET
from common.io_util import category_output_root, read_csv, write_csv, write_json


def _merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if value not in (None, "") and not merged.get(key):
            merged[key] = value
    if incoming.get("main_rank"):
        merged["main_rank"] = incoming["main_rank"]
    if incoming.get("bsr_rank"):
        merged["bsr_rank"] = incoming["bsr_rank"]
    return merged


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT)
    main_rows = read_csv(out / "amzn_listing_main.csv")
    bsr_rows = read_csv(out / "amzn_listing_bsr.csv")
    by_asin: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in main_rows + bsr_rows:
        asin = (row.get("asin") or row.get("item") or "").strip()
        if not asin:
            continue
        if asin not in by_asin:
            by_asin[asin] = row
            order.append(asin)
        else:
            by_asin[asin] = _merge(by_asin[asin], row)
    rows = [by_asin[asin] for asin in order]
    path = out / "amzn_final_targets.csv"
    write_csv(path, rows)
    manifest = {
        "run_type": "targets",
        "product": cfg.PRODUCT,
        "main_rows": len(main_rows),
        "bsr_rows": len(bsr_rows),
        "unique_targets": len(rows),
        "main_target_unique": LISTING_TARGET,
        "bsr_rank_limit": BSR_TARGET,
        "main_target_shortfall": max(0, LISTING_TARGET - sum(1 for r in rows if r.get("main_rank"))),
        "output": str(path),
    }
    write_json(out / "step02_final_targets_manifest.json", manifest)
    print(f"[targets/{cfg.PRODUCT}] unique={len(rows)} main={len(main_rows)} bsr={len(bsr_rows)}")
    return manifest
