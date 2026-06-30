"""Shared paths and I/O helpers for the OTTO category pipelines (TV/REF/LDY).

Category-agnostic. The active category resolves output under OTTO/<category>/data/output.
DB and email credentials are read from the project .env and never printed.
"""
from __future__ import annotations

import ast
import csv
import json
import os
import re
from pathlib import Path
from typing import Any

OTTO_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = OTTO_ROOT.parent
REFERENCES_ROOT = OTTO_ROOT / "references"
SCHEMA_ROOT = REFERENCES_ROOT / "schema"
HAR_ROOT = Path(os.getenv("OTTO_HAR_ROOT", REFERENCES_ROOT / "har"))

RETAILER = "OTTO"
COUNTRY = "SEG"

UMLAUTS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}


def transliterate(term: str) -> str:
    """German umlaut transliteration for the everglades suchbegriff (kühlschränke -> kuehlschraenke)."""
    return "".join(UMLAUTS.get(ch, ch) for ch in term)


def category_output_root(category: str) -> Path:
    return Path(os.getenv("OTTO_OUTPUT_ROOT", OTTO_ROOT / category / "data" / "output"))


def ensure_dirs(category: str) -> Path:
    out = category_output_root(category)
    for path in (REFERENCES_ROOT, HAR_ROOT, SCHEMA_ROOT, out):
        path.mkdir(parents=True, exist_ok=True)
    return out


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


def _env_text() -> str:
    env_path = PROJECT_ROOT / ".env"
    return env_path.read_text(encoding="utf-8-sig", errors="replace") if env_path.exists() else ""


def env_value(name: str, default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    match = re.search(rf"^{re.escape(name)}\s*=\s*(.*)$", _env_text(), re.M)
    if not match:
        return default
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def db_config() -> dict[str, Any]:
    match = re.search(r"DB_CONFIG\s*=\s*(\{.*?\})", _env_text(), re.S)
    if not match:
        return {}
    config = ast.literal_eval(match.group(1))
    config.setdefault("database", "postgres")
    return config


def split_table(qualified: str) -> tuple[str, str]:
    schema, _, table = qualified.partition(".")
    return (schema, table) if table else ("public", schema)


def top_info(target: dict[str, Any], *label_contains: str) -> str | None:
    """Read a value from a listing row's top_infos JSON by partial label match."""
    raw = target.get("top_infos")
    if not raw:
        return None
    try:
        infos = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    for label, value in infos.items():
        if any(key.lower() in label.lower() for key in label_contains):
            return value
    return None
