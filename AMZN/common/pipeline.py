"""Run one Amazon category pipeline with SIEL-style JSONL merge."""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from typing import Any

from common import detail, full_output, listing, merge_insert, notify, targets
from common.config import BSR_TARGET, DEFAULT_SLEEP, LISTING_TARGET, run_meta
from common.io_util import category_output_root, write_csv, write_json
from common.jsonl import append_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an Amazon.de SEG category pipeline.")
    parser.add_argument("--only", default="all", help="Comma list: listing/main,bsr,targets,detail,full,db,notify")
    parser.add_argument("--limit", type=int, default=0, help="detail: 0 = all targets")
    parser.add_argument("--max-detail", type=int, default=None, help="SIEL-compatible alias for detail limit")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--max-rank", type=int, default=LISTING_TARGET)
    parser.add_argument("--bsr-max-rank", type=int, default=BSR_TARGET)
    parser.add_argument("--bsr-retries", type=int, default=int(os.getenv("AMZN_BSR_RETRIES", "2")))
    parser.add_argument("--bsr-min-rank", type=int, default=int(os.getenv("AMZN_BSR_MIN_RANK", "97")))
    parser.add_argument("--bsr-page-load-strategies", default=os.getenv("AMZN_BSR_PAGE_LOAD_STRATEGIES", "eager,none,eager"))
    parser.add_argument("--db-dry-run", action="store_true")
    parser.add_argument("--detail-sleep", type=float, default=DEFAULT_SLEEP)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--streaming-insert", action="store_true", help="insert retail_com rows as detail records are collected")
    parser.add_argument("--no-auto-insert", action="store_true", help="collect JSONL/CSV only; skip DB insert")
    parser.add_argument("--email-report", action="store_true", help="send report when email settings allow it")
    return parser.parse_args()


def _steps(value: str) -> set[str]:
    if value == "all":
        return {"listing", "bsr", "targets", "detail", "full", "db", "notify"}
    aliases = {"main": "listing"}
    return {aliases.get(s.strip(), s.strip()) for s in value.split(",") if s.strip()}


def _bsr_expected_count(stage_max: int) -> int:
    return stage_max if stage_max and stage_max > 0 else BSR_TARGET


def _bsr_required_count(args: argparse.Namespace, stage_max: int) -> int:
    expected = _bsr_expected_count(stage_max)
    return min(expected, max(1, args.bsr_min_rank))


def _bsr_page_load_strategies(args: argparse.Namespace) -> list[str]:
    valid = {"normal", "eager", "none"}
    strategies = [s.strip().lower() for s in (args.bsr_page_load_strategies or "").split(",") if s.strip()]
    strategies = [s for s in strategies if s in valid]
    return strategies or ["eager", "none", "eager"]


