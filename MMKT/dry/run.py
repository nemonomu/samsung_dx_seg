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
from dry.checkpoint import IntermediateJournal, save_checkpoint
from dry.database import DatabaseError, file_digest, inspect_or_insert, load_validated_output
from dry.detail import CollectionError, CommonAPIError, DetailCollector
from dry.output import canonical_product_url, default_missing_ratings, merge_rows, run_metadata, write_output
from dry.output import write_manifest as write_json
from dry.notify import notify_run
from dry.policy import exclusion_reason, washer_dryer_evidence
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


def make_fetcher(session, base_url: str, args, sort="main", progress_state=None, *, phase="listing"):
    attempts = {}
    def fetch(page):
        start = time.perf_counter()
        attempts[page] = attempts.get(page, 0) + 1
        context(phase, phase=phase, sort=sort, page=page, attempt=attempts[page],
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
            "main": {"state": "not_started", "rows": None, "stop_reason": None,
                     "listing_state": "not_started", "detail_state": "not_started", "candidate_count": 0},
            "bsr": {"state": "not_started", "rows": None, "stop_reason": None,
                    "listing_state": "not_started", "detail_state": "not_started", "candidate_count": 0},
            "pages": {"main": [], "bsr": []}, "product_failure_count": 0, "product_failures": [],
            "csv": {"state": "not_created", "path": str(args.output_dir / "mmkt_dry_output.csv")},
            "database": {"state": "not_started", "table": ".".join(table), "db_written": False}}


def cached_candidate_state(cache, row):
    """No API work: presence alone is not proof of an eligible detail result."""
    detail = cache.get(str(row.get("sku_id") or ""))
    if not isinstance(detail, dict) or not isinstance(detail.get("_features"), dict):
        return "unconfirmed"
    reason = exclusion_reason(row, features=detail["_features"], detail_name=detail.get("_detail_name"))
    if reason:
        return "unconfirmed" if reason == "unconfirmed_product_type" else "excluded"
    if not {"count_of_reviews", "star_rating", "count_of_star_ratings"}.issubset(detail):
        return "unconfirmed"
    return "eligible"


