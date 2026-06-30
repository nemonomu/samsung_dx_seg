"""MMKT pipeline orchestrator — run all steps for a product line, ZenRows-free.

  python run.py --product tv                       # full pipeline
  python run.py --product ref --concurrency 1
  python run.py --product tv --steps listing,bsr,detail,full,db
  python run.py --product ref --dry-db             # everything but the real DB write

Each step is a separate process (common.listing / pdp_detail / full_output /
db_save) so a crash in one PDP batch doesn't lose the rest. UC concurrency>1 in
threads is unreliable — default 1.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the MMKT pipeline for a product line.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    p.add_argument("--steps", default="listing,bsr,detail,full,db,notify",
                   help="comma list of: listing,bsr,detail,full,db,notify")
    p.add_argument("--target", type=int, default=0, help="main listing target (0 = product default)")
    p.add_argument("--bsr-target", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--transport", choices=["uc", "zenrows"], default="uc")
    p.add_argument("--dry-db", action="store_true", help="db step runs --dry-run")
    p.add_argument("--no-replace", action="store_true", help="db step keeps existing rows (batch-replace only)")
    p.add_argument("--resume", action="store_true", help="detail step keeps already-collected rows, fetches only the rest")
    return p.parse_args()


def run_step(mod: str, *args: str) -> None:
    cmd = [sys.executable, "-m", mod, *args]
    print(f"\n=== {' '.join(cmd)} ===", flush=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}  # live (unbuffered) child output
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        print(f"!! step {mod} exited {r.returncode}", flush=True)


def main() -> int:
    args = parse_args()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    P = ["--product", args.product]
    T = args.transport

    if "listing" in steps:
        extra = ["--target", str(args.target)] if args.target else []
        run_step("common.listing", *P, "--transport", T, "--sleep", "0", *extra)
    if "bsr" in steps:
        run_step("common.listing", *P, "--sort", "bsr", "--target", str(args.bsr_target),
                 "--transport", T, "--sleep", "0")
    if "detail" in steps:
        extra = ["--resume"] if args.resume else []
        run_step("common.pdp_detail", *P, "--transport", T, "--concurrency", str(args.concurrency), *extra)
    if "full" in steps:
        run_step("common.full_output", *P)
    if "db" in steps:
        db_args = ["--dry-run"] if args.dry_db else ([] if args.no_replace else ["--replace-account"])
        run_step("common.db_save", *P, *db_args)
    if "notify" in steps:
        run_step("common.notify", *P)
    print("\n[run] pipeline done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
