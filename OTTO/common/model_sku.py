"""Shared sku fallback via the Kasada-free /vergleich/ Modellbezeichnung.

For categories whose primary sku source (datasheet Modellkennung / name token) can
miss space-separated models (e.g. "Movie Smart 22 VX", "We. SEE 43"), the comparison
page's Modellbezeichnung is an authoritative per-SKU fallback.
"""
from __future__ import annotations

import re
from typing import Any

from common import compare

_COLOR_SUFFIX = re.compile(r"\s+(weiss|weiß|schwarz|grau|silber|anthrazit|edelstahl|inox|titan)\s*$", re.I)


def model_context(targets: list[dict[str, Any]] | None) -> dict[str, Any]:
    vids: list[str] = []
    for t in (targets or []):
        vid = str(t.get("variation_id") or "").strip()
        if vid and vid not in vids:
            vids.append(vid)
    return {"model": compare.characteristics_map(vids, ["Modellbezeichnung"]) if vids else {}}


def model_sku(target: dict[str, Any], ctx: dict[str, Any] | None) -> str | None:
    vid = str(target.get("variation_id") or "")
    model = (ctx or {}).get("model", {}).get(vid, {}).get("Modellbezeichnung")
    if not model or not model.strip():
        return None
    return _COLOR_SUFFIX.sub("", model.strip()).strip() or None
