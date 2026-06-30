"""Opt-in raw-HTML capture for debugging/audit.

The active pipeline parses live responses in memory and does not persist HTML.
Set OTTO_SAVE_HTML=1 (or pass --save-html) to also write each fetched HTML page to
<category>/data/output/raw_html/ so a parse can be audited against its source.
Off by default; never affects parsing.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from common.category import active_category
from common.io_util import category_output_root

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return str(os.getenv("OTTO_SAVE_HTML", "")).strip().lower() in _TRUTHY


def _safe(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "page"
    return name if name.lower().endswith(".html") else name + ".html"


def raw_dir(category: str | None = None) -> Path:
    return category_output_root(category or active_category()) / "raw_html"


def save(name: str, body: bytes | str, *, category: str | None = None) -> Path | None:
    """Write `body` to raw_html/<name>.html when capture is enabled; else no-op."""
    if not enabled():
        return None
    d = raw_dir(category)
    d.mkdir(parents=True, exist_ok=True)
    path = d / _safe(name)
    path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8", errors="replace"))
    return path
