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
_EAN_SUFFIX = re.compile(r"\s+\d{6,}$")  # trailing EAN/EPREL number OTTO appends to Modellbezeichnung
_PLACEHOLDERS = {"", "-", "--", "—", "–", "k.a.", "n/a", "keine angabe", "nein", "ja"}


def has_value(v) -> bool:
    return bool(v) and str(v).strip().casefold() not in _PLACEHOLDERS


def clean_model(model: str | None) -> str | None:
    """Normalize a /vergleich/ Modellbezeichnung: keep the first colour variant, drop a
    trailing colour word and a trailing EAN/EPREL number ('LR7EA410FL 914501653' -> ...)."""
    if not has_value(model):
        return None
    m = model.split(",")[0].strip()
    m = _EAN_SUFFIX.sub("", _COLOR_SUFFIX.sub("", m)).strip()
    return m if has_value(m) else None
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


def model_context(targets: list[dict[str, Any]] | None, suchbegriff,
                  labels: tuple[str, ...] = ("Modellbezeichnung",),
                  required_any: list[tuple[str, ...]] | None = None) -> dict[str, Any]:
    """{"model": {product_id: {label: value, _name: ...}}} via /vergleich/ on current vids.
    `suchbegriff` may be a single term or several (e.g. REF also lists beverage coolers
    under 'getraenkekuehlschrank'). `required_any` forces a per-id re-fetch for a vid that
    rendered but dropped a whole either/or group (e.g. Gesamtrauminhalt OR Gesamtnutzinhalt)
    so an intermittently-dropped capacity cell is recovered rather than stored as NULL."""
    targets = targets or []
    terms = [suchbegriff] if isinstance(suchbegriff, str) else list(suchbegriff)
    pid_vid: dict[str, str] = {}
    for term in terms:
        for pid, vid in _pid_vid_map(term).items():
            pid_vid.setdefault(pid, vid)
    q: list[tuple[str, str]] = []
    seen: set[str] = set()
    for t in targets:
        pid = str(t.get("product_id") or "").strip()
        vid = pid_vid.get(pid) or str(t.get("variation_id") or "").strip()
        if pid and vid and pid not in seen:
            seen.add(pid)
            q.append((pid, vid))
    chars = compare.characteristics_map([vid for _, vid in q], list(labels), required=["Modellbezeichnung"],
                                        required_any=required_any) if q else {}
    return {"model": {pid: chars.get(vid, {}) for pid, vid in q}}


def model_sku(target: dict[str, Any], ctx: dict[str, Any] | None) -> str | None:
    pid = str(target.get("product_id") or "")
    return clean_model((ctx or {}).get("model", {}).get(pid, {}).get("Modellbezeichnung"))


def characteristic(target: dict[str, Any], ctx: dict[str, Any] | None, *labels: str) -> str | None:
    """First present value among `labels` from the /vergleich/ context (by product_id).
    Skips OTTO 'no data' placeholders ('-', etc.) so they are not stored as a value."""
    pid = str(target.get("product_id") or "")
    c = (ctx or {}).get("model", {}).get(pid, {})
    for lbl in labels:
        v = c.get(lbl)
        if has_value(v):
            return v
    return None
