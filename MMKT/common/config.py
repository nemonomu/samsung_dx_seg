"""Shared (product-agnostic) MMKT configuration + helpers.

Per-product modules (tv/config.py, ref/config.py, ldy/config.py) import this and
add the product-specific bits: category URLs, DB table, output dir, and the PDP
spec-field extractor. All product lines collect from MediaMarkt as
account_name="Mediamarkt". See memory mmkt-bot-defense / mmkt-ref-ldy-spec.
"""
from __future__ import annotations

import ast
import csv
import json
import os
import re
from pathlib import Path
from typing import Any

MMKT_ROOT = Path(__file__).resolve().parent.parent      # .../MMKT
PROJECT_ROOT = MMKT_ROOT.parent
REFERENCES_ROOT = MMKT_ROOT / "common" / "references"   # shared HAR/probe captures
MMKT_BASE = "https://www.mediamarkt.de"

RETAILER = "MediaMarkt"
ACCOUNT_NAME = "Mediamarkt"   # DB account_name (batch-replace key), all product lines
COUNTRY = "SEG"
PAGE_TYPE = "main"
POSTAL_CODE = "10117"         # MediaMarkt Berlin Leipziger Platz

LISTING_PAGE_SIZE = 12        # server-rendered organic products per ?page=N
UNIQUE_KEY = "sku_id"


def product_output_root(product: str) -> Path:
    """Per-product output dir, e.g. MMKT/tv/data/output (override via env)."""
    env = os.getenv(f"MMKT_{product.upper()}_OUTPUT_ROOT")
    return Path(env) if env else MMKT_ROOT / product.lower() / "data" / "output"


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def page_url(base: str, page: int) -> str:
    """Append ?page=N (or &page=N) to a listing/BSR base URL."""
    if page <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page}"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


# --- DB helpers (shared SEG retail.com tables; see memory otto-db-load) --------

def _env_text() -> str:
    env_path = PROJECT_ROOT / ".env"
    return env_path.read_text(encoding="utf-8-sig", errors="replace") if env_path.exists() else ""


def env_value(name: str, default: str | None = None) -> str | None:
    """Read a value from os.environ or the project .env (quotes stripped)."""
    if name in os.environ:
        return os.environ[name]
    m = re.search(rf"^{re.escape(name)}\s*=\s*(.*)$", _env_text(), re.M)
    if not m:
        return default
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def db_config() -> dict[str, Any]:
    """Parse the DB_CONFIG dict literal from the project .env."""
    match = re.search(r"DB_CONFIG\s*=\s*(\{.*?\})", _env_text(), re.S)
    if not match:
        return {}
    config = ast.literal_eval(match.group(1))
    config.setdefault("database", "postgres")
    return config


def seg_final_table(env_var: str, default: str) -> tuple[str, str]:
    """(schema, table) for a SEG retail.com table, from env_var or default."""
    raw = None
    m = re.search(rf"^{re.escape(env_var)}\s*=\s*(.*)$", _env_text(), re.M)
    if m:
        raw = m.group(1).strip().strip('"').strip("'")
    raw = os.getenv(env_var) or raw or default
    schema, _, table = raw.partition(".")
    return (schema, table) if table else ("public", schema)
