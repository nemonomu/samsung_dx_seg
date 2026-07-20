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
    p.add_argument("--sleep", type=float, default=0.5,
                   help="throttle between SKUs to stay under Cloudflare's GraphQL rate limit")
    p.add_argument("--review-pages", type=int, default=4,
                   help="review API pages to fetch (10/page); 4 → up to 40 fetched so the "
                        "written-only top-20 fills even when some pages have few written reviews")
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
    comparison_matched = row is not None
    if not row:
        row = parse_pdp_html(html, sku_id, cfg)
    if not row:
        row = {"sku_id": str(sku_id)}
    reviews = parse_product_reviews(detail.get("review_resps") or [])
    summary = parse_reviews_summary(detail.get("summary_resp"))
    # Comparison is the primary average; the reviews distribution is the
    # authoritative fallback when comparison reviewStatistics is absent.
    if row.get("star_rating") in (None, "") and reviews.get("star_rating") is not None:
        row["star_rating"] = reviews["star_rating"]
    # Reviews query is authoritative for counts + the top-20 written reviews.
    if reviews.get("count_of_star_ratings") is not None:
        row["count_of_star_ratings"] = reviews["count_of_star_ratings"]
    row["count_of_reviews"] = reviews.get("count_of_reviews")
    if reviews.get("detailed_review_content"):
        row["detailed_review_content"] = reviews["detailed_review_content"]
    row["summarized_review_content"] = summary
    row["_comparison_matched"] = comparison_matched
    return row


def backfill_missing_pdp_fields(
    row: dict[str, Any], html: str, sku_id: str, cfg
) -> tuple[bool, list[str]]:
    """Fill only blank SKU/spec fields from an exact-target SSR PDP response."""
    fields = ["sku", *cfg.SPEC_FIELDS]
    ssr_row = parse_pdp_html(html, sku_id, cfg)
    if not ssr_row:
        return False, []
    recovered: list[str] = []
    for field in fields:
        if row.get(field) in (None, "") and ssr_row.get(field) not in (None, ""):
            row[field] = ssr_row[field]
            recovered.append(field)
    return True, recovered


def needs_pdp_backfill(row: dict[str, Any], nav_status: Any, cfg) -> bool:
    """Backfill the reported primary-spec gap after a successful GQL response.

    This intentionally uses the product's primary detail marker. Treating every
    blank optional spec/SKU as a retry trigger would issue extra PDP requests for
    legitimate source-null fields and increase Cloudflare pressure.
    """
    return nav_status == 200 and row.get(cfg.SPEC_FIELDS[0]) in (None, "")


def detail_attempt_is_usable(
    nav_status: Any,
    comparison_matched: bool,
    ssr_attempted: bool,
    ssr_valid: bool,
) -> bool:
    """Reject HTTP-200 responses that contain neither target GQL nor target SSR."""
    if nav_status != 200:
        return False
    return comparison_matched or (ssr_attempted and ssr_valid)


