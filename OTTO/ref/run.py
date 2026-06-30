"""Entry point for the OTTO SEG REF pipeline."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OTTO_CATEGORY"] = "ref"

from common import pipeline  # noqa: E402
from ref import config  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(pipeline.run(config))
