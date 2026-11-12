"""Usage: python OTTO/dry/run.py [--limit 300] [--load-only path/to/otto_dry.csv]."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dry import config
from dry.pipeline import execute


def main(argv=None):
    parser = argparse.ArgumentParser(description="OTTO DRY: Topseller -> 20 columns -> retail DB + report")
    parser.add_argument("--limit", type=int, default=int(os.getenv("OTTO_DRY_LIMIT", config.DEFAULT_LIMIT)))
    parser.add_argument("--table", default=os.getenv("OTTO_DRY_TABLE", config.DEFAULT_TABLE))
    parser.add_argument("--output-root", type=Path, default=Path(os.getenv("OTTO_DRY_OUTPUT_ROOT", str(config.OUTPUT_ROOT))))
    parser.add_argument("--load-only", type=Path, help="Load completed CSV plus adjacent state.json; no site requests")
    args = parser.parse_args(argv)
    try:
        code, _ = execute(limit=args.limit, table=args.table, output_root=args.output_root, load_only=args.load_only)
        return code
    except config.DryError as exc:
        print("OTTO DRY: " + str(exc), flush=True)
        return 5
    except Exception as exc:
        print("OTTO DRY startup: " + type(exc).__name__, flush=True)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
