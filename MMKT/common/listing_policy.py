"""Eligibility shared by listing collection and consumers of saved CSVs."""
from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import unquote


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(char for char in text if not unicodedata.combining(char))


def is_advertisement(row: dict[str, Any]) -> bool:
    return (
        _normalized(row.get("sku_status")).strip() in {"sponsored", "gesponsert"}
        or row.get("listing_card_type") == "sponsored-ad"
    )


def listing_exclusion_reason(row: dict[str, Any], product: str) -> str | None:
    if is_advertisement(row):
        return "advertisement"
    if str(product).casefold() != "ldy":
        return None
    name = row.get("retailer_sku_name") or unquote(str(row.get("product_url") or ""))
    description = _normalized(name)
    product_type = _normalized(row.get("product_type"))
    # Washer/dryer combinations, wash towers and laundry bundles remain eligible.
    washing = r"waschmaschine|waschtrockner|wasch[\s/-]*trocken|wash[\s-]*tower|washer|washing[\s-]*machine"
    if re.search(washing, description + " " + product_type):
        return None
    description = product_type + " " + description
    if re.search(r"kuhlschrank|kuehlschrank|kuhl[\s/-]*gefrier|kuehl[\s/-]*gefrier|refrigerator|fridge", description):
        return "refrigerator"
    if re.search(r"trockner|tumble[\s-]*dryer|\bdryer\b", description):
        return "standalone_dryer"
    return None


def filter_listing_rows(rows: list[dict], product: str, *, renumber: bool = False) -> list[dict]:
    """Filter occurrences before SKU deduplication, retaining their display order."""
    selected = []
    seen = set()
    for row in rows:
        sku_id = str(row.get("sku_id") or row.get("item") or "").strip()
        if listing_exclusion_reason(row, product) or not sku_id or sku_id in seen:
            continue
        seen.add(sku_id)
        selected.append(row)
    if renumber and len(selected) != len(rows):
        selected = [dict(row, rank=str(index), position=str(index)) for index, row in enumerate(selected, 1)]
    return selected
