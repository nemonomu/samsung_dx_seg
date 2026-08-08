"""Lightweight classification for browser-session failures."""
from __future__ import annotations

from typing import Any


FATAL_BROWSER_ERROR_MARKERS = (
    "tab crashed",
    "session deleted because of page crash",
    "invalid session id",
    "disconnected",
    "not connected to devtools",
    "no such window",
    "chrome not reachable",
    "unable to receive message from renderer",
    "failed to establish a new connection",
    "connection refused",
    "actively refused it",
    "winerror 10061",
    "remote end closed connection without response",
)


def fatal_browser_error(value: Any) -> str | None:
    """Return the matched fatal browser-session marker, if any."""
    if isinstance(value, dict):
        value = value.get("error")
    text = str(value or "").casefold()
    return next((marker for marker in FATAL_BROWSER_ERROR_MARKERS if marker in text), None)
