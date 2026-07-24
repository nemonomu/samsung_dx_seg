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


_REF_TYPE_EXCLUDES = (
    "gefrierschrank", "gefriertruhe", "getränkekühlschrank", "getraenkekuehlschrank",
    "getränkekühler", "getraenkekuehler", "fleischreifeschrank", "kühlvitrine",
    "kuehlvitrine", "kühlbox", "kuehlbox", "beverage cooler", "meat aging cabinet",
    "display refrigerator", "cooler box",
)


def _type_key(value: str | None) -> str:
    text = _norm(value) or ""
    text = re.sub(r"\s*[-/]\s*", "-", text)
    text = re.sub(r"-und\s+|\s+und\s+", "-", text)
    return re.sub(r"-+", "-", re.sub(r"\s+", " ", text))


def translate_ref_type(value: str | None) -> str | None:
    key = _type_key(value)
    if not key or any(token in key for token in _REF_TYPE_EXCLUDES):
        return None
    for german, english in REF_TYPE_MAP:
        if _type_key(german) in key:
            return english
    return value  # unknown type -> keep raw


POSITIVE_KEYWORDS = tuple(k for k, _ in REF_TYPE_MAP)
EXCLUDE_KEYWORDS = (
    "wasserfilter", "filter", "ersatzteil", "einlegeboden", "abdeckung", "zubehör",
    "schublade", "türgriff", "scharnier", "halterung", "untergestell",
    # accessories/consumables that carry "kühlschrank" in the name but aren't fridges
    "möbelfolie", "folie", "aufkleber", "organizer", "abtauhilfe", "flüssigreiniger",
    "kühlbox", "dosenspender", "reiniger",
    # measuring / locking accessories (thermometer, padlock) named "... Kühlschrank ..."
    "thermometer", "schloss",
)


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().casefold() or None


def classify(name: str | None) -> tuple[bool, str]:
    key = _norm(name)
    if not key:
        return False, "missing_retailer_sku_name"
    # hard excludes win, even when a positive keyword is also present
    # (e.g. "Xavax Montagezubehör Kühlschrank" -> excluded via "zubehör")
    hits = [t for t in EXCLUDE_KEYWORDS if t in key]
    if hits:
        return False, "exclude_keyword:" + ",".join(hits)
    if any(t in key for t in POSITIVE_KEYWORDS):
        return True, "ref_type_keyword"
    return False, "missing_ref_keyword"


_NAME_LITER = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:liter|l)\b", re.I)
_NAME_COOLING = re.compile(r"(?:k(?:ü|ue)hl(?:schrank|teil|raum|fach|bereich|zone|ung)?|fridge|refrigerator|cooling)", re.I)
_NAME_FREEZER = re.compile(r"(?:gefrier(?:fach|teil|raum|bereich|zone)?|freezer)", re.I)


def _capacity_from_name(name: str | None) -> str | None:
    """Return explicit product-name volume, preferring the cooling compartment.

    A title can contain both cooling and freezer capacities (e.g. ``249 L
    Kühl + 94 L Gefrier``); in that case the cooling-labelled value is the
    SEG ``ref_capacity``. With one unlabelled volume, keep that value.
    """
    text = name or ""
    matches = list(_NAME_LITER.finditer(text))
    if not matches:
        return None
    if len(matches) > 1:
        for match in matches:
            start, end = match.span()
            before = text[max(0, start - 20):start]
            after = text[end:end + 20]
            labels: list[tuple[int, int, str]] = []
            cooling_before = list(_NAME_COOLING.finditer(before))
            cooling_after = _NAME_COOLING.search(after)
            freezer_before = list(_NAME_FREEZER.finditer(before))
            freezer_after = _NAME_FREEZER.search(after)
            if cooling_before:
                labels.append((len(before) - cooling_before[-1].end(), 1, "cooling"))
            if cooling_after:
                labels.append((cooling_after.start(), 0, "cooling"))
            if freezer_before:
                labels.append((len(before) - freezer_before[-1].end(), 1, "freezer"))
            if freezer_after:
                labels.append((freezer_after.start(), 0, "freezer"))
            if labels and min(labels)[2] == "cooling":
                return f"{match.group(1)} l"
    return f"{matches[0].group(1)} l"


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None,
                 sku: str | None = None) -> dict[str, Any]:
    # ref_capacity = TOTAL volume only (Gesamtrauminhalt/Gesamtnutzinhalt), never a partial
    # like "Rauminhalt der Kühlfächer". Prefer an explicit title volume (cooling-labelled
    # when a title also contains a freezer volume), then structured sources.
    capacity = next((v for v in (
        _capacity_from_name(target.get("retailer_sku_name")),
        datasheet.value_with_unit(ds, "Gesamtrauminhalt", "l"),
        model_sku.characteristic(target, ctx, "Gesamtrauminhalt", "Gesamtnutzinhalt"),
    ) if model_sku.has_value(v)), None)
    # Kasada-free default from the listing name; PDP supplement overrides if enabled.
    ref_type = translate_ref_type(target.get("retailer_sku_name"))
    return {"ref_refrigerator_type": ref_type, "ref_capacity": capacity}


def prepare_context(targets=None) -> dict[str, Any]:
    # /vergleich/ Modellbezeichnung (sku fallback) + Gesamtrauminhalt (capacity for beverage
    # coolers the datasheet/structured comparison page miss), on current bestVariationIds.
    # NOTE: we deliberately do NOT force a capacity re-fetch here (model_context supports
    # required_any). ~70 household fridges legitimately lack a /vergleich/ volume label (their
    # capacity comes from the datasheet), so retrying the whole capacity-missing set would add
    # ~50% more /vergleich/ requests every run and risk throttling the sku/Modellbezeichnung
    # harvest that already works — a bad trade for the rare commercial-cooler cell drop.
    return model_sku.model_context(targets, (SUCHBEGRIFF, "getraenkekuehlschrank"),
                                   labels=("Modellbezeichnung", "Gesamtrauminhalt", "Gesamtnutzinhalt"))


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    return model_sku.model_sku(target, ctx)


def extract_pdp_spec(soup) -> dict[str, Any]:
    raw = parsers.characteristic_by_label(soup, "Kühlschranktyp", "Gerätetyp", "Geräteart", "Bauart", "Produktart", "Typ")
    return {"ref_refrigerator_type": translate_ref_type(raw)} if raw else {}
