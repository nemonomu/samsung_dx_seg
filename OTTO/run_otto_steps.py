"""Run the current OTTO captured-page pipeline."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STEPS = [
    "step00_schema.py",
    "step01_listing.py",
    "step02_final_targets.py",
    "step08_detail_review_compare.py",
    "step14_db_save.py",
    "step15_email_notify.py",
]


def main() -> int:
    root = Path(__file__).resolve().parent
    for step in STEPS:
        print(f"[run] {step}")
        result = subprocess.run([sys.executable, str(root / step)], cwd=str(root))
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
