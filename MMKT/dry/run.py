"""Standalone collection + production load; --collect-only never connects to DB.

--write-db loads the last validated CSV without crawling. --check-table is read-only.
"""
from __future__ import annotations

import argparse
import contextlib
import time
from pathlib import Path
from datetime import datetime

from common.config import page_url
from common.parsers import extract_listing_pagination, extract_sponsored_diagnostics, parse_listing_html
from dry import config
from dry.checkpoint import save_checkpoint
from dry.database import DatabaseError, file_digest, inspect_or_insert, load_validated_output
from dry.detail import CollectionError, DetailCollector
from dry.output import canonical_product_url, default_missing_ratings, merge_rows, run_metadata, write_output
from dry.output import write_manifest as write_json
from dry.notify import notify_run
from dry.policy import exclusion_reason
from dry.events import RunLog, context, emit, failure, quiet_external
from dry.listing import collect_pages


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
             raw_last_page=pagination.get("last_page"),
             error="navigation_failed" if response.get("error") else page_error)
        # Render navigation does not expose the actual document HTTP status.
        # 200 is the existing collector's ready-page signal; parsing validates it.
        return html, 200, round(time.perf_counter() - start, 2), (
            "navigation_failed" if response.get("error") else page_error), extra_wait
    return fetch


def initial_manifest(args, meta):
    table = config.TEST_TABLE if args.table == "test" else config.PRODUCTION_TABLE
    return {"artifact_kind": "final", "success": False, "state": "running", **meta,
            "collection_status": "running", "output_rows": 0,
            "targets": {"main": args.target, "bsr": args.bsr_target},
            "main": {"state": "not_started", "rows": None, "stop_reason": None},
            "bsr": {"state": "not_started", "rows": None, "stop_reason": None},
            "pages": {"main": [], "bsr": []}, "product_failure_count": 0, "product_failures": [],
            "csv": {"state": "not_created", "path": str(args.output_dir / "mmkt_dry_output.csv")},
            "database": {"state": "not_started", "table": ".".join(table), "db_written": False}}


