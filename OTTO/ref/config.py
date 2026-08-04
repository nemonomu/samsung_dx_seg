"""OTTO SEG REF (refrigerator) category config."""
from __future__ import annotations

import json
import re
from typing import Any

from common import datasheet, eprel, model_sku, parsers
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
# PDP supplement is reserved for explicitly recoverable missing fields. A missing
# ref_refrigerator_type can be a policy NULL, so do not spend ZenRows PDP calls
# trying to fill it.
PDP_SUPPLEMENT_FIELDS = ["ref_capacity"]

# German refrigerator layout/freezer-position -> English. Product category and
# installation terms (e.g. plain Kuehlschrank, Einbau, Wine Cooler, Freezer) are
# policy NULL for ref_refrigerator_type; they can still guide capacity fallback.
REF_TYPE_MAP = [
    ("k\u00fchl-/gefrierkombination", "Fridge-freezer Combination"),
    ("k\u00fchl-gefrierkombination", "Fridge-freezer Combination"),
    ("k\u00fchl-gefrierkombi", "Fridge-freezer Combination"),
    ("gefrier-/k\u00fchlkombination", "Fridge-freezer Combination"),
    ("kuehl-/gefrierkombination", "Fridge-freezer Combination"),
    ("kuehl-gefrierkombination", "Fridge-freezer Combination"),
    ("kuehl-gefrierkombi", "Fridge-freezer Combination"),
    ("gefrier-/kuehlkombination", "Fridge-freezer Combination"),
    ("side-by-side", "Side by Side"),
    ("french door", "French Door"),
    ("multi door", "Multi Door"),
    ("multidoor", "Multi Door"),
]
_REF_TYPE_POLICY_NULL = (
    "einbauk\u00fchlschrank", "einbaukuehlschrank",
    "built-in refrigerator", "integrated refrigerator",
    "weink\u00fchlschrank", "weinkuehlschrank", "wine cooler", "wine fridge",
    "gefriertruhe", "chest freezer", "gefrierschrank", "freezer",
    "k\u00fchlschrank", "kuehlschrank", "refrigerator",
)


_REF_TYPE_EXCLUDES = (
    "getränkekühlschrank", "getraenkekuehlschrank",
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
    if any(_type_key(token) in key for token in _REF_TYPE_POLICY_NULL):
        return None
    return None


def _is_policy_null_ref_type(value: str | None) -> bool:
    key = _type_key(value)
    if not key:
        return False
    if any(_type_key(layout) in key for layout, _english in REF_TYPE_MAP):
        return False
    policy_tokens = tuple(_REF_TYPE_POLICY_NULL) + tuple(_REF_TYPE_EXCLUDES)
    return any(_type_key(token) in key for token in policy_tokens)


def _single_compartment_kind(value: str | None) -> str | None:
    key = _type_key(value)
    if not key or any(token in key for token in _REF_TYPE_EXCLUDES):
        return None
    if any(_type_key(token) in key for token in ("gefriertruhe", "gefrierschrank", "chest freezer", "freezer")):
        return "freezer"
    if any(_type_key(layout) in key for layout, _english in REF_TYPE_MAP):
        return None
    if any(_type_key(token) in key for token in (
        "einbaukühlschrank", "einbaukuehlschrank",
        "weinkühlschrank", "weinkuehlschrank",
        "kühlschrank", "kuehlschrank",
        "built-in refrigerator", "integrated refrigerator", "wine cooler", "refrigerator",
    )):
        return "cooling"
    return None


POSITIVE_KEYWORDS = tuple(k for k, _ in REF_TYPE_MAP) + (
    "einbauk\u00fchlschrank", "einbaukuehlschrank",
    "weink\u00fchlschrank", "weinkuehlschrank",
    "gefriertruhe", "gefrierschrank",
    "k\u00fchlschrank", "kuehlschrank",
)
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


_NAME_LITER = re.compile(r"(\d+(?:[.,]\d+)?)\s*[- ]?\s*(?:liter|l)\b", re.I)
_NAME_TOTAL = re.compile("(?:gesamt(?:raum|nutz)?inhalt|nutzinhalt\\s*gesamt|total(?: volume| capacity)?)", re.I)
_NAME_COOLING = re.compile("(?:kapazit\\u00e4t\\s*k\\u00fchlen|k(?:\\u00fc|ue)hl(?:en|fach|f\\u00e4cher|faecher|teil|raum|zone|ung)?|fridge|refrigerator|cooling)", re.I)
_NAME_FREEZER = re.compile("(?:kapazit\\u00e4t\\s*frieren|tiefk(?:\\u00fc|ue)hl(?:fach|f\\u00e4cher|faecher|teil|raum)?|gefrier(?:fach|teil|raum|bereich|zone)?|frieren|freezer)", re.I)

_LABEL_TRANSLATION = str.maketrans({
    "\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss",
})
_TOTAL_KEYS = (
    "gesamtrauminhalt", "gesamtnutzinhalt", "nutzinhaltgesamt",
    "gesamtinhalt", "gesamtvolumen", "totalvolume", "totalcapacity",
)
_STANDALONE_TOTAL_KEYS = ("nutzinhalt",)
_COOLING_KEYS = (
    "rauminhaltederkuehlfaecher", "rauminhaltderkuehlfaecher",
    "kapazitaetkuehlen", "kuehlteil", "kuehlfach", "kuehlfaecher", "cooling",
)
_FREEZER_KEYS = (
    "rauminhaltedertiefkuehlfaecher", "rauminhaltdertiefkuehlfaecher",
    "kapazitaetfrieren", "gefrierteil", "gefrierfach", "tiefkuehlfach", "freezer",
)
REF_CONTEXT_LABELS = (
    "Modellbezeichnung",
    "Gesamtrauminhalt",
    "Gesamtnutzinhalt",
    "Nutzinhalt",
    "Rauminhalte der K\u00fchlf\u00e4cher",
    "Rauminhalt der K\u00fchlf\u00e4cher",
    "Rauminhalte der Tiefk\u00fchlf\u00e4cher",
    "Rauminhalt der Tiefk\u00fchlf\u00e4cher",
    "Kapazit\u00e4t K\u00fchlen",
    "Kapazit\u00e4t Frieren",
)


def _label_key(value: str | None) -> str:
    text = (value or "").casefold().translate(_LABEL_TRANSLATION)
    return re.sub(r"[^a-z0-9]+", "", text)


def _label_matches(value: str | None, keys: tuple[str, ...], reject_keys: tuple[str, ...] = ()) -> bool:
    key = _label_key(value)
    if any(k in key for k in reject_keys):
        return False
    return any(k in key for k in keys)


def _liter_number(value: str | None) -> float | None:
    if not value:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d+)?)", str(value))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _format_liters(value: float | int | None) -> str | None:
    if value is None or value <= 0:
        return None
    rounded = round(float(value), 3)
    if abs(rounded - round(rounded)) < 0.001:
        return f"{int(round(rounded))} l"
    return f"{str(rounded).rstrip('0').rstrip('.')} l"


