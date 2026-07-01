"""OTTO SEG REF (refrigerator) category config."""
from __future__ import annotations

import re
from typing import Any

from common import datasheet, model_sku, parsers
from common.io_util import RETAILER, COUNTRY as _COUNTRY, env_value, transliterate

PRODUCT = "REF"
COUNTRY = _COUNTRY
ACCOUNT_NAME = RETAILER
SEARCH_TERM = "kühlschränke"
SUCHBEGRIFF = transliterate(SEARCH_TERM)  # kuehlschraenke (everglades needs umlaut transliteration)
WARMUP_LISTING_URL = "https://www.otto.de/suche/kühlschränke/"
DB_TABLE = env_value("SEG_REF_DB_FINAL_TABLE", "dx_seg.dx_seg_ref_retail_com")

SPEC_FIELDS = ["ref_refrigerator_type", "ref_capacity"]
USE_DATASHEET = True
# ref_refrigerator_type is authoritatively a PDP "Kühlschranktyp" characteristic; the
# listing name carries the same value Kasada-free, so it is the default and the PDP
# supplement (when enabled) overrides it.
PDP_SUPPLEMENT_FIELDS = ["ref_refrigerator_type"]

# German fridge type -> English (longest first so combos match before plain Kühlschrank)
REF_TYPE_MAP = [
    ("kühl-/gefrierkombination", "Fridge-freezer Combination"),
    ("kühl-gefrierkombination", "Fridge-freezer Combination"),
    ("kühl-gefrierkombi", "Fridge-freezer Combination"),
    ("gefrier-/kühlkombination", "Fridge-freezer Combination"),
    ("side-by-side", "Side by Side"),
    ("french door", "French Door"),
    ("multi door", "Multi Door"),
    ("multidoor", "Multi Door"),
    ("einbaukühlschrank", "Built-in Refrigerator"),
    ("weinkühlschrank", "Wine Cooler"),
    ("gefriertruhe", "Chest Freezer"),
    ("gefrierschrank", "Freezer"),
    ("kühlschrank", "Refrigerator"),
]


def translate_ref_type(value: str | None) -> str | None:
    key = _norm(value)
    if not key:
        return None
    for german, english in REF_TYPE_MAP:
        if german in key:
            return english
    return value  # unknown type -> keep raw
POSITIVE_KEYWORDS = tuple(k for k, _ in REF_TYPE_MAP)
EXCLUDE_KEYWORDS = (
    "wasserfilter", "filter", "ersatzteil", "einlegeboden", "abdeckung", "zubehör",
    "schublade", "türgriff", "scharnier", "halterung", "untergestell",
)


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().casefold() or None


def classify(name: str | None) -> tuple[bool, str]:
    key = _norm(name)
    if not key:
        return False, "missing_retailer_sku_name"
    hits = [t for t in EXCLUDE_KEYWORDS if t in key]
    has_type = any(t in key for t in POSITIVE_KEYWORDS)
    if hits and not has_type:
        return False, "exclude_keyword:" + ",".join(hits)
    if has_type:
        return True, "ref_type_keyword"
    return False, "missing_ref_keyword"


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None,
                 sku: str | None = None) -> dict[str, Any]:
    capacity = datasheet.value_with_unit(ds, "Gesamtrauminhalt", "l")
    # Kasada-free default from the listing name; PDP supplement overrides if enabled.
    ref_type = translate_ref_type(target.get("retailer_sku_name"))
    return {"ref_refrigerator_type": ref_type, "ref_capacity": capacity}


def prepare_context(targets=None) -> dict[str, Any]:
    # /vergleich/ Modellbezeichnung as a sku fallback for space-separated models
    return model_sku.model_context(targets)


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    return model_sku.model_sku(target, ctx)


def extract_pdp_spec(soup) -> dict[str, Any]:
    raw = parsers.characteristic_by_label(soup, "Kühlschranktyp", "Gerätetyp", "Geräteart", "Bauart", "Produktart", "Typ")
    return {"ref_refrigerator_type": translate_ref_type(raw)} if raw else {}