def crawl(args, session, meta=None, *, manifest=None, started=None) -> dict:
    meta = meta or run_metadata()
    manifest = manifest if manifest is not None else initial_manifest(args, meta)
    started = time.monotonic() if started is None else started
    collector = DetailCollector(session, retries=args.page_retries, sleep_s=args.sleep)
    product_failures = []
    failed_items = set()
    accepted = set()
    counters = {}
    sort = "main"
    target = args.target
    current_page = 1

    def progress_state():
        return {"accepted": len(accepted), "target": target,
                "excluded_count": sum(count for reason, count in counters.items() if reason != "duplicate"),
                "duplicates": counters.get("duplicate", 0), "failed_products": len(failed_items)}

    def eligibility(row):
        sku_id = str(row.get("sku_id") or "").strip()
        # The listing collector attaches row['page'] AFTER calling eligibility.
        # Track the fetcher's page locally; leave the shared collector untouched.
        context("listing_product", sort=sort, page=current_page, item=sku_id, **progress_state())
        early = exclusion_reason(row, require_evidence=False)
        if early:
            counters[early] = counters.get(early, 0) + 1
            emit("product_excluded", item=row.get("sku_id"), reason=early, excluded=counters)
            return early
        if sku_id in accepted or sku_id in failed_items:
            counters["duplicate"] = counters.get("duplicate", 0) + 1
            emit("product_duplicate", item=sku_id, duplicates=counters["duplicate"])
            return "duplicate"
        # Do not cache banner decisions by ID: a standard occurrence is valid.
        context("detail", sort=sort, page=current_page, item=sku_id, **progress_state())
        emit("detail_start", cached=sku_id in collector.cache, detail_products=len(collector.cache))
        try:
            if not sku_id:
                raise CollectionError("MISSING_PRODUCT_ID")
            canonical_product_url(row.get("product_url"), sku_id)
            detail = collector.get(sku_id, row)
            reason = exclusion_reason(row, features=detail["_features"], detail_name=detail["_detail_name"])
            if reason == "unconfirmed_product_type":
                raise CollectionError("UNCONFIRMED_PRODUCT_TYPE")
        except Exception as exc:
            # Only individual product work is recoverable. Listing navigation,
            # parsing, CSV/DB failures and user interrupts retain their own paths.
            error = str(exc) if isinstance(exc, CollectionError) else "PRODUCT_PROCESSING_FAILED"
            failure(exc, code=error)
            failed_items.add(sku_id)
            record = {"sort": sort, "page": current_page, "item": sku_id,
                      "error": error, "error_type": type(exc).__name__}
            product_failures.append(record)
            emit("product_failed", **record, action="skip_and_continue", **progress_state())
            return "product_failed"
        if reason:
            counters[reason] = counters.get(reason, 0) + 1
            emit("product_excluded", item=sku_id, reason=reason, excluded=counters)
        else:
            accepted.add(sku_id)
            emit("product_accepted", item=sku_id, accepted=len(accepted), target=target,
                 detail_products=len(collector.cache), api_calls=collector.api_calls)
        return reason

    listing_results = {"main": [], "bsr": []}

    def refresh():
        manifest.update(output_rows=len({row["sku_id"] for values in listing_results.values() for row in values}),
                        api_calls=collector.api_calls, unique_detail_products=len(collector.cache),
                        product_failure_count=len(product_failures), product_failures=list(product_failures))
        manifest.setdefault("timing", {})["collection_elapsed_s"] = round(time.monotonic() - started, 1)

    def checkpoint(rows, pages, stop):
        listing_results[sort] = rows
        entry = pages[-1]
        listing = manifest[sort]
        complete = stop in {"target_reached", "last_page"}
        listing.update(rows=len(rows), state="complete" if complete else "failed" if stop else "collecting",
                       stop_reason=stop, failed_products=len(failed_items),
                       excluded=dict(counters), duplicates=counters.get("duplicate", 0),
                       last_requested_page=entry["page"], end_basis=entry.get("end_basis"))
        if not entry["error"]:
            listing["last_successful_page"] = entry.get("end_for_page", entry["page"])
        manifest["pages"][sort] = list(pages)
        refresh()
        emit("page_summary", sort=sort, **entry)
        context("intermediate_save", sort=sort, page=entry["page"], batch_id=meta["batch_id"])
        snapshot_rows = merge_rows(listing_results["main"], listing_results["bsr"], collector.cache, meta,
                                   log_defaults=False)
        save_checkpoint(args.output_dir, manifest, snapshot_rows, sort=sort,
                        page=entry["page"], attempt=entry["attempt"])

    try:
        for sort, url, target in (("main", config.LISTING_URL, args.target), ("bsr", config.BSR_URL, args.bsr_target)):
            accepted, failed_items, counters = set(), set(), {}
            manifest[sort].update(state="collecting", rows=0)
            context("listing", sort=sort, target=target)
            emit("listing_start", accepted=0, target=target)
            fetch = make_fetcher(session, url, args, sort, progress_state=progress_state)
            def fetch_page(page):
                nonlocal current_page
                current_page = page
                return fetch(page)
            rows, pages, reason = collect_pages(fetch_page, product="dry", target=target,
                retries=args.page_retries, sleep_s=args.sleep, eligibility=eligibility, on_page=checkpoint)
            context("listing", sort=sort, page=current_page, **progress_state())
            emit("listing_complete", sort=sort, accepted=len(rows), target=target, stop_reason=reason,
                 pages=len(pages), excluded=counters, failed_products=len(failed_items),
                 end_basis=manifest[sort].get("end_basis"))
            if reason not in {"target_reached", "last_page"}:
                raise CollectionError("LISTING_FAILED:" + sort + ":" + reason)
        rows = merge_rows(listing_results["main"], listing_results["bsr"], collector.cache, meta)
        path = args.output_dir / "mmkt_dry_output.csv"
        context("csv_save", batch_id=meta["batch_id"])
        manifest["csv"]["state"] = "saving"
        try:
            write_output(path, rows)
        except Exception as exc:
            manifest["csv"].update(state="failed", error_type=type(exc).__name__)
            failure(exc, code="FINAL_CSV_SAVE_FAILED")
            raise CollectionError("FINAL_CSV_SAVE_FAILED") from None
        manifest["csv"].update(state="saved", rows=len(rows))
        refresh()
        manifest.update(success=True, state="complete", output_rows=len(rows),
                        collection_status="partial" if product_failures else "complete",
                        output_digest=file_digest(path),
                        missing_counts={key: sum(row[key] is None for row in rows) for key in config.COLUMNS if key != "id"})
        manifest["timing"]["collection_completed_at"] = datetime.now().isoformat(timespec="seconds")
        emit("csv_saved", path=str(path), rows=len(rows), api_calls=collector.api_calls,
             missing_counts=manifest["missing_counts"], collection_status=manifest["collection_status"],
             product_failure_count=len(product_failures))
        return manifest
    except BaseException as exc:
        refresh()
        error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
        manifest.update(success=False, state="failed", collection_status="failed", error=error)
        manifest["timing"]["collection_stopped_at"] = datetime.now().isoformat(timespec="seconds")
        if manifest[sort]["state"] == "collecting":
            manifest[sort].update(state="failed", stop_reason=error)
        exc.collection_manifest = manifest
        raise


