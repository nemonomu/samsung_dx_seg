"""Entry point for the OTTO SEG TV pipeline."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OTTO_CATEGORY"] = "tv"

from common import pipeline  # noqa: E402
from tv import config  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(pipeline.run(config))
