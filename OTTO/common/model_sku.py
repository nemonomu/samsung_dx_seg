"""Shared Kasada-free /vergleich/ context for TV/REF: Modellbezeichnung (sku fallback for
space-separated models like "Movie Smart 22 VX") and other characteristics (e.g. REF
Gesamtrauminhalt for beverage coolers the datasheet/EPREL miss).

Keyed and queried by the original variation_id. Never substitute a product-level
representative: another option can have a different model and specifications.
"""
from __future__ import annotations

import re
from typing import Any

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


def model_context(targets: list[dict[str, Any]] | None, suchbegriff,
                  labels: tuple[str, ...] = ("Modellbezeichnung",),
                  required_any: list[tuple[str, ...]] | None = None) -> dict[str, Any]:
    """Read each selected option, deduplicating only identical variation IDs.

    `suchbegriff` is retained for category-call compatibility; no search is used to
    replace the selected option. Missing comparison cells use the existing bounded
    retry path, followed by the caller's row-specific fallback sources.
    """
    ids = list(dict.fromkeys(str(t.get("variation_id") or "").strip()
                             for t in (targets or [])))
    ids = [vid for vid in ids if vid]
    chars = compare.characteristics_map(ids, list(labels), required=["Modellbezeichnung"],
                                        required_any=required_any) if ids else {}
    return {"model": {vid: chars.get(vid, {}) for vid in ids}}


def model_sku(target: dict[str, Any], ctx: dict[str, Any] | None) -> str | None:
    vid = str(target.get("variation_id") or "").strip()
    return clean_model((ctx or {}).get("model", {}).get(vid, {}).get("Modellbezeichnung"))


def characteristic(target: dict[str, Any], ctx: dict[str, Any] | None, *labels: str) -> str | None:
    """First present value among `labels` from the /vergleich/ context (by variation_id).
    Skips OTTO 'no data' placeholders ('-', etc.) so they are not stored as a value."""
    vid = str(target.get("variation_id") or "").strip()
    c = (ctx or {}).get("model", {}).get(vid, {})
    for lbl in labels:
        v = c.get(lbl)
        if has_value(v):
            return v
    return None