def load_output(args, table):
    context("output_validation", table=".".join(table))
    path = args.output_dir / "mmkt_dry_output.csv"
    rows, manifest = load_validated_output(path, with_manifest=True)
    manifest_path = args.output_dir / "mmkt_dry_manifest.json"
    written = False
    try:
        # Every subsequent failure retains this validated artifact's identity.
        # Older validated files can have blank stars; normalize a memory copy.
        rows = [default_missing_ratings(row, source="validated_csv") for row in rows]
        emit("output_validated", batch_id=manifest.get("batch_id"), rows=len(rows), table=".".join(table))
        manifest["database"] = {"table": ".".join(table), "state": "running", "db_written": None}
        if not rows and manifest.get("product_failure_count", 0):
            raise DatabaseError("NO_VALID_PRODUCTS", db_written=False)
        try:
            write_json(manifest_path, manifest)
        except Exception:
            raise DatabaseError("DB_STATE_SAVE_FAILED", db_written=False) from None
        result = inspect_or_insert(table, rows)
        written = True
        manifest["database"].update(state="success", db_written=True, **result)
        try:
            write_json(manifest_path, manifest)
        except Exception:
            raise DatabaseError("DB_COMMITTED_MANIFEST_SAVE_FAILED", db_written=True) from None
    except BaseException as exc:
        exc.validated_output = True
        exc.db_written = getattr(exc, "db_written", written)
        exc.validated_manifest = manifest
        if not written:
            manifest.setdefault("database", {"table": ".".join(table)}).update(
                state="failed", db_written=exc.db_written,
                error=str(exc) if isinstance(exc, CollectionError) else type(exc).__name__)
        with contextlib.suppress(Exception):
            write_json(manifest_path, manifest)
        raise
    return manifest, result