def detail_attempt_is_rate_limited(
    nav_status: Any, ssr_attempted: bool, ssr_status: Any
) -> bool:
    """Treat either the comparison request or required SSR fallback as 429."""
    return nav_status == 429 or (ssr_attempted and ssr_status == 429)


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
            print(f"[step02] union: +{len(extra)} BSR-only SKUs added to targets", flush=True)
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
        print(f"[step02] resume: kept {len(kept_rows)} good rows, {len(valid)} to (re)fetch", flush=True)

    concurrency = max(1, args.concurrency)
    shards: list[list[tuple[int, dict]]] = [valid[w::concurrency] for w in range(concurrency)]
    rows: list[dict[str, Any]] = []
    page_log: list[dict[str, Any]] = []
    lock = threading.Lock()
    done = {"n": 0}
    total = len(valid)

    print(f"[step02] targets {start + 1}..{end} of {len(targets)}, {total} valid, "
          f"concurrency={concurrency} -> {out_path}", flush=True)

    def worker(worker_id: int, shard: list[tuple[int, dict]]) -> None:
        if not shard:
            return
        with lock:
            print(f"[step02][w{worker_id}] warming up session ({len(shard)} items)...", flush=True)
        try:
            session = make_session(args.transport, args.review_pages)
            session.open()
        except Exception as exc:
            with lock:
                print(f"[step02][w{worker_id}] session open FAILED: {exc!r}", flush=True)
            return
        diagnostic_logged = False
        try:
            for i, t in shard:
                sku_id = t["sku_id"].strip()
                url = t["product_url"].strip()
                row: dict[str, Any] = {}
                last_err = None
                # Retry with a fresh session if the browser dropped (TargetClosedError)
                # or the navigation failed — a dead session fails instantly otherwise.
                for attempt in range(1, args.max_retries + 2):
                    nav = None
                    last_err = None
                    ssr_attempted = False
                    ssr_valid = False
                    ssr_status: Any = None
                    try:
                        if session is None:
                            session = make_session(args.transport, args.review_pages)
                            session.open()
                        detail = session.fetch_pdp_detail(url, sku_id)
                        nav = detail["nav_status"]
                        candidate = merge_detail(detail["html"], detail, sku_id, cfg)
                        comparison_matched = bool(candidate.pop("_comparison_matched", False))
                        pdp_html: str | None = None
                        if needs_pdp_backfill(candidate, nav, cfg):
                            ssr_attempted = True
                            page_response = session.fetch_page_response(url)
                            ssr_status = page_response.get("status")
                            pdp_html = page_response.get("body") or ""
                            target_valid, recovered = backfill_missing_pdp_fields(
                                candidate, pdp_html, sku_id, cfg
                            )
                            ssr_valid = ssr_status == 200 and target_valid
                            remaining = [
                                field for field in ["sku", *cfg.SPEC_FIELDS]
                                if candidate.get(field) in (None, "")
                            ]
                            with lock:
                                print(
                                    f"[step02][w{worker_id}] ssr-backfill "
                                    f"sku={sku_id} status={ssr_status} "
                                    f"target={'ok' if target_valid else 'MISS'} "
                                    f"recovered={','.join(recovered) or '-'} "
                                    f"remaining={','.join(remaining) or '-'}",
                                    flush=True,
                                )
                        ok = detail["gql_status"]
                        candidate.update({
                            "rank": t.get("rank") or t.get("position") or i,
                            "product_url": url, "nav_status": nav,
                            "gql_summary": ok["summary"],
                            "gql_reviews": ",".join(str(s) for s in ok["reviews"]),
                            "gql_comparison": ok["comparison"],
                            "fetch_error": detail.get("error"), "crawl_strdatetime": crawl_dt,
                            "attempts": attempt,
                        })
                        if detail.get("error") and not diagnostic_logged:
                            with lock:
                                print(f"[step02][w{worker_id}][diag] sku={sku_id} "
                                      f"{detail['error']}", flush=True)
                            diagnostic_logged = True
                        if (
                            detail_attempt_is_usable(
                                nav, comparison_matched, ssr_attempted, ssr_valid
                            )
                            and not detail_attempt_is_rate_limited(
                                nav, ssr_attempted, ssr_status
                            )
                        ):
                            # Only current-attempt warnings belong on a successful
                            # row; errors from an earlier retry must not leak here.
                            last_err = None
                            if ssr_attempted and not ssr_valid:
                                last_err = f"ssr_backfill_failed status={ssr_status}"
                            elif ssr_attempted and candidate.get(spec0) in (None, ""):
                                last_err = f"field_missing_after_valid_ssr field={spec0}"
                            if last_err and not candidate.get("fetch_error"):
                                candidate["fetch_error"] = last_err
                            # Optional per-product recovery of a field that lives
                            # only in the PDP description body (REF ref_capacity).
                            # Lazy: fetch_page_text runs only if the field is empty.
                            recover = getattr(cfg, "recover_missing_from_description", None)
                            if recover is not None:
                                try:
                                    recover(
                                        candidate,
                                        lambda: pdp_html
                                        if pdp_html is not None
                                        else session.fetch_page_text(url),
                                    )
                                except Exception as exc:
                                    with lock:
                                        print(f"[step02][w{worker_id}] capacity recover failed "
                                              f"{sku_id}: {exc!r}", flush=True)
                            row = candidate
                            break
                        row = candidate  # keep last even if weak
                        ssr_diag = (
                            f"status={ssr_status} valid={ssr_valid}"
                            if ssr_attempted else "not_attempted"
                        )
                        last_err = detail.get("error") or (
                            f"semantic_detail_missing nav={nav} "
                            f"comparison_matched={comparison_matched} ssr={ssr_diag}"
                        )
                        candidate["fetch_error"] = last_err
                    except Exception as exc:
                        last_err = type(exc).__name__ + ": " + str(exc)
                    # Do not launch and warm another Chrome after the final
                    # allowed attempt; there is no subsequent request to use it.
                    if attempt >= args.max_retries + 1:
                        break
                    # 429 = Cloudflare rate-limit: BACK OFF and retry with the SAME
                    # session — reconnecting just hammers the IP harder (churn loop).
                    if detail_attempt_is_rate_limited(nav, ssr_attempted, ssr_status):
                        backoff = min(90, 20 * attempt)
                        with lock:
                            print(f"[step02][w{worker_id}] rate-limited (429) on {sku_id} — "
                                  f"backing off {backoff}s", flush=True)
                        time.sleep(backoff)
                    else:
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
                    print(f"[step02][w{worker_id}] {done['n']:>3}/{total} {line}", flush=True)
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
    print(f"[step02] DONE rows={len(rows)} specs={filled} similar={with_sim} summary={with_sum} -> {out_path}", flush=True)
    return 0 if filled else 1


if __name__ == "__main__":
    raise SystemExit(main())
