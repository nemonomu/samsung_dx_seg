"""Step01: collect the MediaMarkt TV listing into a per-SKU CSV.

Iterates ?page=1..N through ZenRows (DE proxy, no JS), parses each page's
__PRELOADED_STATE__ + JSON-LD into Main-page fields, dedups by sku_id preserving
display order, and stops once MAIN_TARGET_UNIQUE unique SKUs are collected.

  python MMKT/step01_listing.py                 # 300 SKUs, Beste Ergebnisse sort
  python MMKT/step01_listing.py --sort bsr      # Topseller order (for bsr_rank)
  python MMKT/step01_listing.py --target 36 --max-pages 3

Raw HTML for each page is saved under references/listing/<stamp>/ for
reproducibility; parsed rows go to data/output/mmkt_listing_<sort>.csv + manifest.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import importlib

from common.config import LISTING_PAGE_SIZE, REFERENCES_ROOT, ensure_dirs, page_url, write_json
from common.parsers import parse_listing_html


def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")

CSV_COLUMNS = [
    "position",
    "page",
    "sku_id",
    "retailer_sku_name",
    "manufacturer",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "sku_status",
    "discount_type",
    "discount_type_en",
    "star_rating",
    "count_of_reviews",
    "is_available",
    "product_url",
    "crawl_strdatetime",
    "calendar_week",
    "batch_id",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect a MediaMarkt category listing into a CSV.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    p.add_argument("--sort", choices=["main", "bsr"], default="main",
                   help="main = Beste Ergebnisse; bsr = Topseller (salescount desc)")
    p.add_argument("--target", type=int, default=0,
                   help="stop after this many unique SKUs (0 = product default)")
    p.add_argument("--max-pages", type=int, default=0,
                   help="hard page cap (0 = derive from target)")
    p.add_argument("--sleep", type=float, default=1.0)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--transport", choices=["uc", "zenrows"], default="uc",
                   help="uc = local undetected-chromedriver (no ZenRows); zenrows = legacy")
    p.add_argument("--output", default="")
    return p.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    cfg = load_cfg(args.product)
    ensure_dirs(cfg.OUTPUT_ROOT, REFERENCES_ROOT)

    target = args.target or cfg.MAIN_TARGET_UNIQUE
    base_url = cfg.BSR_URL if args.sort == "bsr" else cfg.LISTING_URL
    max_pages = args.max_pages or max(1, math.ceil(target / LISTING_PAGE_SIZE) + 2)
    stamp = now_stamp()
    # Run meta mirrors OTTO step09 (batch_id prefix "m_" for MediaMarkt).
    run_now = datetime.now()
    crawl_dt = run_now.strftime("%Y-%m-%d %H:%M:%S")
    run_meta = {
        "crawl_strdatetime": crawl_dt,
        "calendar_week": "w" + str(run_now.isocalendar().week),
        "batch_id": "m_" + run_now.strftime("%Y%m%d_%H%M%S"),
    }
    raw_dir = REFERENCES_ROOT / "listing" / f"{args.sort}_{stamp}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    seen: dict[str, dict[str, Any]] = {}  # sku_id -> row (first occurrence wins)
    page_log: list[dict[str, Any]] = []
    bad_pages = 0

    # Transport: local UC (default, ZenRows-free) or legacy ZenRows GET.
    uc_session = None
    if args.transport == "uc":
        from common.uc import UcSession
        uc_session = UcSession()
        uc_session.open()
        print(f"[step01] transport=uc warmup={uc_session.warmup_status}")

    def fetch_page(url: str) -> tuple[str, int, float, str | None]:
        t0 = time.perf_counter()
        if uc_session is not None:
            r = uc_session.navigate(url)
            html = r["html"]
            ok = 200 if (not r["blocked"] and "__PRELOADED_STATE__" in html) else 403
            return html, ok, round(time.perf_counter() - t0, 2), r["error"]
        from common.zenrows import fetch_via_universal
        res = fetch_via_universal(url, timeout=args.timeout, proxy_country="de")
        body = res["body"]
        return body.decode("utf-8", errors="replace"), res["status"], res["elapsed"], res["error"]

    for page in range(1, max_pages + 1):
        url = page_url(base_url, page)
        print(f"[step01] sort={args.sort} page={page:>2}/{max_pages} fetching "
              f"(unique={len(seen)}/{target}) ...", flush=True)
        html, status, elapsed, err = fetch_page(url)
        (raw_dir / f"page_{page:02d}.html").write_text(html, encoding="utf-8")
        rows = parse_listing_html(html, page=page) if status == 200 else []
        new = 0
        for row in rows:
            sku_id = row.get("sku_id")
            if not sku_id or sku_id in seen:
                continue
            row["page"] = page
            row.update(run_meta)
            seen[sku_id] = row
            new += 1
        page_log.append({"page": page, "status": status, "parsed": len(rows),
                         "new_unique": new, "elapsed": elapsed, "error": err})
        print(f"[step01] sort={args.sort} page={page:>2} status={status} "
              f"parsed={len(rows):>2} new={new:>2} total_unique={len(seen)} ({elapsed}s)", flush=True)
        if status != 200 or not rows:
            bad_pages += 1
            if bad_pages >= 3:
                print(f"[step01] stopping: {bad_pages} consecutive empty/failed pages")
                break
        else:
            bad_pages = 0
        if len(seen) >= target:
            break
        if args.sleep > 0:
            time.sleep(args.sleep)

    if uc_session is not None:
        uc_session.close()

    ordered = sorted(seen.values(), key=lambda r: r["position"])[: target]
    # renumber position 1..N contiguously after dedup/trim
    for i, row in enumerate(ordered, start=1):
        row["rank"] = i

    out_path = Path(args.output) if args.output else cfg.OUTPUT_ROOT / f"mmkt_listing_{args.sort}.csv"
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in ordered:
            writer.writerow(row)

    manifest = {
        "run_type": f"mmkt_step01_listing_{args.sort}",
        "created_at": crawl_dt,
        "batch_id": run_meta["batch_id"],
        "calendar_week": run_meta["calendar_week"],
        "base_url": base_url,
        "sort": args.sort,
        "target": target,
        "pages_fetched": len(page_log),
        "unique_collected": len(seen),
        "written_rows": len(ordered),
        "raw_dir": str(raw_dir.relative_to(REFERENCES_ROOT.parent)),
        "output_csv": str(out_path),
        "pages": page_log,
    }
    manifest_path = cfg.OUTPUT_ROOT / f"mmkt_step01_listing_{args.sort}_manifest.json"
    write_json(manifest_path, manifest)

    print(f"[step01] DONE unique={len(seen)} written={len(ordered)} -> {out_path}")
    print(f"[step01] manifest={manifest_path}")
    return 0 if len(ordered) >= min(target, 1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