def main(argv=None) -> int:
    args = parse_args(argv)
    table = config.TEST_TABLE if args.table == "test" else config.PRODUCTION_TABLE
    session = None
    meta = run_metadata()
    collection_success = False
    artifact_ready = False
    db_written = False
    manifest = initial_manifest(args, meta)
    started = time.monotonic()
    notification = None

    def report(error=None):
        if args.collect_only or args.check_table:
            emit("mail_skipped", reason="collect_only" if args.collect_only else "check_table")
            return {}
        try:
            options = {}
            if args.write_db and not artifact_ready:
                # A transient validation/read failure must not replace a source
                # manifest that we have not yet been able to validate.
                options["status_path"] = args.output_dir / "mmkt_dry_load_failure.json"
            return notify_run(args.output_dir, manifest, table, collection_success=collection_success,
                              db_written=db_written, error=error,
                              elapsed_s=round(time.monotonic() - started, 1), **options)
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
                write_json(args.output_dir / "mmkt_dry_manifest.json", manifest)
                context("browser_open", batch_id=meta["batch_id"])
                emit("browser_start", note="UC Chrome 실행 및 MediaMarkt 홈 접속 준비 중")
                from common.uc import UcSession
                with quiet_external():
                    session = UcSession()
                    session.open()
                emit("browser_ready")
                wait_for_captcha(session, session.driver.page_source or "", args.captcha_wait)
                manifest = crawl(args, session, meta, manifest=manifest, started=started)
                try:
                    write_json(args.output_dir / "mmkt_dry_manifest.json", manifest)
                except Exception as exc:
                    failure(exc, code="FINAL_MANIFEST_SAVE_FAILED")
                    raise CollectionError("FINAL_MANIFEST_SAVE_FAILED") from None
                artifact_ready = True
                collection_success = not manifest.get("product_failure_count", 0)
                emit("collection_complete", **manifest["timing"], output_rows=manifest["output_rows"],
                     collection_status=manifest.get("collection_status", "complete"),
                     product_failure_count=manifest.get("product_failure_count", 0))
                with quiet_external():
                    session.close()
                session = None
            if not args.collect_only:
                manifest, result = load_output(args, table)
                artifact_ready = True
                collection_success = not manifest.get("product_failure_count", 0)
                db_written = True
            else:
                result = {}
            error = "PARTIAL_PRODUCT_FAILURES" if not collection_success else None
            notification = report(error)
            context("complete", batch_id=manifest.get("batch_id"))
            emit("run_complete", success=collection_success and not notification.get("failed", False),
                 collection_success=collection_success, collection_completed=artifact_ready,
                 collection_status=manifest.get("collection_status", "complete"),
                 product_failure_count=manifest.get("product_failure_count", 0), error=error,
                 db_success=True if db_written else None, db_written=db_written,
                 main=manifest.get("main"), bsr=manifest.get("bsr"), output_rows=manifest["output_rows"],
                 inserted=result.get("inserted", 0), already_loaded=result.get("already_loaded", False),
                 csv=manifest.get("csv"), intermediate=manifest.get("intermediate"),
                 collection_elapsed_s=(manifest.get("timing") or {}).get("collection_elapsed_s"),
                 total_elapsed_s=round(time.monotonic() - started, 1), notification=notification)
            return 1 if error or notification.get("failed") else 0
        except BaseException as exc:
            error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            failure(exc, code=error)
            manifest_path = args.output_dir / "mmkt_dry_manifest.json"
            artifact_ready = artifact_ready or getattr(exc, "validated_output", False)
            db_written = getattr(exc, "db_written", db_written)
            if getattr(exc, "validated_manifest", None) is not None:
                manifest = exc.validated_manifest
            elif getattr(exc, "collection_manifest", None) is not None:
                manifest = exc.collection_manifest
            collection_success = artifact_ready and not manifest.get("product_failure_count", 0)
            if not args.check_table and not args.write_db and not artifact_ready:
                manifest.update(success=False, state="failed", collection_status="failed", error=error)
                timing = manifest.setdefault("timing", {})
                timing.update(collection_elapsed_s=round(time.monotonic() - started, 1),
                              collection_stopped_at=datetime.now().isoformat(timespec="seconds"))
                try:
                    write_json(manifest_path, manifest)
                except Exception as save_error:
                    failure(save_error, code="FAILURE_MANIFEST_SAVE_FAILED")
            notification = report(error)
            context("complete", batch_id=manifest.get("batch_id"))
            emit("run_complete", success=False, collection_success=collection_success,
                 collection_completed=artifact_ready, collection_status=manifest.get("collection_status", "failed"),
                 product_failure_count=manifest.get("product_failure_count", 0),
                 db_success=db_written if artifact_ready and not args.collect_only else None,
                 error=error, db_written=db_written, csv_preserved=artifact_ready,
                 main=manifest.get("main"), bsr=manifest.get("bsr"), output_rows=manifest.get("output_rows", 0),
                 csv=manifest.get("csv"), intermediate=manifest.get("intermediate"),
                 collection_elapsed_s=(manifest.get("timing") or {}).get("collection_elapsed_s"),
                 total_elapsed_s=round(time.monotonic() - started, 1), notification=notification)
            return 130 if isinstance(exc, KeyboardInterrupt) else 1
        finally:
            if session is not None:
                with contextlib.suppress(Exception), quiet_external():
                    session.close()


if __name__ == "__main__":
    raise SystemExit(main())