def crawl(args, session, meta=None, *, manifest=None, started=None) -> dict:
    meta = meta or run_metadata()
    manifest = manifest if manifest is not None else initial_manifest(args, meta)
    started = time.monotonic() if started is None else started
    collector = DetailCollector(session, retries=args.page_retries, sleep_s=args.sleep)
    product_failures = []
    listing_results = {"main": [], "bsr": []}  # Only final eligible rows.
    candidates, exclusions, detail_results = [], [], []
    candidate_ids, accepted, failed_items = set(), set(), set()
    early_counts, detail_counts = {}, {}
    confirmed_ids, cached_excluded_ids = set(), set()
    candidate_cursor = exclusion_cursor = 0
    journal = None
    sort, target, current_page = "main", args.target, 1
    phase_started = time.monotonic()

    def refresh():
        manifest.update(output_rows=len({r["sku_id"] for values in listing_results.values() for r in values}),
                        api_calls=collector.api_calls, unique_detail_products=len(collector.cache),
                        product_failure_count=len(product_failures), product_failures=list(product_failures))
        manifest.setdefault("timing", {})["collection_elapsed_s"] = round(time.monotonic() - started, 1)

    def progress_state():
        return {"candidates": len(candidate_ids), "accepted": len(accepted), "target": target,
                "cached_eligible_candidates": len(confirmed_ids),
                "cached_excluded_candidates": len(cached_excluded_ids),
                "unconfirmed_candidates": len(candidate_ids) - len(confirmed_ids) - len(cached_excluded_ids),
                "excluded_count": sum(v for k, v in early_counts.items() if k != "duplicate") + sum(detail_counts.values()),
                "duplicates": early_counts.get("duplicate", 0), "failed_products": len(failed_items)}

    def snapshot(**options):
        refresh()
        # Candidate rows have no detail contract. Never feed them to merge_rows.
        rows = merge_rows(listing_results["main"], listing_results["bsr"], collector.cache, meta,
                          log_defaults=False)
        return save_checkpoint(args.output_dir, manifest, rows, sort=sort, **options)

    def early_eligibility(row):
        item = str(row.get("sku_id") or "").strip()
        row["listing_sort"] = sort
        context("listing_product", phase="listing", sort=sort, page=current_page, item=item, **progress_state())
        reason = exclusion_reason(row, require_evidence=False)
        if not reason and item in candidate_ids:
            reason = "duplicate"
        if reason:
            early_counts[reason] = early_counts.get(reason, 0) + 1
            record = {"item": item, "title": row.get("retailer_sku_name"),
                      "page": row.get("page"), "listing_order": row.get("listing_order"), "reason": reason}
            exclusions.append(record)
            emit("product_duplicate" if reason == "duplicate" else "product_excluded",
                 phase="listing", reason=reason, evidence=washer_dryer_evidence(row) if reason == "washer_dryer" else None,
                 **{key: value for key, value in record.items() if key != "reason"}, **progress_state())
            return reason
        if not item:
            raise CollectionError("MISSING_PRODUCT_ID")
        row["sku_id"] = item
        row["product_url"] = canonical_product_url(row.get("product_url"), item)
        candidate_ids.add(item)
        if sort == "bsr":
            state = cached_candidate_state(collector.cache, row)
            if state == "eligible":
                confirmed_ids.add(item)
            elif state == "excluded":
                cached_excluded_ids.add(item)
        return None

    def candidates_sufficient(incoming):
        # Pure preview for the incoming block; repeat/invalid pages never use it.
        # Unknown candidates stay in order and are NOT counted toward the proof.
        eligible = set(confirmed_ids)
        for row in incoming:
            item = str(row.get("sku_id") or "").strip()
            if (item and item not in candidate_ids and not exclusion_reason(row, require_evidence=False)
                    and cached_candidate_state(collector.cache, row) == "eligible"):
                eligible.add(item)
        return len(eligible) >= target

    def checkpoint(rows, pages, stop):
        nonlocal candidates, candidate_cursor, exclusion_cursor
        candidates = list(rows)
        entry = pages[-1]
        listing = manifest[sort]
        listing.update(candidate_count=len(candidates), early_excluded=dict(early_counts),
                       excluded=dict(early_counts), duplicates=early_counts.get("duplicate", 0),
                       last_requested_page=entry["page"], end_basis=entry.get("end_basis"),
                       category_slots_read=entry.get("category_slots_read", 0),
                       listing_stop_reason=stop, listing_state="complete" if stop in {"last_page", "confirmed_candidates"}
                       else "failed" if stop else "collecting",
                       cached_eligible_candidates=len(confirmed_ids), cached_excluded_candidates=len(cached_excluded_ids),
                       unconfirmed_candidates=len(candidate_ids) - len(confirmed_ids) - len(cached_excluded_ids))
        entry.update({key: listing[key] for key in (
            "cached_eligible_candidates", "cached_excluded_candidates", "unconfirmed_candidates")})
        options = {"page": entry["page"], "attempt": entry["attempt"], "phase": "candidates"}
        if entry.get("phase") == "tail_confirmation":
            confirmation = entry["confirmation"]
            listing.setdefault("tail_confirmation", {}).update(confirmation,
                state={"complete": "confirmed", "continue": "more_products", "failed": "failed"}[confirmation["action"]])
            options.update(phase="tail_result", sequence=confirmation["confirmation_cycle"])
        if not entry["error"]:
            listing["last_successful_page"] = entry.get("end_for_page", entry["page"])
        manifest["pages"][sort] = list(pages)
        emit("page_summary", sort=sort, **entry)
        context("intermediate_save", phase="listing", sort=sort, page=entry["page"], batch_id=meta["batch_id"])
        refresh()
        journal.append("listing", page=entry["page"], attempt=entry["attempt"], summary=entry,
                       candidates=candidates[candidate_cursor:], exclusions=exclusions[exclusion_cursor:],
                       counts=progress_state(), stop_reason=stop)
        candidate_cursor, exclusion_cursor = len(candidates), len(exclusions)
        if stop in {"last_page", "confirmed_candidates"}:
            try:
                journal.verify()
                path = snapshot(candidates=candidates, exclusions=exclusions, **options)
            except Exception:
                listing["listing_state"] = "failed"
                raise
            listing["candidate_path"] = str(path)

    def probe_checkpoint(record):
        status = manifest[sort].setdefault("tail_confirmation", {"reads": []})
        if status.get("confirmation_cycle") != record["confirmation_cycle"]:
            status.update(confirmation_cycle=record["confirmation_cycle"], reason=None, latest_total=None)
        status.update(state="checking", queries=record["queries"], query_limit=record["query_limit"],
                      remaining=record["remaining"], last_probe_page=record["page"])
        status.setdefault("reads", []).append(dict(record))
        context("intermediate_save", phase="tail_confirmation", sort=sort, page=record["page"])
        try:
            refresh()
            journal.append("tail_read", page=record["page"], evidence=record, counts=progress_state())
        except Exception:
            status.update(state="failed", reason="probe_checkpoint_failed")
            raise

    def process_details():
        nonlocal phase_started
        listing = manifest[sort]
        phase_started = time.monotonic()
        listing.update(detail_state="running", detail_processed=0)
        api_before, cache_hits = collector.api_calls, 0
        listing.setdefault("timing", {})["detail_started_at"] = datetime.now().isoformat(timespec="seconds")
        context("detail_phase", phase="detail", sort=sort, **progress_state())
        emit("detail_phase_start", sort=sort, candidate_count=len(candidates), target=target)
        for index, row in enumerate(candidates, 1):
            if len(accepted) >= target:
                break
            item, page = row["sku_id"], row["page"]
            context("detail", phase="detail", sort=sort, page=page, item=item,
                    detail_processed=index - 1, **progress_state())
            cached = item in collector.cache
            emit("detail_start", cached=cached, detail_products=len(collector.cache))
            record = {"item": item, "page": page, "listing_order": row["listing_order"]}
            fatal = None
            try:
                detail = collector.get(item, row)
                cache_hits += int(cached)
                reason = exclusion_reason(row, features=detail["_features"], detail_name=detail["_detail_name"])
                if reason == "unconfirmed_product_type":
                    raise CollectionError("UNCONFIRMED_PRODUCT_TYPE")
            except Exception as exc:
                error = str(exc) if isinstance(exc, CollectionError) else "PRODUCT_PROCESSING_FAILED"
                failure(exc, code=error)
                failed_items.add(item)
                product_failures.append({"sort": sort, "page": page, "item": item,
                                         "error": error, "error_type": type(exc).__name__})
                record.update(state="failed", error=error)
                fatal = exc if isinstance(exc, CommonAPIError) else None
                emit("product_failed", sort=sort, page=page, item=item, error=error,
                     action="stop_common_failure" if fatal else "skip_and_continue", **progress_state())
            else:
                record.update(state="excluded" if reason else "accepted", reason=reason,
                              values={key: detail.get(key) for key in
                                      ("sku", "capacity", "loading_type", "count_of_reviews",
                                       "count_of_star_ratings", "star_rating")})
                if detail.get("_purchase_status") is not None:
                    record["values"]["_purchase_status"] = detail["_purchase_status"]
                if reason:
                    detail_counts[reason] = detail_counts.get(reason, 0) + 1
                    emit("product_excluded", phase="detail", item=item, reason=reason,
                         evidence=washer_dryer_evidence(row, features=detail["_features"],
                             detail_name=detail["_detail_name"]) if reason == "washer_dryer" else None)
                else:
                    accepted.add(item)
                    listing_results[sort].append(row)
                    emit("product_accepted", item=item, accepted=len(accepted), target=target,
                         detail_products=len(collector.cache), api_calls=collector.api_calls)
            detail_results.append(record)
            combined = dict(early_counts)
            for reason, count in detail_counts.items():
                combined[reason] = combined.get(reason, 0) + count
            listing.update(rows=len(accepted), detail_processed=index, failed_products=len(failed_items),
                           detail_excluded=dict(detail_counts), excluded=combined,
                           cache_hits=cache_hits, new_api_calls=collector.api_calls - api_before)
            listing["timing"]["detail_elapsed_s"] = round(time.monotonic() - phase_started, 1)
            if fatal:
                listing.update(detail_state="failed", stop_reason=str(fatal))
            context("intermediate_save", phase="detail", sort=sort, page=page, item=item)
            refresh()
            journal.append("detail", page=page, result=record, cached=cached, counts=progress_state(),
                           api_calls=collector.api_calls)
            if fatal:
                raise fatal
        if len(accepted) < target and listing.get("listing_stop_reason") != "last_page":
            raise CollectionError("CONFIRMED_CANDIDATES_INSUFFICIENT")
        reason = "target_reached" if len(accepted) >= target else "last_page"
        listing.update(state="complete", detail_state="complete", rows=len(accepted), stop_reason=reason)
        listing["timing"].update(detail_completed_at=datetime.now().isoformat(timespec="seconds"),
                                detail_elapsed_s=round(time.monotonic() - phase_started, 1))
        try:
            journal.verify()
            snapshot(page=listing.get("last_successful_page", 1), attempt=1, phase="detail",
                     sequence=len(detail_results), detail_results=detail_results)
        except Exception:
            listing.update(state="failed", detail_state="failed", stop_reason="INTERMEDIATE_SAVE_FAILED")
            raise
        context("detail_complete", phase="detail", sort=sort, **progress_state())
        emit("detail_phase_complete", sort=sort, accepted=len(accepted), target=target,
             candidate_count=len(candidates), processed=len(detail_results), stop_reason=reason,
             failed_products=len(failed_items), cache_hits=cache_hits,
             new_api_calls=collector.api_calls - api_before, **listing["timing"])

    try:
        for sort, url, target in (("main", config.LISTING_URL, args.target),
                                  ("bsr", config.BSR_URL, args.bsr_target)):
            candidates, exclusions, detail_results = [], [], []
            candidate_ids, accepted, failed_items = set(), set(), set()
            early_counts, detail_counts = {}, {}
            confirmed_ids, cached_excluded_ids = set(), set()
            candidate_cursor = exclusion_cursor = 0
            journal = IntermediateJournal(args.output_dir, manifest, sort)
            listing = manifest[sort]
            phase_started = time.monotonic()
            listing.update(state="collecting", rows=0, candidate_count=0,
                           listing_state="running", detail_state="not_started")
            listing["timing"] = {"listing_started_at": datetime.now().isoformat(timespec="seconds")}
            context("listing", phase="listing", sort=sort, target=target)
            emit("listing_start", accepted=0, candidates=0, target=target)
            fetch = make_fetcher(session, url, args, sort, progress_state=progress_state)
            probe = make_fetcher(session, url, args, sort, progress_state=progress_state, phase="tail_confirmation")
            def fetch_page(page):
                nonlocal current_page
                current_page = page
                return fetch(page)
            rows, pages, reason = collect_pages(fetch_page, product="dry", target=None,
                retries=args.page_retries, sleep_s=args.sleep, eligibility=early_eligibility, on_page=checkpoint,
                probe_page=probe, on_probe=probe_checkpoint,
                candidates_sufficient=candidates_sufficient if sort == "bsr" else None)
            listing["timing"]["listing_elapsed_s"] = round(time.monotonic() - phase_started, 1)
            if reason not in {"last_page", "confirmed_candidates"}:
                raise CollectionError("LISTING_FAILED:" + sort + ":" + reason)
            listing["timing"]["listing_completed_at"] = datetime.now().isoformat(timespec="seconds")
            context("listing_complete", phase="listing", sort=sort, page=current_page, **progress_state())
            emit("listing_complete", sort=sort, candidate_count=len(rows), accepted=0,
                 target=target, stop_reason=reason, end_basis=listing.get("end_basis"), **listing["timing"])
            # The checkpoint callback has saved, read back and validated all
            # candidates before this transition. Do not navigate during details.
            process_details()
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
        listing = manifest[sort]
        if listing.get("listing_state") != "complete":
            listing["state"] = "failed"
            listing["listing_state"] = "failed"
            listing["stop_reason"] = listing.get("listing_stop_reason") or error
            listing.setdefault("timing", {})["listing_elapsed_s"] = round(time.monotonic() - phase_started, 1)
        elif listing.get("detail_state") != "complete":
            listing["state"] = "failed"
            listing["detail_state"] = "failed"
            listing["stop_reason"] = error
            listing.setdefault("timing", {})["detail_elapsed_s"] = round(time.monotonic() - phase_started, 1)
        listing.setdefault("timing", {})["phase_stopped_at"] = datetime.now().isoformat(timespec="seconds")
        manifest.update(success=False, state="failed", collection_status="failed", error=error)
        manifest["timing"]["collection_stopped_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            if journal is not None and not journal.failed:
                journal.append("failure", page=current_page, error=error, counts=progress_state())
            snapshot(page=current_page, attempt=1, phase="failure", candidates=candidates,
                     exclusions=exclusions, detail_results=detail_results)
            if error == "INTERMEDIATE_SAVE_FAILED":
                manifest["intermediate"].update(state="failed", failure_snapshot_saved=True)
        except Exception as save_exc:
            manifest["intermediate_failure"] = {"error": "INTERMEDIATE_SAVE_FAILED",
                                                 "error_type": type(save_exc).__name__}
            failure(save_exc, code="INTERMEDIATE_SAVE_FAILED")
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
        if not rows:
            result = {"inserted": 0, "already_loaded": False,
                      "backfilled": {"sku": 0, "loading_type": 0, "capacity": 0}}
            manifest["database"].update(state="skipped_empty", db_written=False, **result)
            write_json(manifest_path, manifest)
            emit("db_skipped", reason="confirmed_empty_result", rows=0)
            return manifest, result
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
                db_written = manifest["database"].get("db_written") is True
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
