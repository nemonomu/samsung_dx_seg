"""Run the full pipeline for one category: schema -> listing -> targets -> full_output -> db_save -> notify."""
from __future__ import annotations

import argparse
import os

from common import db_save, full_output, listing, notify, schema, targets


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run an OTTO category pipeline (TV/REF/LDY).")
    p.add_argument("--only", default="all", help="Comma list of steps to run: schema,listing,targets,full,db,notify (default all).")
    p.add_argument("--limit", type=int, default=0, help="full_output: 0 = all targets.")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--pdp-supplement", choices=["none", "zenrows"], default="none",
                   help="full_output: supplement PDP-only fields (e.g. LDY Bauart) via ZenRows browser.")
    p.add_argument("--detail-sleep", type=float, default=1.0)
    p.add_argument("--save-html", action="store_true",
                   help="persist each fetched HTML page to <category>/data/output/raw_html/ for audit (also via OTTO_SAVE_HTML=1).")
    p.add_argument("--db-dry-run", action="store_true", help="db_save: never write to DB.")
    return p.parse_args()


def run(cfg, args: argparse.Namespace | None = None) -> int:
    if args is None:
        args = parse_args()
    if getattr(args, "save_html", False):
        os.environ["OTTO_SAVE_HTML"] = "1"
    steps = {"schema", "listing", "targets", "full", "db", "notify"} if args.only == "all" else {s.strip() for s in args.only.split(",")}
    if "schema" in steps:
        schema.run(cfg)
    if "listing" in steps:
        listing.run(cfg)
    if "targets" in steps:
        targets.run(cfg)
    if "full" in steps:
        full_output.run(cfg, limit=args.limit, start=args.start, pdp_supplement=args.pdp_supplement, detail_sleep=args.detail_sleep)
    if "db" in steps:
        # A DB failure (e.g. psycopg2 missing, connection error) must NOT skip the email:
        # log it loudly and continue so notify still runs.
        try:
            db_save.run(cfg, dry_run=True if args.db_dry_run else None)
        except Exception as exc:
            print(f"[db/{cfg.PRODUCT}] FAILED: {exc!r} -- continuing to notify", flush=True)
    if "notify" in steps:
        notify.run(cfg)
    return 0
