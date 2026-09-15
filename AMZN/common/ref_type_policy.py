"""Null only explicitly excluded refrigerator-type values."""
from __future__ import annotations

import unicodedata
from html import unescape
from typing import Any


_EXCLUDED_REF_TYPES = frozenset({
    "built-in refrigerator",
    "refrigerator",
    "mini fridge",
    "chest freezer",
    "fleischreifeschrank",
    "getr\u00e4nkek\u00fchler",
    "getr\u00e4nkek\u00fchlschrank",
    "k\u00fchlbox",
    "party-k\u00fchlbox",
    "generation 2",
})


def is_excluded_ref_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    # Normalize the comparison key only; never rewrite a retained value.
    key = unicodedata.normalize("NFC", unescape(value)).casefold()
    return " ".join(key.split()) in _EXCLUDED_REF_TYPES


def apply_ref_type_policy(row: dict[str, Any]) -> bool:
    """Clear a listed value in place without changing other fields or keys."""
    if not is_excluded_ref_type(row.get("ref_refrigerator_type")):
        return False
    row["ref_refrigerator_type"] = None
    return True
