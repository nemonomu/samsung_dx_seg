"""Exclude non-layout values and translate approved refrigerator types."""
from __future__ import annotations

import unicodedata
from html import unescape
from typing import Any


_EXCLUDED_REF_TYPES = frozenset({
    "built-in refrigerator",
    "refrigerator",
    "mini fridge",
    "mini refrigerator",
    "mini k\u00fchlschrank",
    "mini kuehlschrank",
    "mini-k\u00fchlschrank",
    "mini-kuehlschrank",
    "stehender vorratsschrank",
    "compact",
    "without water dispenser",
    "chest freezer",
    "fleischreifeschrank",
    "getr\u00e4nkek\u00fchler",
    "getr\u00e4nkek\u00fchlschrank",
    "k\u00fchlbox",
    "party-k\u00fchlbox",
    "generation 2",
})

_REF_TYPE_ALIASES = {
    "vollraumk\u00fchlschrank": "Full-space Refrigerator",
    "vollraumkuehlschrank": "Full-space Refrigerator",
    "compact freezer-on-top": "Freezer-on-top",
    "compact internal freezer compartment": "Internal freezer compartment",
}


def _comparison_key(value: str) -> str:
    key = unicodedata.normalize("NFC", unescape(value)).casefold()
    return " ".join(key.split())


def is_excluded_ref_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    # Normalize the comparison key only; never rewrite a retained value.
    return _comparison_key(value) in _EXCLUDED_REF_TYPES


def normalize_ref_type(value: Any) -> Any:
    """Preserve unlisted values exactly; never treat a compound layout as compact alone."""
    if is_excluded_ref_type(value):
        return None
    if isinstance(value, str):
        return _REF_TYPE_ALIASES.get(_comparison_key(value), value)
    return value


def apply_ref_type_policy(row: dict[str, Any]) -> bool:
    """Normalize only the type field; return True only for a policy NULL.

    Callers use True to skip missing-type retries, so a normalized valid layout
    must return False even though its text changed.
    """
    if "ref_refrigerator_type" not in row:
        return False
    value = row["ref_refrigerator_type"]
    row["ref_refrigerator_type"] = normalize_ref_type(value)
    return is_excluded_ref_type(value)
