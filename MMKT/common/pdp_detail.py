"""Step02: collect PDP detail fields (SEG No.37-48) for the listing targets.

Reads the step01 listing CSV, drives one warmed browser session
(PdpBrowserSession) over each product URL, and merges the SSR fields
(parse_pdp_html) with the three lazy GraphQL responses (KI summary, top-20
reviews, Alternativen) into one row per SKU. Writes mmkt_pdp_detail.csv +
manifest. Joins back to listing by sku_id in the later full-output step.

  python MMKT/step02_pdp_detail.py                 # all listing targets
  python MMKT/step02_pdp_detail.py --limit 5       # smoke test first 5
  python MMKT/step02_pdp_detail.py --input <csv> --start 50 --limit 50
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import importlib

from common.config import REFERENCES_ROOT, ensure_dirs, write_json
from common.parsers import (
    parse_comparison_detail,
    parse_pdp_html,
    parse_product_reviews,
    parse_reviews_summary,
)
def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")


def make_session(transport: str, review_pages: int):
    """Build a PDP session for the chosen transport (both share the same API:
    open / fetch_pdp_detail / reconnect / close)."""
    if transport == "uc":
        from common.uc import UcSession
        return UcSession(review_pages=review_pages)
    from common.pdp_browser import PdpBrowserSession
    return PdpBrowserSession(review_pages=review_pages)

def csv_columns(cfg):
    return [
        "rank", "sku_id", "product_url",
        "delivery_availability", "delivery_availability_en",
        "pick_up_availability", "pick_up_availability_en",
        "sku", *cfg.SPEC_FIELDS,
        "star_rating", "count_of_star_ratings", "count_of_reviews",
        "summarized_review_content", "retailer_sku_name_similar", "detailed_review_content",
        "nav_status", "gql_summary", "gql_reviews", "gql_comparison",
        "attempts", "fetch_error", "crawl_strdatetime",
    ]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Collect MediaMarkt PDP detail fields.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    p.add_argument("--input", default="")
    p.add_argument("--bsr", default="",
                   help="BSR listing CSV; its BSR-only SKUs are unioned into the targets")
    p.add_argument("--output", default="")
    p.add_argument("--start", type=int, default=1, help="1-based start index into targets")
    p.add_argument("--limit", type=int, default=0, help="max targets (0 = all)")
    p.add_argument("--sleep", type=float, default=0.0)
    p.add_argument("--review-pages", type=int, default=2)
    p.add_argument("--transport", choices=["uc", "zenrows"], default="uc",
                   help="uc = local undetected-chromedriver (no ZenRows); zenrows = legacy")
    p.add_argument("--concurrency", type=int, default=2,
                   help="parallel browser sessions (UC = local Chrome windows)")
    p.add_argument("--max-retries", type=int, default=2,
                   help="reconnect+retry attempts per PDP when the session drops")
    p.add_argument("--resume", action="store_true",
                   help="keep rows already collected (specs present) and re-fetch only the rest")
    return p.parse_args()


def merge_detail(html: str, detail: dict[str, Any], sku_id: str, cfg) -> dict[str, Any]:
    """Build one detail row. GraphQL-only: the comparison response carries specs/
    delivery/pickup/ratings/similar; reviews + summary come alongside. Falls back
    to SSR-HTML parsing only if the comparison response is empty AND html exists."""
    row = parse_comparison_detail(detail.get("comparison_resp"), sku_id, cfg)
    if not row:
        row = parse_pdp_html(html, sku_id, cfg)
    reviews = parse_product_reviews(detail.get("review_resps") or [])
    summary = parse_reviews_summary(detail.get("summary_resp"))
    # Reviews query is authoritative for counts + the top-20 written reviews.
    if reviews.get("count_of_star_ratings") is not None:
        row["count_of_star_ratings"] = reviews["count_of_star_ratings"]
    row["count_of_reviews"] = reviews.get("count_of_reviews")
    if reviews.get("detailed_review_content"):
        row["detailed_review_content"] = reviews["detailed_review_content"]
    row["summarized_review_content"] = summary
    return row


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    cfg = load_cfg(args.product)
    ensure_dirs(cfg.OUTPUT_ROOT)
    input_csv = args.input or str(cfg.OUTPUT_ROOT / "mmkt_listing_main.csv")
    bsr_csv = args.bsr or str(cfg.OUTPUT_ROOT / "mmkt_listing_bsr.csv")

    with open(input_csv, encoding="utf-8-sig") as fh:
        targets = list(csv.DictReader(fh))
    # Union in BSR-only SKUs (top sellers that fall outside the main listing) so
    # they also get PDP detail. Main rows keep their order; BSR-only ones append.
    seen_ids = {(t.get("sku_id") or "").strip() for t in targets}
    bsr_path = Path(bsr_csv)
    if bsr_path.exists():
        with bsr_path.open(encoding="utf-8-sig") as fh:
            extra = [r for r in csv.DictReader(fh)
                     if (r.get("sku_id") or "").strip() not in seen_ids]
        if extra:
            print(f"[step02] union: +{len(extra)} BSR-only SKUs added to targets")
            targets += extra
    start = max(args.start, 1) - 1
    end = len(targets) if args.limit <= 0 else min(len(targets), start + args.limit)
    selected = targets[start:end]
    crawl_dt = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    out_path = Path(args.output) if args.output else cfg.OUTPUT_ROOT / "mmkt_pdp_detail.csv"
    # attach a stable rank/index to each target up front
    valid = [
        (i, t) for i, t in enumerate(selected, start=start + 1)
        if (t.get("sku_id") or "").strip() and (t.get("product_url") or "").strip()
    ]

    # Resume: keep previously collected good rows; only re-fetch sku_ids still missing.
    kept_rows: list[dict[str, Any]] = []
    spec0 = cfg.SPEC_FIELDS[0]  # product's first spec column = "row has detail" marker
    if args.resume and out_path.exists():
        with open(out_path, encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if (r.get(spec0) or "").strip():
                    kept_rows.append(r)
        good_ids = {r["sku_id"] for r in kept_rows}
        valid = [(i, t) for i, t in valid if t["sku_id"].strip() not in good_ids]
        print(f"[step02] resume: kept {len(kept_rows)} good rows, {len(valid)} to (re)fetch")

    concurrency = max(1, args.concurrency)
    shards: list[list[tuple[int, dict]]] = [valid[w::concurrency] for w in range(concurrency)]
    rows: list[dict[str, Any]] = []
    page_log: list[dict[str, Any]] = []
    lock = threading.Lock()
    done = {"n": 0}
    total = len(valid)

    print(f"[step02] targets {start + 1}..{end} of {len(targets)}, {total} valid, "
          f"concurrency={concurrency} -> {out_path}")

    def worker(worker_id: int, shard: list[tuple[int, dict]]) -> None:
        if not shard:
            return
        try:
            session = make_session(args.transport, args.review_pages)
            session.open()
        except Exception as exc:
            with lock:
                print(f"[step02][w{worker_id}] session open FAILED: {exc!r}")
            return
        try:
            for i, t in shard:
                sku_id = t["sku_id"].strip()
                url = t["product_url"].strip()
                row: dict[str, Any] = {}
                last_err = None
                # Retry with a fresh session if the browser dropped (TargetClosedError)
                # or the navigation failed — a dead session fails instantly otherwise.
                for attempt in range(1, args.max_retries + 2):
                    try:
                        if session is None:
                            session = make_session(args.transport, args.review_pages)
                            session.open()
                        detail = session.fetch_pdp_detail(url, sku_id)
                        candidate = merge_detail(detail["html"], detail, sku_id, cfg)
                        ok = detail["gql_status"]
                        candidate.update({
                            "rank": t.get("rank") or t.get("position") or i,
                            "product_url": url, "nav_status": detail["nav_status"],
                            "gql_summary": ok["summary"],
                            "gql_reviews": ",".join(str(s) for s in ok["reviews"]),
                            "gql_comparison": ok["comparison"],
                            "fetch_error": detail.get("error"), "crawl_strdatetime": crawl_dt,
                            "attempts": attempt,
                        })
                        if candidate.get(spec0) or detail["nav_status"] == 200:
                            row = candidate
                            break
                        row = candidate  # keep last even if weak
                        last_err = detail.get("error")
                    except Exception as exc:
                        last_err = type(exc).__name__ + ": " + str(exc)
                    # failed or weak — rebuild the session before the next attempt
                    try:
                        session.reconnect()
                    except Exception:
                        try:
                            session.close()
                        except Exception:
                            pass
                        session = None
                if not row:
                    row = {"rank": i, "sku_id": sku_id, "product_url": url,
                           "fetch_error": last_err, "crawl_strdatetime": crawl_dt}
                specs_ok = bool(row.get(spec0))
                line = (f"sku={sku_id} nav={row.get('nav_status')} "
                        f"specs={'ok' if specs_ok else 'MISS'} "
                        f"sim={'y' if row.get('retailer_sku_name_similar') else 'n'} "
                        f"att={row.get('attempts', '-')}"
                        + ("" if specs_ok else f" ERR={str(last_err)[:50]}"))
                with lock:
                    rows.append(row)
                    inc_writer.writerow(row)   # crash-safe: persist each row as collected
                    inc_fh.flush()
                    page_log.append({"sku_id": sku_id, "nav_status": row.get("nav_status"),
                                     "specs_ok": specs_ok, "error": None if specs_ok else last_err})
                    done["n"] += 1
                    print(f"[step02][w{worker_id}] {done['n']:>3}/{total} {line}")
                if args.sleep > 0:
                    time.sleep(args.sleep)
        finally:
            if session is not None:
                session.close()

    # Open the CSV now and stream rows as they are collected, so a mid-run
    # interruption (e.g. VPN/IP drop → Cloudflare block) leaves a resumable file.
    inc_fh = out_path.open("w", encoding="utf-8-sig", newline="")
    inc_writer = csv.DictWriter(inc_fh, fieldnames=csv_columns(cfg), extrasaction="ignore")
    inc_writer.writeheader()
    for r in kept_rows:                 # resumed good rows persisted up front
        inc_writer.writerow(r)
    inc_fh.flush()

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for w, shard in enumerate(shards):
                pool.submit(worker, w, shard)
    finally:
        inc_fh.close()

    # Final clean pass: re-sort the full set by rank and rewrite tidily.
    rows.extend(kept_rows)  # resume: fold in previously good rows
    rows.sort(key=lambda r: int(r.get("rank") or 0))

    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_columns(cfg), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    filled = sum(1 for r in rows if r.get(spec0))
    with_sim = sum(1 for r in rows if r.get("retailer_sku_name_similar"))
    with_sum = sum(1 for r in rows if r.get("summarized_review_content"))
    manifest = {
        "run_type": "mmkt_step02_pdp_detail",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_csv": input_csv,
        "targets_range": [start + 1, end],
        "written_rows": len(rows),
        "rows_with_specs": filled,
        "rows_with_similar": with_sim,
        "rows_with_summary": with_sum,
        "output_csv": str(out_path),
        "pages": page_log,
    }
    write_json(cfg.OUTPUT_ROOT / "mmkt_step02_pdp_detail_manifest.json", manifest)
    print(f"[step02] DONE rows={len(rows)} specs={filled} similar={with_sim} summary={with_sum} -> {out_path}")
    return 0 if filled else 1


if __name__ == "__main__":
    raise SystemExit(main())
