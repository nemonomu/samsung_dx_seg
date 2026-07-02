"""EU EPREL energy registry lookup (Kasada-free, structured).

Every EU energy-labelled electronic display / appliance is registered in EPREL with
its on-mode power etc. This recovers fields when the OTTO energy datasheet PDF is
image-only (no extractable text). Public API, keyed by modelIdentifier (partial match
works, e.g. '50UV1563DD' -> '50UV1563DDW').
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any
from urllib.parse import quote

_BASE = "https://eprel.ec.europa.eu/api/products"
_HDR = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://eprel.ec.europa.eu/",
}


def _search(group: str, model: str, timeout: int, retries: int = 1) -> list[dict[str, Any]]:
    url = f"{_BASE}/{group}?modelIdentifier={quote(model)}&_page=1&_limit=5"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HDR), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace")).get("hits", []) or []
        except Exception:
            if attempt < retries:
                time.sleep(1.5)
    return []


def _best_hit(hits: list[dict[str, Any]], model: str) -> dict[str, Any] | None:
    if not hits:
        return None
    key = (model or "").strip().lower()
    for h in hits:
        if str(h.get("modelIdentifier", "")).strip().lower() == key:
            return h
    for h in hits:
        if str(h.get("modelIdentifier", "")).strip().lower().startswith(key):
            return h
    return hits[0]


def _fmt(value: Any) -> str | None:
    if value in (None, "", "NA", 0, "0"):
        return None
    return f"{value} W"


def display_on_mode_power(model: str | None, *, timeout: int = 30) -> str | None:
    """HDR on-mode power for an electronic display as '<n> W' (SDR is not a collection target)."""
    if not model or not model.strip():
        return None
    hit = _best_hit(_search("electronicdisplays", model.strip(), timeout), model.strip())
    if not hit:
        return None
    return _fmt(hit.get("powerOnModeHDR"))


def fridge_total_volume(model: str | None, *, timeout: int = 30) -> str | None:
    """Total volume (Gesamtrauminhalt) for a refrigerating appliance as '<n> l'."""
    if not model or not model.strip():
        return None
    hit = _best_hit(_search("refrigeratingappliances2019", model.strip(), timeout), model.strip())
    if not hit:
        return None
    v = hit.get("totalVolume")
    return f"{v} l" if v not in (None, "", "NA", 0, "0") else None
