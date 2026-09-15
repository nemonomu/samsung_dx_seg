"""Step01: collect the MediaMarkt TV listing into a per-SKU CSV.

Iterates ?page=1..N through ZenRows (DE proxy, no JS), parses each page's
__PRELOADED_STATE__ + JSON-LD into Main-page fields, dedups by sku_id preserving
display order, and stops once MAIN_TARGET_UNIQUE unique SKUs are collected.

  python MMKT/step01_listing.py                 # 300 SKUs, Beste Ergebnisse sort
  python MMKT/step01_listing.py --sort bsr      # Topseller order (for bsr_rank)
  python MMKT/step01_listing.py --target 36

Raw HTML for each page is saved under references/listing/<stamp>/ for
reproducibility. Inactive timestamped raw-HTML directories older than 48 hours
are removed before a new listing run; parsed rows go to
data/output/mmkt_listing_<sort>.csv + manifest.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import importlib

from common.config import REFERENCES_ROOT, ensure_dirs, page_url, write_json
from common.listing_retention import LISTING_ARCHIVE_RETENTION_HOURS, cleanup_listing_archives
from common.listing_policy import is_advertisement, listing_exclusion_reason
from common.parsers import extract_listing_pagination, extract_sponsored_diagnostics, parse_listing_html


def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")

CSV_COLUMNS = [
    "position",
    "rank",
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
                   help="deprecated and ignored; collect until target or actual last page")
    p.add_argument("--page-retries", type=int, default=2,
                   help="retry a failed/repeated page without skipping to the next page")
    p.add_argument("--sleep", type=float, default=1.0)
    p.add_argument("--render-settle", type=float, default=15.0,
                   help="seconds to wait for lazy sponsored listing cards before capture")
    p.add_argument("--sponsored-extra-wait", type=float, default=20.0,
                   help="extra seconds when Gesponsert placeholders still have no product link")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--transport", choices=["uc", "zenrows"], default="uc",
                   help="uc = local undetected-chromedriver (no ZenRows); zenrows = legacy")
    p.add_argument("--output", default="")
    return p.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def collect_pages(fetch_page, *, product: str, target: int, retries: int = 2,
                  sleep_s: float = 0, save_page=None) -> tuple[list[dict], list[dict], str]:
    """Count eligible unique products; only explicit pagination proves the end."""
    seen: dict[str, dict[str, Any]] = {}
    fingerprints: set[tuple[str, ...]] = set()
    page_log = []
    page = 1
    while True:
        for attempt in range(1, max(0, retries) + 2):
            print(f"[step01] page={page} attempt={attempt} unique={len(seen)}/{target}", flush=True)
            try:
                html, status, elapsed, error, extra_wait = fetch_page(page)
            except Exception as exc:
                html, status, elapsed, error, extra_wait = "", None, 0, type(exc).__name__, 0
            if save_page is not None:
                save_page(page, attempt, html)
            diagnostics: dict[str, Any] = {}
            pagination = {"listing_valid": False, "last_page": False, "has_next": None}
            rows = []
            if status == 200:
                try:
                    rows = parse_listing_html(html, page=page, diagnostics=diagnostics)
                    pagination = extract_listing_pagination(html)
                except Exception as exc:
                    error = "parse_failed: " + type(exc).__name__
            fingerprint = tuple(dict.fromkeys(
                str(row.get("sku_id") or "").strip() for row in rows
                if row.get("sku_id") and not is_advertisement(row)
            ))
            signature = fingerprint or (
                "empty_listing", str(pagination.get("shown")), str(pagination.get("total"))
            )
            failure = None
            if status != 200 or error:
                failure = "fetch_failed"
            elif not pagination["listing_valid"]:
                failure = "parse_failed"
            elif signature in fingerprints:
                failure = "repeated_page"
            elif not fingerprint and not pagination["last_page"] and pagination["has_next"] is not True:
                failure = "parse_failed"
            elif any(not row.get("product_url") for row in rows if not is_advertisement(row)):
                failure = "parse_failed"

            excluded: dict[str, int] = {}
            new = 0
            if not failure:
                fingerprints.add(signature)
                for row in rows:
                    reason = listing_exclusion_reason(row, product)
                    if reason:
                        excluded[reason] = excluded.get(reason, 0) + 1
                        continue
                    sku_id = str(row.get("sku_id") or "").strip()
                    if sku_id and sku_id not in seen:
                        row["sku_id"] = sku_id
                        row["page"] = page
                        seen[sku_id] = row
                        new += 1
            page_log.append({
                "page": page, "attempt": attempt, "status": status, "parsed": len(rows),
                "new_unique": new, "elapsed": elapsed, "sponsored_extra_wait": extra_wait,
                "error": error or failure, "excluded": excluded, **diagnostics, **pagination,
            })
            if not failure:
                break
            if attempt == max(0, retries) + 1:
                return list(seen.values())[:target], page_log, failure
            if sleep_s > 0:
                time.sleep(sleep_s)
        if len(seen) >= target:
            return list(seen.values())[:target], page_log, "target_reached"
        if pagination["last_page"]:
            return list(seen.values()), page_log, "last_page"
        page += 1
        if sleep_s > 0:
            time.sleep(sleep_s)


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    cfg = load_cfg(args.product)
    ensure_dirs(cfg.OUTPUT_ROOT, REFERENCES_ROOT)

    target = args.target or (cfg.BSR_TARGET_RANK if args.sort == "bsr" else cfg.MAIN_TARGET_UNIQUE)
    base_url = cfg.BSR_URL if args.sort == "bsr" else cfg.LISTING_URL
    if target <= 0:
        raise ValueError("listing target must be positive")
    stamp = now_stamp()
    # Run meta mirrors OTTO step09 (batch_id prefix "m_" for MediaMarkt).
    run_now = datetime.now()
    crawl_dt = run_now.strftime("%Y-%m-%d %H:%M:%S")
    run_meta = {
        "crawl_strdatetime": crawl_dt,
        "calendar_week": "w" + str(run_now.isocalendar().week),
        "batch_id": "m_" + run_now.strftime("%Y%m%d_%H%M%S"),
    }
    listing_root = REFERENCES_ROOT / "listing"
    cleanup = cleanup_listing_archives(listing_root)
    if cleanup.deleted:
        print(f"[step01][cleanup] removed={len(cleanup.deleted)} "
              f"retention_hours={LISTING_ARCHIVE_RETENTION_HOURS}")
    if cleanup.skipped_recently_modified:
        print(f"[step01][cleanup] active_old_dirs_kept={len(cleanup.skipped_recently_modified)}")
    for error in cleanup.errors:
        print(f"[step01][cleanup][WARN] {error}")

    raw_dir = listing_root / f"{args.sort}_{stamp}"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Transport: local UC (default, ZenRows-free) or legacy ZenRows GET.
    uc_session = None
    if args.transport == "uc":
        from common.uc import UcSession
        uc_session = UcSession()
        uc_session.open()
        print(f"[step01] transport=uc warmup={uc_session.warmup_status}")

    def fetch_page(url: str) -> tuple[str, int, float, str | None, float]:
        t0 = time.perf_counter()
        if uc_session is not None:
            r = uc_session.navigate(url, settle_s=max(0.0, args.render_settle))
            html = r["html"]
            extra_waited = 0.0
            if not r["blocked"] and "__PRELOADED_STATE__" in html:
                early = extract_sponsored_diagnostics(html)
                unresolved = int(early.get("unmapped_label_occurrences") or 0)
                if unresolved and args.sponsored_extra_wait > 0:
                    extra_waited = max(0.0, args.sponsored_extra_wait)
                    print(
                        f"[step01] Sponsored placeholders={unresolved}; "
                        f"waiting {extra_waited:.1f}s for product links ...",
                        flush=True,
                    )
                    time.sleep(extra_waited)
                    html = uc_session.driver.page_source or ""
            ok = 200 if (not r["blocked"] and "__PRELOADED_STATE__" in html) else 403
            return html, ok, round(time.perf_counter() - t0, 2), r["error"], extra_waited
        from common.zenrows import fetch_via_universal
        res = fetch_via_universal(url, timeout=args.timeout, proxy_country="de")
        body = res["body"]
        return body.decode("utf-8", errors="replace"), res["status"], res["elapsed"], res["error"], 0.0

    def save_page(page: int, attempt: int, html: str) -> None:
        suffix = "" if attempt == 1 else f"_attempt_{attempt}"
        (raw_dir / f"page_{page:02d}{suffix}.html").write_text(html, encoding="utf-8")

    try:
        ordered, page_log, stop_reason = collect_pages(
            lambda page: fetch_page(page_url(base_url, page)), product=args.product,
            target=target, retries=args.page_retries, sleep_s=args.sleep, save_page=save_page,
        )
    finally:
        if uc_session is not None:
            uc_session.close()
    success = stop_reason in {"target_reached", "last_page"}
    # renumber position 1..N contiguously after dedup/trim
    for i, row in enumerate(ordered, start=1):
        row.update(run_meta)
        row["position"] = i
        row["rank"] = i

    mapped_sponsored_ids = sorted({
        str(pid)
        for page_info in page_log
        for pid in page_info.get("sponsored_product_ids", [])
    })
    unmatched_sponsored_ids = sorted({
        str(pid)
        for page_info in page_log
        for pid in page_info.get("unmatched_sponsored_ids", [])
    })
    raw_gesponsert_occurrences = sum(
        int(page_info.get("raw_gesponsert_occurrences") or 0)
        for page_info in page_log
    )
    final_sponsored_rows = sum(
        1 for row in ordered if row.get("sku_status") == "Sponsored"
    )
    listing_sponsored_labels = sum(
        int(page_info.get("visible_label_occurrences") or 0)
        for page_info in page_log
    )
    sponsored_monitoring = {
        "raw_gesponsert_occurrences": raw_gesponsert_occurrences,
        "visible_label_occurrences": listing_sponsored_labels,
        "mapped_label_occurrences": sum(
            int(page_info.get("mapped_label_occurrences") or 0)
            for page_info in page_log
        ),
        "unmapped_label_occurrences": sum(
            int(page_info.get("unmapped_label_occurrences") or 0)
            for page_info in page_log
        ),
        "mapped_product_id_count": len(mapped_sponsored_ids),
        "mapped_product_ids": mapped_sponsored_ids,
        "unmatched_product_id_count": len(unmatched_sponsored_ids),
        "unmatched_product_ids": unmatched_sponsored_ids,
        "parsed_sponsored_rows": sum(
            int(page_info.get("parsed_sponsored_rows") or 0)
            for page_info in page_log
        ),
        "final_sponsored_rows": final_sponsored_rows,
        "excluded_rows": sum(info.get("excluded", {}).get("advertisement", 0) for info in page_log),
        "warning_zero_collected": False,
        "warning_advertisements_collected": bool(final_sponsored_rows),
    }

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
        "pages_fetched": len({info["page"] for info in page_log}),
        "request_attempts": len(page_log),
        "unique_collected": sum(info["new_unique"] for info in page_log),
        "written_rows": len(ordered),
        "success": success,
        "stop_reason": stop_reason,
        "target_reached": len(ordered) >= target,
        "excluded_product_rows": sum(
            sum(count for reason, count in info.get("excluded", {}).items() if reason != "advertisement")
            for info in page_log
        ),
        "raw_dir": str(raw_dir.relative_to(REFERENCES_ROOT.parent)),
        "output_csv": str(out_path),
        "sponsored_monitoring": sponsored_monitoring,
        "pages": page_log,
    }
    manifest_path = cfg.OUTPUT_ROOT / f"mmkt_step01_listing_{args.sort}_manifest.json"
    write_json(manifest_path, manifest)

    print(f"[step01] DONE written={len(ordered)} reason={stop_reason} -> {out_path}")
    print(f"[step01] manifest={manifest_path}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
