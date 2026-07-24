"""MMKT REF product-line config (Kühlschränke / refrigerators). SEG No.121-138.

REF replaces TV's screen_size/model_year/electricity with:
  ref_refrigerator_type  <- PDP feature "Produkttyp" (translated)
  ref_capacity           <- PDP feature "Rauminhalt der Kühlfächer" (liters)

ref_capacity fallback: ~12% of products have no "Rauminhalt der Kühlfächer"
feature (marketplace mini/beverage fridges). For those the volume lives only in
the PDP description body under varied labels (Nutzinhalt gesamt / Gesamtnutzinhalt
/ Fassungsvermögen / Gesamtvolumen). recover_missing_from_description() pulls it
from there — but that is TOTAL usable volume, not the fridge-compartment volume,
so this fallback is a documented mixed-definition (see customer note 2026-07-11).
"""
from __future__ import annotations

import re
from typing import Any

from common import config as base
from common.parsers import text_clean

PRODUCT = "REF"
ACCOUNT_NAME = base.ACCOUNT_NAME
COUNTRY = base.COUNTRY
PAGE_TYPE = base.PAGE_TYPE

# Kühlschränke (CAT_DE_MM_33) — all refrigerators incl. fridge-freezer combos /
# Side-by-Side / French Door (~1216 products). URLs per user.
LISTING_URL = "https://www.mediamarkt.de/de/category/k%C3%BChlschr%C3%A4nke-33.html?query=K%C3%BChlschr%C3%A4nke"
BSR_URL = LISTING_URL + "&sort=salescount+desc"

MAIN_TARGET_UNIQUE = 300
BSR_TARGET_RANK = 100
OUTPUT_ROOT = base.product_output_root("ref")
DB_TABLE = base.seg_final_table("SEG_REF_DB_FINAL_TABLE", "dx_seg.dx_seg_ref_retail_com")

SPEC_FIELDS = ["ref_refrigerator_type", "ref_capacity"]

# ref_refrigerator_type [수집 후 번역 필요] — refrigerator form by freezer position.
REF_TYPE_TRANSLATIONS = {
    "Kühlschrank": "Refrigerator",
    "Kühlgefrierkombination": "Fridge-freezer combination",   # MediaMarkt's actual value (no hyphen)
    "Kühl-Gefrierkombination": "Fridge-freezer combination",
    "Kühl-Gefrierkombinationen": "Fridge-freezer combination",
    "Side-by-Side": "Side-by-Side",
    "Side by Side": "Side-by-Side",
    "Side-by-Side-Kühlschrank": "Side-by-Side",
    "French Door": "French Door",
    "French-Door-Kühlschrank": "French Door",
    "Gefrierschrank": "Freezer",
    "Gefriertruhe": "Chest freezer",
    "Mini-Kühlschrank": "Mini fridge",
    "Weinkühlschrank": "Wine fridge",
    "Einbaukühlschrank": "Built-in refrigerator",
}


def _norm_liters(raw: str) -> str | None:
    """'193' -> '193L', '9.8' -> '9.8L', '129,0' -> '129L'; None for <=0/unparsable."""
    if raw is None:
        return None
    try:
        val = float(str(raw).replace(",", "."))
    except ValueError:
        raw = str(raw).strip()
        return f"{raw}L" if raw else None
    if val <= 0:
        return None
    return f"{int(val)}L" if val == int(val) else f"{val}L"


def _ref_capacity(features: dict[str, str]) -> str | None:
    """Fridge (cooling) compartment volume — PDP feature 'Rauminhalt der
    Kühlfächer' (per user). Falls back to Gesamtrauminhalt; skips zero/blank
    values (some marketplace listings report 0.0)."""
    for key in ("Rauminhalt der Kühlfächer", "Gesamtrauminhalt"):
        v = _norm_liters(text_clean(features.get(key)))
        if v:
            return v
    return None


_NAME_LITER = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:l|liter)\b", re.I)
_NAME_COOLING = re.compile(r"(?:k(?:ü|ue)hl(?:schrank|teil|raum|fach|bereich|zone|ung)?|fridge|refrigerator|cooling)", re.I)
_NAME_FREEZER = re.compile(r"(?:gefrier(?:fach|teil|raum|bereich|zone)?|freezer)", re.I)


def _capacity_from_name(name: str | None) -> str | None:
    """Use an explicit title capacity, preferring a cooling-labelled value."""
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
                return f"{match.group(1)}L"
    return f"{matches[0].group(1)}L"


