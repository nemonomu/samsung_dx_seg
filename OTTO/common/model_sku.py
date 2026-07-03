"""Shared Kasada-free /vergleich/ context for TV/REF: Modellbezeichnung (sku fallback for
space-separated models like "Movie Smart 22 VX") and other characteristics (e.g. REF
Gesamtrauminhalt for beverage coolers the datasheet/EPREL miss).

Keyed by product_id and queried with the CURRENT bestVariationId — stored target vids go
stale (OTTO switches bestVariationId) and then /vergleich/ returns an empty column.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any
from urllib.parse import urlencode

from common import compare

_COLOR_SUFFIX = re.compile(r"\s+(weiss|weiß|schwarz|grau|silber|anthrazit|edelstahl|inox|titan)\s*$", re.I)
_EVER_URL = "https://www.otto.de/everglades/products"
_EVER_HDR = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}


def _ever_fetch(rule: str, offset: int, timeout: int = 45, attempts: int = 3) -> dict | None:
    url = _EVER_URL + "?" + urlencode([("rule", rule), ("intents", "ranked"), ("ranked.offset", str(offset))])
    for _ in range(attempts):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_EVER_HDR), timeout=timeout).read())
        except Exception:
            time.sleep(1.5)
    return None


def _pid_vid_map(suchbegriff: str, hard_cap: int = 4000) -> dict[str, str]:
    """{product_id: current bestVariationId} from the everglades listing for `suchbegriff`."""
    out: dict[str, str] = {}
    offset = 0
    total = None
    fails = 0
    rule = f"(und.(suchbegriff.{suchbegriff}).(~.(v.1)))"
    while offset < hard_cap:
        data = _ever_fetch(rule, offset)
        if data is None:
            fails += 1
            if fails >= 3:
                break
            continue
        fails = 0
        intent = next((it for it in data.get("intents", []) if it.get("intent") == "ranked"), {})
        products = intent.get("products", []) or []
        if total is None:
            total = intent.get("count")
        if not products:
            break
        for p in products:
            pid = str(p.get("id") or "")
            vid = p.get("bestVariationId") or p.get("id")
            if pid and vid:
                out.setdefault(pid, str(vid))
        offset += len(products)
        if total and offset >= total:
            break
    return out


def model_context(targets: list[dict[str, Any]] | None, suchbegriff: str,
                  labels: tuple[str, ...] = ("Modellbezeichnung",)) -> dict[str, Any]:
    """{"model": {product_id: {label: value, _name: ...}}} via /vergleich/ on current vids."""
    targets = targets or []
    pid_vid = _pid_vid_map(suchbegriff)
    q: list[tuple[str, str]] = []
    seen: set[str] = set()
    for t in targets:
        pid = str(t.get("product_id") or "").strip()
        vid = pid_vid.get(pid) or str(t.get("variation_id") or "").strip()
        if pid and vid and pid not in seen:
            seen.add(pid)
            q.append((pid, vid))
    chars = compare.characteristics_map([vid for _, vid in q], list(labels), required=["Modellbezeichnung"]) if q else {}
    return {"model": {pid: chars.get(vid, {}) for pid, vid in q}}


def model_sku(target: dict[str, Any], ctx: dict[str, Any] | None) -> str | None:
    pid = str(target.get("product_id") or "")
    model = (ctx or {}).get("model", {}).get(pid, {}).get("Modellbezeichnung")
    if not model or not model.strip():
        return None
    return _COLOR_SUFFIX.sub("", model.strip()).strip() or None


def characteristic(target: dict[str, Any], ctx: dict[str, Any] | None, *labels: str) -> str | None:
    """First present value among `labels` from the /vergleich/ context (by product_id)."""
    pid = str(target.get("product_id") or "")
    c = (ctx or {}).get("model", {}).get(pid, {})
    for lbl in labels:
        v = c.get(lbl)
        if v and str(v).strip():
            return v
    return None
