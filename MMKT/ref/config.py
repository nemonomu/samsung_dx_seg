"""MMKT REF product-line config (Kühlschränke / refrigerators). SEG No.121-138.

REF replaces TV's screen_size/model_year/electricity with:
  ref_refrigerator_type  <- PDP feature "Produkttyp" (translated)
  ref_capacity           <- PDP feature "Gesamtrauminhalt" (liters)
"""
from __future__ import annotations

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


def _ref_capacity(features: dict[str, str]) -> str | None:
    raw = text_clean(features.get("Gesamtrauminhalt"))
    if raw:
        return f"{raw}L"
    # Marketplace products (reduced feature set) lack Gesamtrauminhalt — sum the
    # fridge + freezer compartment volumes instead.
    total = 0.0
    found = False
    for key in ("Rauminhalt der Kühlfächer", "Rauminhalt der Tiefkühlfächer"):
        v = text_clean(features.get(key))
        if v:
            try:
                total += float(v.replace(",", "."))
                found = True
            except ValueError:
                pass
    if not found:
        return None
    return f"{int(total)}L" if abs(total - int(total)) < 0.05 else f"{total}L"


def extract_pdp_spec(features: dict[str, str]) -> dict[str, Any]:
    typ = text_clean(features.get("Produkttyp"))
    return {
        "ref_refrigerator_type": REF_TYPE_TRANSLATIONS.get(typ, typ) if typ else None,
        "ref_capacity": _ref_capacity(features),
    }