def _type_key(value: str | None) -> str:
    text = text_clean(value) or ""
    text = re.sub(r"\s*[-/]\s*", "-", text.casefold())
    text = re.sub(r"-und\s+|\s+und\s+", "-", text)
    return re.sub(r"-+", "-", re.sub(r"\s+", " ", text))


_REF_TYPE_EXCLUDES = (
    "gefrierschrank", "gefriertruhe", "getränkekühlschrank", "getraenkekuehlschrank",
    "getränkekühler", "getraenkekuehler", "fleischreifeschrank", "kühlvitrine",
    "kuehlvitrine", "kühlbox", "kuehlbox", "beverage cooler", "meat aging cabinet",
    "display refrigerator", "cooler box",
)


def _excluded_type(value: str | None) -> bool:
    key = _type_key(value)
    return bool(key) and any(token in key for token in _REF_TYPE_EXCLUDES)


def _translate_type(value: str | None) -> str | None:
    key = _type_key(value)
    if not key or _excluded_type(value):
        return None
    for german, english in sorted(REF_TYPE_TRANSLATIONS.items(), key=lambda item: len(item[0]), reverse=True):
        if _type_key(german) in key:
            return english
    return None


# --- ref_capacity description fallback (marketplace mini/beverage fridges) -------
# Capacity labels in the PDP description body, in priority order. Each captures a
# number (group 1) next to a capacity word; the number-before form is for
# "72 Liter Gesamtvolumen". Validated against the 2026-07-11 missing-36 set.
_DESC_CAP_PATTERNS = (
    r"Nutzinhalt\s*gesamt[^0-9]{0,12}([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:l\b|liter)",
    r"Gesamt[-\s]?[Nn]utzinhalt[^0-9]{0,12}([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:l\b|liter)",
    r"Nutzinhalt(?:\s*von)?(?:\s*insgesamt)?[^0-9]{0,12}([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:l\b|liter)",
    r"([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:l\b|liter)[nrs]?\s*Gesamtvolumen",
    r"Fassungsverm[oö]gen[^0-9]{0,15}([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:l\b|liter)",
    r"([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*liter\s*Volumen",
)
# Reject a hit whose ±30-char context names a non-capacity liter figure
# (door shelf, energy use, wine-bottle size, freezer-only compartment).
_DESC_DECOY = re.compile(
    r"flaschenfach|energieverbrauch|bordeaux|gefrierfach|tiefk|kwh|türfach|schallem",
    re.I,
)


def capacity_from_description(html: str) -> str | None:
    """Pull the total usable volume from a PDP description body's HTML. Returns a
    'NNNL' string, or None if no clear capacity label is present."""
    if not html:
        return None
    txt = (html.replace("\\u003c", "<").replace("\\u003e", ">")
              .replace("\\u002F", "/").replace("\\u002f", "/").replace("\\n", " "))
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    for pat in _DESC_CAP_PATTERNS:
        for m in re.finditer(pat, txt, re.I):
            # Only inspect the immediate surroundings of THIS figure — a small
            # trailing margin catches a decoy word right after the unit
            # ("2 l Flaschenfach") without reaching the NEXT figure's word
            # (".. Gesamtvolumen und 21 l Gefrierfach" must keep the 72).
            ctx = txt[max(0, m.start() - 25):m.end() + 10]
            if _DESC_DECOY.search(ctx):
                continue
            v = _norm_liters(m.group(1))
            if v:
                return v
    return None


def recover_missing_from_description(row: dict[str, Any], fetch_text) -> None:
    """REF-only detail fallback: if ref_capacity is empty (product has no
    'Rauminhalt der Kühlfächer' feature), pull it from the PDP description body.
    `fetch_text` is a lazy thunk returning the PDP HTML — it is only called when
    the field is actually missing, so the ~264 products that already have the
    feature pay no extra request."""
    if (row.get("ref_capacity") or "").strip():
        return
    v = capacity_from_description(fetch_text() or "")
    if v:
        row["ref_capacity"] = v


def extract_pdp_spec(features: dict[str, str], name: str | None = None) -> dict[str, Any]:
    raw_feature_type = text_clean(features.get("Produkttyp"))
    if _excluded_type(name) or _excluded_type(raw_feature_type):
        typ = None
    else:
        typ = (_translate_type(name)
               or _translate_type(raw_feature_type)
               or raw_feature_type)
    return {
        "ref_refrigerator_type": typ,
        "ref_capacity": _capacity_from_name(name) or _ref_capacity(features),
    }