def _emit_error(emit, *, stage: str, product: str, message: str, **extra: Any) -> None:
    rec = {
        "stage": stage,
        "product": product,
        "_error": extra.pop("_error", stage + " failed"),
        "message": message,
        "crawl_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    rec.update(extra)
    emit(rec)


def _run_bsr_with_retries(cfg, args: argparse.Namespace, *, batch_id: str, emit) -> list[dict[str, Any]]:
    stage_max = args.bsr_max_rank
    strategies = _bsr_page_load_strategies(args)
    attempts = max(1, args.bsr_retries + 1, len(strategies))
    required = _bsr_required_count(args, stage_max)
    expected = _bsr_expected_count(stage_max)
    best_rows: list[dict[str, Any]] = []
    attempt_summaries = []
    for attempt in range(1, attempts + 1):
        strategy = strategies[(attempt - 1) % len(strategies)]
        print(
            f"[bsr/{cfg.PRODUCT}] isolated attempt={attempt}/{attempts} required={required} expected={expected} pageLoadStrategy={strategy}",
            flush=True,
        )
        manifest = listing.run(
            cfg,
            sort="bsr",
            target=stage_max,
            max_pages=args.max_pages,
            batch_id=batch_id,
            emit=None,
            headless=args.headless,
            page_load_strategy=strategy,
        )
        rows = manifest.get("rows_data", [])
        attempt_summaries.append({"attempt": attempt, "strategy": strategy, "rows": len(rows)})
        if len(rows) > len(best_rows):
            best_rows = rows
        if len(rows) >= required:
            break
        time.sleep(min(10, 2 * attempt))

    out = category_output_root(cfg.PRODUCT)
    write_csv(out / "amzn_listing_bsr.csv", best_rows)
    retry_manifest = {
        "run_type": "bsr_retry",
        "product": cfg.PRODUCT,
        "required": required,
        "expected": expected,
        "best_rows": len(best_rows),
        "attempts": attempt_summaries,
        "success": len(best_rows) >= required,
        "output": str(out / "amzn_listing_bsr.csv"),
    }
    write_json(out / "step01_listing_bsr_retry_manifest.json", retry_manifest)
    for row in best_rows:
        emit(row)
    if len(best_rows) < required:
        message = f"BSR target not met: captured={len(best_rows)} required={required} expected={expected}"
        _emit_error(emit, stage="bsr", product=cfg.PRODUCT, message=message, _error="bsr target not met", captured=len(best_rows), required=required, expected=expected)
        raise RuntimeError(message)
    return best_rows


def run(cfg, args: argparse.Namespace | None = None) -> int:
    args = args or parse_args()
    steps = _steps(args.only)
    out = category_output_root(cfg.PRODUCT)
    out.mkdir(parents=True, exist_ok=True)
    if steps == {"notify"} and (out / "step00_run_manifest.json").exists():
        notify.run(cfg)
        return 0
    meta = run_meta("a")
    batch_id = meta["batch_id"]
    jsonl_path = out / f"amzn_{cfg.PRODUCT.lower()}_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    run_manifest = {
        "run_type": "run",
        "product": cfg.PRODUCT,
        "batch_id": batch_id,
        "jsonl_path": str(jsonl_path),
        "steps": sorted(steps),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_json(out / "step00_run_manifest.json", run_manifest)

    streamer = None
    if args.streaming_insert and not args.no_auto_insert and "db" in steps:
        streamer = merge_insert.StreamingRetailInserter(cfg, batch_id=batch_id, dry_run=args.db_dry_run)

    def emit(record: dict[str, Any]) -> None:
        if record.get("batch_id") in (None, "") and record.get("stage") in {"main", "bsr", "detail"}:
            record["batch_id"] = batch_id
        append_jsonl(jsonl_path, record)
        if streamer is not None:
            streamer.handle(record)

    status = 0
    fatal_error: Exception | None = None
    try:
        try:
            if "listing" in steps:
                listing.run(cfg, sort="main", target=args.max_rank, max_pages=args.max_pages, batch_id=batch_id, emit=emit, headless=args.headless)
            if "bsr" in steps:
                _run_bsr_with_retries(cfg, args, batch_id=batch_id, emit=emit)
            if "targets" in steps:
                targets.run(cfg)
            if "detail" in steps:
                detail_limit = args.limit
                if getattr(args, "max_detail", None) is not None:
                    detail_limit = args.max_detail or 0
                detail.run(cfg, limit=detail_limit, start=args.start, batch_id=batch_id, emit=emit, headless=args.headless, sleep=args.detail_sleep)
            if "full" in steps:
                full_output.run(cfg)
            if "db" in steps and not args.no_auto_insert:
                if streamer is not None:
                    summary = streamer.summary()
                    emit(summary)
                    write_json(out / "step14_streaming_db_save_manifest.json", summary)
                else:
                    manifest = merge_insert.insert_jsonl(cfg, jsonl_path, dry_run=args.db_dry_run)
                    manifest["stage"] = "db_insert_summary"
                    emit(manifest)
        except Exception as exc:  # noqa: BLE001
            status = 1
            fatal_error = exc
            _emit_error(emit, stage="run_error", product=cfg.PRODUCT, message=repr(exc), _error="product run failed")
        if "notify" in steps or args.email_report:
            notify.run(cfg)
    finally:
        if streamer is not None:
            streamer.close()
    if fatal_error:
        print(f"[run/{cfg.PRODUCT}] FAILED: {fatal_error!r}", flush=True)
    return status
