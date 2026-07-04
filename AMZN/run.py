"""SIEL-style entry point for Amazon.de SEG crawler."""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import pipeline  # noqa: E402
from common.config import BSR_TARGET, DEFAULT_SLEEP, LISTING_TARGET  # noqa: E402

PRODUCT_CONFIGS = {
    "tv": "TV.config",
    "ref": "REF.config",
}


def _load_config(product: str):
    module_name = PRODUCT_CONFIGS.get(product.lower())
    if not module_name:
        raise SystemExit(f"unsupported product: {product}")
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise SystemExit(f"config module not found for product={product}: {module_name}") from exc


def _steps_from_stages(stages: list[str], *, no_auto_insert: bool, email_report: bool) -> str:
    steps: list[str] = []
    if "main" in stages:
        steps.append("listing")
    if "bsr" in stages:
        steps.append("bsr")
    if "detail" in stages:
        steps.extend(["targets", "detail", "full"])
        if not no_auto_insert:
            steps.append("db")
    if email_report:
        steps.append("notify")
    return ",".join(dict.fromkeys(steps))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Amazon.de SEG crawler (SIEL-compatible frame)")
    ap.add_argument("--product", nargs="+", required=True, choices=sorted(PRODUCT_CONFIGS))
    ap.add_argument("--stages", nargs="+", default=["main", "bsr", "detail"], choices=["main", "bsr", "detail"])
    ap.add_argument("--max-rank", type=int, default=LISTING_TARGET)
    ap.add_argument("--bsr-max-rank", type=int, default=BSR_TARGET)
    ap.add_argument("--bsr-retries", type=int, default=2)
    ap.add_argument("--bsr-min-rank", type=int, default=97)
    ap.add_argument("--bsr-page-load-strategies", default="eager,none,eager")
    ap.add_argument("--max-pages", type=int, default=30)
    ap.add_argument("--max-detail", type=int, default=None)
    ap.add_argument("--detail-sleep", type=float, default=DEFAULT_SLEEP)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--db-dry-run", action="store_true")
    ap.add_argument("--no-auto-insert", action="store_true")
    ap.add_argument("--streaming-insert", action="store_true")
    ap.add_argument("--email-report", action="store_true")
    ap.add_argument("--only", default="", help="legacy direct pipeline steps; overrides --stages when set")
    return ap.parse_args(argv)


def _pipeline_args(args: argparse.Namespace) -> argparse.Namespace:
    only = args.only or _steps_from_stages(
        args.stages,
        no_auto_insert=args.no_auto_insert,
        email_report=args.email_report,
    )
    limit = args.max_detail if args.max_detail is not None else 0
    return argparse.Namespace(
        only=only,
        limit=limit,
        max_detail=args.max_detail,
        start=args.start,
        max_pages=args.max_pages,
        max_rank=args.max_rank,
        bsr_max_rank=args.bsr_max_rank,
        bsr_retries=args.bsr_retries,
        bsr_min_rank=args.bsr_min_rank,
        bsr_page_load_strategies=args.bsr_page_load_strategies,
        db_dry_run=args.db_dry_run,
        detail_sleep=args.detail_sleep,
        headless=args.headless,
        streaming_insert=args.streaming_insert,
        no_auto_insert=args.no_auto_insert,
        email_report=args.email_report,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = 0
    for product in args.product:
        print(f"\n=== [run] starting product={product} ===\n", file=sys.stderr)
        cfg = _load_config(product)
        rc = pipeline.run(cfg, _pipeline_args(args))
        if rc:
            status = rc
            print(f"[run] product={product} failed; continuing next product", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
