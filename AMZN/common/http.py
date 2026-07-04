"""URL and raw HTML file helpers for Amazon.de browser crawls."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def add_query(url: str, **params: Any) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items() if v not in (None, "")})
    return urlunparse(parsed._replace(query=urlencode(query)))


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8", errors="replace")
