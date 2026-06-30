"""Fetch OTTO 'Ähnliche Artikel' (similar articles) without the Kasada PDP.

The PDP renders the AlternativesCinema from this JSON service:

    GET https://www.otto.de/reco-core/cinemas/alternative?variationId=<VID>

Response is application/json (no Kasada). Each recommendation exposes:
    brandName
    _embedded["o:variation"].name

joined to "<brand> <name>" — the same value the PDP's reco tiles show.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RECO_ALTERNATIVE_URL = "https://www.otto.de/reco-core/cinemas/alternative"
MULTI_VALUE_DELIMITER = " ||| "
DEFAULT_LIMIT = 20

RECO_HEADERS = {
    "Accept": "application/json, */*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.otto.de/suche/fernseher/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def _recommendation_name(rec: dict[str, Any]) -> str | None:
    if not isinstance(rec, dict):
        return None
    brand = rec.get("brandName")
    embedded = rec.get("_embedded") or {}
    variation = embedded.get("o:variation") if isinstance(embedded, dict) else None
    name = variation.get("name") if isinstance(variation, dict) else None
    parts = [str(part).strip() for part in (brand, name) if part and str(part).strip()]
    if not parts:
        return None
    full = " ".join(parts)
    if brand and name and str(name).strip().lower().startswith(str(brand).strip().lower()):
        full = str(name).strip()
    return full


def fetch_similar_product_names(
    variation_id: str | None,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout: int = 45,
) -> dict[str, Any]:
    """Return {retailer_sku_name_similar, similar_count, reco_http_status, reco_error}."""
    result: dict[str, Any] = {
        "retailer_sku_name_similar": None,
        "similar_count": 0,
        "reco_http_status": None,
        "reco_error": None,
    }
    if not variation_id:
        result["reco_error"] = "missing_variation_id"
        return result
    url = RECO_ALTERNATIVE_URL + "?" + urlencode({"variationId": variation_id})
    try:
        with urlopen(Request(url, headers=RECO_HEADERS, method="GET"), timeout=timeout) as response:
            result["reco_http_status"] = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        result["reco_http_status"] = exc.code
        result["reco_error"] = repr(exc)
        return result
    except (URLError, json.JSONDecodeError) as exc:
        result["reco_error"] = repr(exc)
        return result

    recommendations = ((payload.get("data") or {}).get("recommendations")) or []
    names: list[str] = []
    for rec in recommendations:
        name = _recommendation_name(rec)
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    result["retailer_sku_name_similar"] = MULTI_VALUE_DELIMITER.join(names) or None
    result["similar_count"] = len(names)
    return result