def _sum_liters(left: str | None, right: str | None) -> str | None:
    a = _liter_number(left)
    b = _liter_number(right)
    if a is None or b is None:
        return None
    return _format_liters(a + b)


def _nearest_label_kind(text: str, start: int, end: int) -> str | None:
    before = text[max(0, start - 30):start]
    after = text[end:end + 30]
    candidates: list[tuple[int, int, str]] = []
    for kind, pattern in (("total", _NAME_TOTAL), ("cooling", _NAME_COOLING), ("freezer", _NAME_FREEZER)):
        for m in pattern.finditer(before):
            candidates.append((len(before) - m.end(), 1, kind))
        m = pattern.search(after)
        if m:
            candidates.append((m.start(), 0, kind))
    return min(candidates)[2] if candidates else None


def _capacity_from_name(name: str | None) -> str | None:
    """Return total volume from the title; sum cooling+freezer when both are labelled."""
    text = name or ""
    matches = list(_NAME_LITER.finditer(text))
    if not matches:
        return None
    by_kind: dict[str, list[float]] = {"total": [], "cooling": [], "freezer": [], "unknown": []}
    for match in matches:
        value = _liter_number(match.group(0))
        if value is None:
            continue
        kind = _nearest_label_kind(text, match.start(), match.end()) or "unknown"
        by_kind[kind].append(value)
    if by_kind["total"]:
        return _format_liters(by_kind["total"][0])
    if by_kind["cooling"] and by_kind["freezer"]:
        return _format_liters(by_kind["cooling"][0] + by_kind["freezer"][0])
    if len(matches) == 1:
        for values in by_kind.values():
            if values:
                return _format_liters(values[0])
    return None


def _top_info_map(target: dict[str, Any]) -> dict[str, str]:
    raw = target.get("top_infos") or target.get("top_info") or ""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v not in (None, "")}
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if v not in (None, "")}
    out: dict[str, str] = {}
    for part in re.split(r"\s*\|\|\|\s*|\n|;", text):
        if ":" in part:
            key, value = part.split(":", 1)
            key = parsers.text_clean(key)
            value = parsers.text_clean(value)
            if key and value:
                out[key] = value
    return out


def _ctx_values(target: dict[str, Any], ctx: dict[str, Any] | None) -> dict[str, str]:
    pid = str(target.get("product_id") or "")
    values = ((ctx or {}).get("model", {}) or {}).get(pid, {})
    return values if isinstance(values, dict) else {}


def _value_by_label(values: dict[str, Any], keys: tuple[str, ...],
                    reject_keys: tuple[str, ...] = ()) -> str | None:
    for label, value in values.items():
        if _label_matches(str(label), keys, reject_keys) and model_sku.has_value(value):
            formatted = _format_liters(_liter_number(str(value)))
            return formatted or str(value)
    return None


