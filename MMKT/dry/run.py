"""python -m dry.run: render Main/BSR, fetch minimal detail, write 20-column CSV.

No DB connection or write by default. --check-table is read-only. --write-db
loads the last successful, unchanged output and never starts a new crawl.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path

from common.config import page_url, write_json
from common.listing import collect_pages
from common.parsers import extract_listing_pagination, extract_sponsored_diagnostics, parse_listing_html
from dry import config
from dry.database import file_digest, inspect_or_insert, load_validated_output
from dry.detail import CollectionError, DetailCollector
from dry.output import canonical_product_url, merge_rows, run_metadata, write_output
from dry.policy import exclusion_reason


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Standalone MMKT clothes dryer crawler (no DB write by default)")
    parser.add_argument("--target", type=int, default=config.MAIN_TARGET_UNIQUE)
    parser.add_argument("--bsr-target", type=int, default=config.BSR_TARGET_UNIQUE)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_ROOT)
    parser.add_argument("--page-retries", type=int, default=2)
    parser.add_argument("--render-settle", type=float, default=15)
    parser.add_argument("--sponsored-extra-wait", type=float, default=20)
    parser.add_argument("--captcha-wait", type=float, default=180)
    parser.add_argument("--sleep", type=float, default=.5)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-table", action="store_true", help="read-only schema check, no crawl")
    group.add_argument("--write-db", action="store_true", help="insert existing validated CSV, no crawl")
    parser.add_argument("--table", choices=["test", "production"], default="test")
    args = parser.parse_args(argv)
    if args.target <= 0 or args.bsr_target <= 0 or args.page_retries < 0 or min(
            args.render_settle, args.sponsored_extra_wait, args.captcha_wait, args.sleep) < 0:
        parser.error("targets must be positive; retry/wait settings must be nonnegative")
    return args


def wait_for_captcha(session, html: str, wait_s: float) -> str:
    if not session._blocked(html):
        return html
    print("[dry] CAPTCHA/접속 차단 감지. 열린 UC Chrome에서 직접 완료해 주세요. 제한 시간 후 미해결이면 실패 처리함.", flush=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        html = session.driver.page_source or ""
        if not session._blocked(html):
            return html
    raise CollectionError("CAPTCHA_OR_ACCESS_BLOCKED")


def make_fetcher(session, base_url: str, args):
    def fetch(page):
        start = time.perf_counter()
        response = session.navigate(page_url(base_url, page), settle_s=args.render_settle)
        html = wait_for_captcha(session, response.get("html") or "", args.captcha_wait)
        session._click_consent()
        html = session.driver.page_source or html
        extra_wait = 0
        diagnostics = extract_sponsored_diagnostics(html)
        if diagnostics.get("unmapped_label_occurrences") and args.sponsored_extra_wait:
            extra_wait = args.sponsored_extra_wait
            time.sleep(extra_wait)
            html = session.driver.page_source or ""
            diagnostics = extract_sponsored_diagnostics(html)
        # A positive counter and zero parsed rows is a parser failure, even if
        # the counter claims the last page. Never call that a short category.
        pagination = extract_listing_pagination(html)
        page_error = None
        if (pagination.get("shown") or 0) > 0 and not parse_listing_html(html, page=page):
            page_error = "positive_counter_without_products"
        if diagnostics.get("unmapped_label_occurrences"):
            page_error = "unresolved_sponsored_cards"
        # Render navigation does not expose the actual document HTTP status.
        # 200 is the existing collector's ready-page signal; parsing validates it.
        return html, 200, round(time.perf_counter() - start, 2), (
            "navigation_failed" if response.get("error") else page_error), extra_wait
    return fetch


def crawl(args, session) -> dict:
    meta = run_metadata()
    collector = DetailCollector(session, retries=args.page_retries, sleep_s=args.sleep)
    unknown = set()

    def eligibility(row):
        early = exclusion_reason(row, require_evidence=False)
        if early:
            return early
        sku_id = str(row.get("sku_id") or "").strip()
        if not sku_id:
            return "missing_product_id"
        canonical_product_url(row.get("product_url"), sku_id)
        # Do not cache banner decisions by ID: a standard occurrence is valid.
        detail = collector.get(sku_id, row)
        reason = exclusion_reason(row, features=detail["_features"], detail_name=detail["_detail_name"])
        if reason == "unconfirmed_product_type":
            unknown.add(sku_id)
        return reason

    listing_results = {}
    manifest = {"success": False, **meta, "pages": {}, "targets": {"main": args.target, "bsr": args.bsr_target}}
    for sort, url, target in (("main", config.LISTING_URL, args.target), ("bsr", config.BSR_URL, args.bsr_target)):
        rows, pages, reason = collect_pages(make_fetcher(session, url, args), product="dry", target=target,
                                            retries=args.page_retries, sleep_s=args.sleep, eligibility=eligibility)
        # Page logs contain only parsed product diagnostics/counters, no HTML/errors.
        manifest["pages"][sort] = pages
        manifest[sort] = {"rows": len(rows), "stop_reason": reason}
        if reason not in {"target_reached", "last_page"}:
            raise CollectionError("LISTING_FAILED:" + sort + ":" + reason)
        listing_results[sort] = rows
    if unknown:
        # An unknown classification must not masquerade as a genuinely short category.
        raise CollectionError("UNCONFIRMED_PRODUCT_TYPES")
    rows = merge_rows(listing_results["main"], listing_results["bsr"], collector.cache, meta)
    path = args.output_dir / "mmkt_dry_output.csv"
    write_output(path, rows)
    manifest.update(success=True, output_rows=len(rows), api_calls=collector.api_calls,
                    unique_detail_products=len(collector.cache), output_digest=file_digest(path),
                    missing_counts={key: sum(row[key] is None for row in rows) for key in config.COLUMNS if key != "id"})
    return manifest


def main(argv=None) -> int:
    args = parse_args(argv)
    table = config.DB_TABLE if args.table == "test" else config.PRODUCTION_TABLE
    session = None
    try:
        if args.check_table or args.write_db:
            rows = load_validated_output(args.output_dir / "mmkt_dry_output.csv") if args.write_db else None
            result = inspect_or_insert(table, rows)
            print(json.dumps({"table": ".".join(table), "readonly": not args.write_db, **result}, ensure_ascii=False))
            return 0
        # Invalidate a previous success before launch, including interruption.
        write_json(args.output_dir / "mmkt_dry_manifest.json", {"success": False, "state": "running"})
        from common.uc import UcSession
        session = UcSession()  # existing home warmup, language, cookies, API headers
        print("[dry] UC Chrome 실행 및 MediaMarkt 홈 접속 준비 중...", flush=True)
        # Existing UC's error messages can embed driver text. Keep only our summary.
        with contextlib.redirect_stdout(io.StringIO()):
            session.open()
        print("[dry] UC Chrome 준비 완료. Main/BSR 렌더링과 상품별 상세 API 수집 시작.", flush=True)
        wait_for_captcha(session, session.driver.page_source or "", args.captcha_wait)
        manifest = crawl(args, session)
        write_json(args.output_dir / "mmkt_dry_manifest.json", manifest)
        print(json.dumps({"success": True, "main": manifest["main"], "bsr": manifest["bsr"],
                          "output_rows": manifest["output_rows"], "api_calls": manifest["api_calls"],
                          "missing_counts": manifest["missing_counts"], "db_written": False}, ensure_ascii=False))
        return 0
    except Exception as exc:
        error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
        # A lost connection during COMMIT can leave the result uncertain.
        print(json.dumps({"success": False, "error": error,
                          "db_written": None if args.write_db else False}), flush=True)
        if not args.check_table and not args.write_db:
            write_json(args.output_dir / "mmkt_dry_manifest.json", {"success": False, "error": error})
        return 1
    finally:
        if session is not None:
            with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
                session.close()


if __name__ == "__main__":
    raise SystemExit(main())
