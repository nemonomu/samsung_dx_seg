"""Standalone collection + production load; --collect-only never connects to DB.

--write-db loads the last validated CSV without crawling. --check-table is read-only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import time
from pathlib import Path
from datetime import datetime

from common.config import page_url
from common.listing import collect_pages
from common.parsers import extract_listing_pagination, extract_sponsored_diagnostics, parse_listing_html
from dry import config
from dry.database import DatabaseError, file_digest, inspect_or_insert, load_validated_output
from dry.detail import CollectionError, DetailCollector
from dry.output import canonical_product_url, default_missing_ratings, merge_rows, run_metadata, write_output
from dry.output import write_manifest as write_json
from dry.notify import notify_run
from dry.policy import exclusion_reason
from dry.events import RunLog, context, emit, failure, quiet_external


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Standalone MMKT DRY: collect and load production by default")
    parser.add_argument("--target", type=int, default=config.MAIN_TARGET_UNIQUE)
    parser.add_argument("--bsr-target", type=int, default=config.BSR_TARGET_UNIQUE)
    parser.add_argument("--output-dir", type=Path, default=config.OUTPUT_ROOT)
    parser.add_argument("--log-dir", type=Path, default=config.LOG_ROOT)
    parser.add_argument("--page-retries", type=int, default=2)
    parser.add_argument("--render-settle", type=float, default=15)
    parser.add_argument("--sponsored-extra-wait", type=float, default=20)
    parser.add_argument("--captcha-wait", type=float, default=180)
    parser.add_argument("--sleep", type=float, default=.5)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check-table", action="store_true", help="read-only schema check, no crawl")
    group.add_argument("--write-db", action="store_true", help="insert existing validated CSV, no crawl")
    group.add_argument("--collect-only", action="store_true", help="collect CSV only; no DB connection")
    parser.add_argument("--table", choices=["test", "production"], default="production")
    args = parser.parse_args(argv)
    if args.target <= 0 or args.bsr_target <= 0 or args.page_retries < 0 or min(
            args.render_settle, args.sponsored_extra_wait, args.captcha_wait, args.sleep) < 0:
        parser.error("targets must be positive; retry/wait settings must be nonnegative")
    return args


def wait_for_captcha(session, html: str, wait_s: float) -> str:
    if not session._blocked(html):
        return html
    emit("captcha_wait", wait_s=wait_s, note="열린 UC Chrome에서 직접 완료해 주세요. 미해결 시 수집 실패 처리함.")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
        html = session.driver.page_source or ""
        if not session._blocked(html):
            return html
    raise CollectionError("CAPTCHA_OR_ACCESS_BLOCKED")


def make_fetcher(session, base_url: str, args, sort="main", progress_state=None):
    attempts = {}
    def fetch(page):
        start = time.perf_counter()
        attempts[page] = attempts.get(page, 0) + 1
        context("listing", sort=sort, page=page, attempt=attempts[page],
                **(progress_state() if progress_state else {}))
        emit("page_start", render_wait_s=args.render_settle)
        try:
            with quiet_external():
                response = session.navigate(page_url(base_url, page), settle_s=args.render_settle)
            html = wait_for_captcha(session, response.get("html") or "", args.captcha_wait)
            with quiet_external():
                session._click_consent()
        except Exception as exc:
            failure(exc, code=str(exc) if isinstance(exc, CollectionError) else "LISTING_NAVIGATION_FAILED")
            raise
        html = session.driver.page_source or html
        extra_wait = 0
        diagnostics = extract_sponsored_diagnostics(html)
        if diagnostics.get("unmapped_label_occurrences") and args.sponsored_extra_wait:
            extra_wait = args.sponsored_extra_wait
            emit("sponsored_wait", wait_s=extra_wait, unresolved=diagnostics["unmapped_label_occurrences"])
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
        emit("page_ready", shown=pagination.get("shown"), total=pagination.get("total"),
             last_page=pagination.get("last_page"),
             error="navigation_failed" if response.get("error") else page_error)
        # Render navigation does not expose the actual document HTTP status.
        # 200 is the existing collector's ready-page signal; parsing validates it.
        return html, 200, round(time.perf_counter() - start, 2), (
            "navigation_failed" if response.get("error") else page_error), extra_wait
    return fetch


def crawl(args, session, meta=None) -> dict:
    meta = meta or run_metadata()
    collector = DetailCollector(session, retries=args.page_retries, sleep_s=args.sleep)
    unknown = set()
    accepted = set()
    counters = {}
    sort = "main"
    target = args.target
    current_page = 1

    def progress_state():
        return {"accepted": len(accepted), "target": target,
                "excluded_count": sum(count for reason, count in counters.items() if reason != "duplicate"),
                "duplicates": counters.get("duplicate", 0)}

    def eligibility(row):
        sku_id = str(row.get("sku_id") or "").strip()
        # common.collect_pages attaches row['page'] AFTER calling eligibility.
        # Track the fetcher's page locally; leave the shared collector untouched.
        context("listing_product", sort=sort, page=current_page, item=sku_id, **progress_state())
        early = exclusion_reason(row, require_evidence=False)
        if early:
            counters[early] = counters.get(early, 0) + 1
            emit("product_excluded", item=row.get("sku_id"), reason=early, excluded=counters)
            return early
        if not sku_id:
            return "missing_product_id"
        canonical_product_url(row.get("product_url"), sku_id)
        if sku_id in accepted:
            counters["duplicate"] = counters.get("duplicate", 0) + 1
            emit("product_duplicate", item=sku_id, duplicates=counters["duplicate"])
            return "duplicate"
        # Do not cache banner decisions by ID: a standard occurrence is valid.
        context("detail", sort=sort, page=current_page, item=sku_id, **progress_state())
        emit("detail_start", cached=sku_id in collector.cache, detail_products=len(collector.cache))
        detail = collector.get(sku_id, row)
        reason = exclusion_reason(row, features=detail["_features"], detail_name=detail["_detail_name"])
        if reason == "unconfirmed_product_type":
            unknown.add(sku_id)
        if reason:
            counters[reason] = counters.get(reason, 0) + 1
            emit("product_excluded", item=sku_id, reason=reason, excluded=counters)
        else:
            accepted.add(sku_id)
            emit("product_accepted", item=sku_id, accepted=len(accepted), target=target,
                 detail_products=len(collector.cache), api_calls=collector.api_calls)
        return reason

    listing_results = {}
    manifest = {"success": False, **meta, "pages": {}, "targets": {"main": args.target, "bsr": args.bsr_target}}
    for sort, url, target in (("main", config.LISTING_URL, args.target), ("bsr", config.BSR_URL, args.bsr_target)):
        accepted = set()
        counters = {}
        context("listing", sort=sort, target=target)
        emit("listing_start", accepted=0, target=target)
        fetch = make_fetcher(session, url, args, sort, progress_state=progress_state)
        def fetch_page(page):
            nonlocal current_page
            current_page = page
            return fetch(page)
        rows, pages, reason = collect_pages(fetch_page, product="dry", target=target,
                                            retries=args.page_retries, sleep_s=args.sleep, eligibility=eligibility)
        # Page logs contain only parsed product diagnostics/counters, no HTML/errors.
        manifest["pages"][sort] = pages
        manifest[sort] = {"rows": len(rows), "stop_reason": reason}
        emit("listing_complete", sort=sort, accepted=len(rows), target=target, stop_reason=reason,
             pages=len(pages), excluded=counters)
        for page in pages:
            emit("page_summary", sort=sort, **page)
        if reason not in {"target_reached", "last_page"}:
            raise CollectionError("LISTING_FAILED:" + sort + ":" + reason)
        listing_results[sort] = rows
    if unknown:
        # An unknown classification must not masquerade as a genuinely short category.
        raise CollectionError("UNCONFIRMED_PRODUCT_TYPES")
    rows = merge_rows(listing_results["main"], listing_results["bsr"], collector.cache, meta)
    path = args.output_dir / "mmkt_dry_output.csv"
    context("csv_save", batch_id=meta["batch_id"])
    write_output(path, rows)
    manifest.update(success=True, output_rows=len(rows), api_calls=collector.api_calls,
                    unique_detail_products=len(collector.cache), output_digest=file_digest(path),
                    missing_counts={key: sum(row[key] is None for row in rows) for key in config.COLUMNS if key != "id"})
    emit("csv_saved", path=str(path), rows=len(rows), api_calls=collector.api_calls,
         missing_counts=manifest["missing_counts"])
    return manifest


def load_output(args, table):
    context("output_validation", table=".".join(table))
    path = args.output_dir / "mmkt_dry_output.csv"
    rows = load_validated_output(path)
    # Older validated files can have blank stars. Keep the file/digest unchanged.
    rows = [default_missing_ratings(row, source="validated_csv") for row in rows]
    manifest_path = args.output_dir / "mmkt_dry_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    emit("output_validated", batch_id=manifest.get("batch_id"), rows=len(rows), table=".".join(table))
    manifest["database"] = {"table": ".".join(table), "state": "running", "db_written": None}
    write_json(manifest_path, manifest)
    try:
        result = inspect_or_insert(table, rows)
    except BaseException as exc:
        exc.validated_output = True
        manifest["database"].update(state="failed", db_written=getattr(exc, "db_written", False),
                                    error=str(exc) if isinstance(exc, CollectionError) else type(exc).__name__)
        with contextlib.suppress(Exception):
            write_json(manifest_path, manifest)
        raise
    manifest["database"].update(state="success", db_written=True, **result)
    try:
        write_json(manifest_path, manifest)
    except Exception:
        exc = DatabaseError("DB_COMMITTED_MANIFEST_SAVE_FAILED", db_written=True)
        exc.validated_output = True
        raise exc from None
    return manifest, result


def main(argv=None) -> int:
    args = parse_args(argv)
    table = config.TEST_TABLE if args.table == "test" else config.PRODUCTION_TABLE
    session = None
    meta = run_metadata()
    collection_success = False
    db_written = False
    manifest = {"success": False, **meta}
    started = time.monotonic()
    notification = None

    def report(error=None):
        if args.collect_only or args.check_table:
            emit("mail_skipped", reason="collect_only" if args.collect_only else "check_table")
            return {}
        try:
            return notify_run(args.output_dir, manifest, table, collection_success=collection_success,
                              db_written=db_written, error=error,
                              elapsed_s=round(time.monotonic() - started, 1))
        except Exception as exc:
            failure(exc, code="MAIL_NOTIFICATION_FAILED")
            return {"failed": True, "sent": None, "state": "notification_error"}
    try:
        log = RunLog(args.log_dir)
    except Exception as exc:
        failure(exc, code="LOG_SETUP_FAILED")
        return 1  # no crawl or DB work without a writable log
    with log:
        start_meta = meta if not (args.check_table or args.write_db) else {"invocation_datetime": meta["crawl_datetime"]}
        emit("run_start", **start_meta, targets={"main": args.target, "bsr": args.bsr_target},
             table=".".join(table), mode="check_table" if args.check_table else
             "load_only" if args.write_db else "collect_only" if args.collect_only else "collect_and_load",
             log_path=str(log.path))
        try:
            if args.check_table:
                result = inspect_or_insert(table)
                emit("run_complete", success=True, readonly=True, db_written=False, **result)
                return 0
            if not args.write_db:
                write_json(args.output_dir / "mmkt_dry_manifest.json", {"success": False, "state": "running", **meta})
                context("browser_open", batch_id=meta["batch_id"])
                emit("browser_start", note="UC Chrome 실행 및 MediaMarkt 홈 접속 준비 중")
                from common.uc import UcSession
                with quiet_external():
                    session = UcSession()
                    session.open()
                emit("browser_ready")
                wait_for_captcha(session, session.driver.page_source or "", args.captcha_wait)
                manifest = crawl(args, session, meta)
                manifest["timing"] = {"collection_elapsed_s": round(time.monotonic() - started, 1),
                                      "collection_completed_at": datetime.now().isoformat(timespec="seconds")}
                write_json(args.output_dir / "mmkt_dry_manifest.json", manifest)
                collection_success = True
                emit("collection_complete", **manifest["timing"], output_rows=manifest["output_rows"])
                with quiet_external():
                    session.close()
                session = None
            if not args.collect_only:
                manifest, result = load_output(args, table)
                collection_success = True
                db_written = True
            else:
                result = {}
            notification = report()
            context("complete", batch_id=manifest.get("batch_id"))
            emit("run_complete", success=not notification.get("failed", False), collection_success=collection_success,
                 db_success=True if db_written else None, db_written=db_written,
                 main=manifest.get("main"), bsr=manifest.get("bsr"), output_rows=manifest["output_rows"],
                 inserted=result.get("inserted", 0), already_loaded=result.get("already_loaded", False),
                 collection_elapsed_s=(manifest.get("timing") or {}).get("collection_elapsed_s"),
                 total_elapsed_s=round(time.monotonic() - started, 1), notification=notification)
            return 1 if notification.get("failed") else 0
        except BaseException as exc:
            error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            failure(exc, code=error)
            manifest_path = args.output_dir / "mmkt_dry_manifest.json"
            collection_success = collection_success or getattr(exc, "validated_output", False)
            db_written = getattr(exc, "db_written", db_written)
            if collection_success:
                with contextlib.suppress(Exception):
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.check_table and not args.write_db and not collection_success:
                manifest = {"success": False, "error": error, **meta}
                with contextlib.suppress(Exception):
                    write_json(manifest_path, manifest)
            notification = report(error)
            context("complete", batch_id=manifest.get("batch_id"))
            emit("run_complete", success=False, collection_success=collection_success,
                 db_success=db_written if collection_success and not args.collect_only else None,
                 error=error, db_written=db_written, csv_preserved=collection_success,
                 total_elapsed_s=round(time.monotonic() - started, 1), notification=notification)
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        finally:
            if session is not None:
                with contextlib.suppress(Exception), quiet_external():
                    session.close()


if __name__ == "__main__":
    raise SystemExit(main())
