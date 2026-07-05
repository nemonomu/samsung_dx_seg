"""Shared paths, env, and CSV/JSON helpers for Amazon SEG pipelines."""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Any

AMZN_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = AMZN_ROOT.parent
REFERENCES_ROOT = AMZN_ROOT / "references"

RETAILER = "Amazon.de"
ACCOUNT_NAME = "Amazon"
COUNTRY = "SEG"


def category_output_root(category: str) -> Path:
    env = os.getenv(f"AMZN_{category.upper()}_OUTPUT_ROOT")
    return Path(env) if env else AMZN_ROOT / category.upper() / "data" / "output"


def category_reference_root(category: str) -> Path:
    return REFERENCES_ROOT / category.lower()


def ensure_dirs(category: str) -> tuple[Path, Path]:
    out = category_output_root(category)
    ref = category_reference_root(category)
    for path in (out, ref):
        path.mkdir(parents=True, exist_ok=True)
    return out, ref


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
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
    try:
        import config as runtime_config  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("config.py with DB_CONFIG is required") from exc
    config = dict(getattr(runtime_config, "DB_CONFIG", {}) or {})
    if not config:
        raise RuntimeError("config.DB_CONFIG missing")
    config.setdefault("database", "postgres")
    return config


def split_table(qualified: str) -> tuple[str, str]:
    schema, _, table = qualified.partition(".")
    return (schema, table) if table else ("public", schema)


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
