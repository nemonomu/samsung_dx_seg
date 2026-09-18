"""Deterministic English discount labels and diagnostics for unknown badges."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

MULTI_VALUE_DELIMITER = " ||| "
TEXT_TRANSLATIONS = {
    "Gesponsert": "Sponsored",
    "gesponsert": "Sponsored",
    "Preisheld": "Price champion",
    "Gratis Standard-Lieferung": "Free standard delivery",
    "0% Finanzierung": "0% financing",
    "Deal des Tages": "Deal of the day",
    "Deal der Woche": "Deal of the week",
    "Deal des Monats": "Deal of the month",
    "Tiefpreis": "Lowest price",
    "Neu": "New",
    "Unsere Eigenmarke": "Our own brand",
    "Gewinnspiel": "Prize draw",
    "Inkl. Streaming Content": "Incl. streaming content",
    "WM-Highlight": "World Cup highlight",
    "myMediaMarkt-Rabatt verfügbar": "myMediaMarkt discount available",
    "-30€ mit Kalibrierung": "-30€ with calibration",
    "Auch für Geschäftskunden": "Also for business customers",
    "Mini LED mit QLED": "Mini LED with QLED",
    "Technik Highlight": "Tech highlight",
    "Läuft mit Powerbank": "Runs on power bank",
    "Gratis Versand": "Free shipping",
}


def _clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


_TRANSLATIONS = {key.casefold(): value for key, value in TEXT_TRANSLATIONS.items()}
_TRANSLATIONS.update({value.casefold(): value for value in TEXT_TRANSLATIONS.values()})
# English alternatives make translation idempotent at the DB boundary.
_NUMBER = r"-?\d+(?:[.,]\d+)*"
_PATTERNS = (
    (rf"({_NUMBER}\s*€) (?:mit Kalibrierung|with calibration)", "{0} with calibration"),
    (rf"({_NUMBER}\s*%) (?:Finanzierung|financing)", "{0} financing"),
    (rf"({_NUMBER}\s*(?:€|%)) (?:Rabatt|discount)", "{0} discount"),
    (rf"(?:Bis zu|Up to) ({_NUMBER}\s*(?:€|%)) (?:Rabatt|discount)", "Up to {0} discount"),
)


def translate_discount_type(value: Any) -> tuple[str | None, list[str]]:
    """Return known English labels and unknown originals, preserving order.

    Never guess that an unrecognized ASCII label is English: German marketing
    labels often contain no umlauts. Only approved labels/patterns pass through.
    """
    translated: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for part in str(value or "").split("|||"):
        label = _clean(part)
        key = label.casefold()
        if not label or key in seen:
            continue
        seen.add(key)
        english = _TRANSLATIONS.get(key)
        if english is None:
            for pattern, template in _PATTERNS:
                match = re.fullmatch(pattern, label, flags=re.I)
                if match:
                    english = template.format(*match.groups())
                    break
        if english is None:
            unknown.append(label)
        elif english not in translated:
            translated.append(english)
    return MULTI_VALUE_DELIMITER.join(translated) or None, unknown


def normalize_discount_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize output rows in place; preserve rejected text in the manifest."""
    items = []
    for row in rows:
        raw = row.get("discount_type")
        english, unknown = translate_discount_type(raw)
        # Do not introduce a column in legacy files that never contained it.
        if "discount_type" in row:
            row["discount_type"] = english
        if unknown:
            items.append({
                "sku_id": str(row.get("item") or row.get("sku_id") or ""),
                "product_url": row.get("product_url"),
                "raw": raw,
                "untranslated": unknown,
                "all_untranslated": english is None,
            })
    return {
        "rows_with_untranslated": len(items),
        "rows_all_untranslated": sum(item["all_untranslated"] for item in items),
        "items": items,
    }
