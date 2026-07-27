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
from common.parsers import PRIMARY_SPEC_EXPECTED_NULL, text_clean

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
PERSIST_PRIMARY_SPEC_EXPECTED_NULL = True


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


_TYPE_CHAR_TRANSLATION = str.maketrans({
    "\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss",
})


def _type_key(value: str | None) -> str:
    """Comparison key insensitive to German umlauts, spaces and punctuation."""
    text = (text_clean(value) or "").casefold()
    text = text.translate(_TYPE_CHAR_TRANSLATION)
    return re.sub(r"[^a-z0-9]+", "", text)


_REF_TYPE_EXCLUDE_TOKENS = (
    "gefrierschrank", "gefriertruhe",
    "getraenkekuehlschrank", "getraenkekuehler",
    "beveragecooler", "beveragerefrigerator",
    "fleischreifeschrank", "meatagingcabinet",
    "kuehlvitrine", "displayrefrigerator", "refrigerateddisplaycase",
    "kuehlbox", "coolerbox", "coolbox",
)
_REF_TYPE_EXCLUDE_EXACT = {"freezer", "chestfreezer", "uprightfreezer"}


def _excluded_type(value: str | None) -> bool:
    key = _type_key(value)
    if not key:
        return False
    if key in _REF_TYPE_EXCLUDE_EXACT:
        return True
    if any(token in key for token in _REF_TYPE_EXCLUDE_TOKENS):
        return True
    # A branded/title-form standalone freezer is still a freezer. Preserve the
    # two accepted refrigerator expressions that legitimately contain the word.
    return "freezer" in key and not any(
        token in key for token in ("fridgefreezer", "freezercompartment")
    )


def _translate_type(value: str | None) -> str | None:
    """Return one English refrigerator form, with specific forms taking priority."""
    key = _type_key(value)
    if not key or _excluded_type(value):
        return None

    # Door/layout forms outrank generic fridge-freezer words in compound values.
    if "frenchdoor" in key:
        return "French Door"
    if "sidebyside" in key:
        return "Side-by-Side"

    if "minikuehlschrank" in key or "minifridge" in key:
        return "Mini fridge"
    if "kuehlschrankmitgefrierfach" in key or "refrigeratorwithfreezercompartment" in key:
        return "Refrigerator with freezer compartment"
    if "kuehlschrankmitkaltlagerfach" in key or "refrigeratorwithchillcompartment" in key:
        return "Refrigerator with chill compartment"
    if "einbaukuehlschrank" in key or "builtinrefrigerator" in key or "integratedrefrigerator" in key:
        return "Built-in refrigerator"
    if "weinkuehlschrank" in key or "winefridge" in key or "winerefrigerator" in key:
        return "Wine fridge"

    if any(token in key for token in (
        "kuehlgefrierkombination",
        "kuehlundgefrierkombination",
        "fridgefreezercombination",
    )):
        return "Fridge-freezer combination"

    if "vollraumkuehlschrank" in key or "stehendervorratsschrank" in key:
        return "Refrigerator"
    if "kuehlschrank" in key or "refrigerator" in key:
        return "Refrigerator"
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
    excluded = _excluded_type(name) or _excluded_type(raw_feature_type)
    if excluded:
        typ = None
    else:
        typ = (_translate_type(name)
               or _translate_type(raw_feature_type))
    result = {
        "ref_refrigerator_type": typ,
        "ref_capacity": _capacity_from_name(name) or _ref_capacity(features),
    }
    # A non-empty but excluded/unknown source type is a deliberate policy NULL,
    # not a failed parse. Keep that distinction in the internal detail row so
    # step02 does not issue an unnecessary SSR request or refetch it on resume.
    if typ is None and (excluded or raw_feature_type):
        result[PRIMARY_SPEC_EXPECTED_NULL] = True
    return result
