"""OTTO crawler path and run configuration."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

OTTO_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = OTTO_ROOT.parent
REFERENCES_ROOT = OTTO_ROOT / "references"
HAR_ROOT = Path(os.getenv("OTTO_HAR_ROOT", REFERENCES_ROOT / "har"))
SCHEMA_ROOT = REFERENCES_ROOT / "schema"
RUN_ROOT = Path(os.getenv("OTTO_RUN_ROOT", OTTO_ROOT / "data"))
OUTPUT_ROOT = Path(os.getenv("OTTO_OUTPUT_ROOT", RUN_ROOT / "output"))

RETAILER = "OTTO"
PRODUCT = "TV"
COUNTRY = "SEG"
SEARCH_TERM = "fernseher"
TOPSELLER_URL = "https://www.otto.de/suche/fernseher/?sortiertnach=topseller"
MAIN_TARGET_UNIQUE = int(os.getenv("OTTO_MAIN_TARGET_UNIQUE", "300"))
BSR_TARGET_RANK = int(os.getenv("OTTO_BSR_TARGET_RANK", "100"))
LISTING_PAGES_TO_COLLECT = int(os.getenv("OTTO_LISTING_PAGES_TO_COLLECT", "4"))
LISTING_POSITIONS_PER_PAGE = int(os.getenv("OTTO_LISTING_POSITIONS_PER_PAGE", "120"))
UNIQUE_KEY = "retailer_sku_name"

CANONICAL_LISTING_HTML = Path(os.getenv("OTTO_CANONICAL_LISTING_HTML", HAR_ROOT / "listing_bsr_o120.html"))
MAIN_CAPTURE_HTML = HAR_ROOT / "listing_main.html"
BSR_CAPTURE_HTML = HAR_ROOT / "listing_bsr_o120.html"
MANUAL_DETAIL_SAMPLE_HTML = REFERENCES_ROOT / "detail.html"
DETAIL_SAMPLE_HTML = Path(os.getenv("OTTO_DETAIL_SAMPLE_HTML", MANUAL_DETAIL_SAMPLE_HTML if MANUAL_DETAIL_SAMPLE_HTML.exists() else HAR_ROOT / "detail_sample.html"))
COMPARE_SAMPLE_HTML = HAR_ROOT / "compare_sample.html"
DETAIL_SAMPLE_URL = "https://www.otto.de/p/philips-32phs6000-12-led-fernseher-80-cm-32-zoll-hd-ready-smart-tv-pixel-plus-hd-hdr10-hlg-dolby-audio-vocal-boost-C2000548368/"
REVIEW_SAMPLE_URL = "https://www.otto.de/kundenbewertungen/C2000548368/"
REVIEW_SAMPLE_HTML = Path(os.getenv("OTTO_REVIEW_SAMPLE_HTML", HAR_ROOT / "sample_philips" / "review_philips.html"))


def ensure_dirs() -> None:
    for path in (REFERENCES_ROOT, HAR_ROOT, SCHEMA_ROOT, RUN_ROOT, OUTPUT_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))