def _value_by_exact_label(values: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for label, value in values.items():
        if _label_key(str(label)) in keys and model_sku.has_value(value):
            return _format_liters(_liter_number(str(value)))
    return None

def _capacity_sum_by_labels(values: dict[str, Any]) -> str | None:
    cooling = _value_by_label(values, _COOLING_KEYS, reject_keys=_FREEZER_KEYS)
    freezer = _value_by_label(values, _FREEZER_KEYS)
    return _sum_liters(cooling, freezer)



def _single_compartment_capacity(values: dict[str, Any], compartment_kind: str | None) -> str | None:
    cooling = _value_by_label(values, _COOLING_KEYS, reject_keys=_FREEZER_KEYS)
    freezer = _value_by_label(values, _FREEZER_KEYS)
    if compartment_kind == "cooling":
        return None if freezer else cooling
    if compartment_kind == "freezer":
        return None if cooling else freezer
    return None


def _datasheet_sum_capacity(ds: dict[str, Any]) -> str | None:
    cooling = (
        datasheet.value_with_unit(ds, "Rauminhalte der K\u00fchlf\u00e4cher", "l")
        or datasheet.value_with_unit(ds, "Rauminhalt der K\u00fchlf\u00e4cher", "l")
    )
    freezer = (
        datasheet.value_with_unit(ds, "Rauminhalte der Tiefk\u00fchlf\u00e4cher", "l")
        or datasheet.value_with_unit(ds, "Rauminhalt der Tiefk\u00fchlf\u00e4cher", "l")
    )
    return _sum_liters(cooling, freezer)


def _eprel_capacity(sku: str | None) -> str | None:
    try:
        return eprel.fridge_total_volume(sku, timeout=30)
    except Exception:
        return None


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None,
                 sku: str | None = None) -> dict[str, Any]:
    # ref_capacity is total volume. If OTTO exposes only compartment values,
    # collect cooling + freezer together. For pure refrigerators, a lone cooling
    # compartment is the total usable volume; do not apply that to fridge-freezers.
    top_infos = _top_info_map(target)
    ctx_values = _ctx_values(target, ctx)
    raw_type_hint = target.get("retailer_sku_name")
    ref_type = translate_ref_type(raw_type_hint)
    compartment_kind = _single_compartment_kind(raw_type_hint)
    capacity = None
    for source in (
        lambda: _capacity_from_name(target.get("retailer_sku_name")),
        lambda: _capacity_from_name(target.get("product_url")),
        lambda: datasheet.value_with_unit(ds, "Gesamtrauminhalt", "l"),
        lambda: datasheet.value_with_unit(ds, "Gesamtnutzinhalt", "l"),
        lambda: _datasheet_sum_capacity(ds),
        lambda: _value_by_label(top_infos, _TOTAL_KEYS),
        lambda: _value_by_label(ctx_values, _TOTAL_KEYS),
        lambda: _value_by_exact_label(top_infos, _STANDALONE_TOTAL_KEYS),
        lambda: _value_by_exact_label(ctx_values, _STANDALONE_TOTAL_KEYS),
        lambda: _capacity_sum_by_labels(top_infos),
        lambda: _capacity_sum_by_labels(ctx_values),
        lambda: _single_compartment_capacity(top_infos, compartment_kind),
        lambda: _single_compartment_capacity(ctx_values, compartment_kind),
        lambda: _eprel_capacity(sku),
    ):
        value = source()
        if model_sku.has_value(value):
            capacity = value
            break
    spec = {"ref_refrigerator_type": ref_type, "ref_capacity": capacity}
    if ref_type is None and _is_policy_null_ref_type(raw_type_hint):
        spec["_policy_null_fields"] = ["ref_refrigerator_type"]
    return spec


def prepare_context(targets=None) -> dict[str, Any]:
    # /vergleich/ Modellbezeichnung (sku fallback) + Gesamtrauminhalt (capacity for beverage
    # coolers the datasheet/structured comparison page miss), on current bestVariationIds.
    # NOTE: we deliberately do NOT force a capacity re-fetch here (model_context supports
    # required_any). ~70 household fridges legitimately lack a /vergleich/ volume label (their
    # capacity comes from the datasheet), so retrying the whole capacity-missing set would add
    # ~50% more /vergleich/ requests every run and risk throttling the sku/Modellbezeichnung
    # harvest that already works — a bad trade for the rare commercial-cooler cell drop.
    return model_sku.model_context(targets, (SUCHBEGRIFF, "getraenkekuehlschrank"),
                                   labels=REF_CONTEXT_LABELS)


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    return model_sku.model_sku(target, ctx)


def extract_pdp_spec(soup) -> dict[str, Any]:
    rows = parsers.all_characteristics(soup)
    raw = parsers.characteristic_by_label(
        soup,
        "K\u00fchlschranktyp", "Ger\u00e4tetyp", "Ger\u00e4teart", "Bauart", "Produktart", "Typ",
    )
    out: dict[str, Any] = {}
    if raw:
        out["ref_refrigerator_type"] = translate_ref_type(raw)
    compartment_kind = _single_compartment_kind(raw)
    capacity = (
        _value_by_label(rows, _TOTAL_KEYS)
        or _value_by_exact_label(rows, _STANDALONE_TOTAL_KEYS)
        or _capacity_sum_by_labels(rows)
        or _single_compartment_capacity(rows, compartment_kind)
    )
    if capacity:
        out["ref_capacity"] = capacity
    return out
