"""MMKT LDY product-line config (Waschmaschinen / washing machines). SEG No.211-228.

LDY replaces TV's screen_size/model_year/electricity with:
  ldy_loading_type  <- PDP feature "Beladung" (Frontlader/Toplader, translated)
  ldy_capacity      <- PDP feature "Füllmenge Baumwolle (Waschen)" (kg)
"""
from __future__ import annotations

from typing import Any

from common import config as base
from common.parsers import text_clean

PRODUCT = "LDY"
ACCOUNT_NAME = base.ACCOUNT_NAME
COUNTRY = base.COUNTRY
PAGE_TYPE = base.PAGE_TYPE

# Waschmaschinen (CAT_DE_MM_3) — all washing machines (~651; Frontlader + Toplader
# + Einbau + small). Same URL pattern as REF (?query + &page=N / &sort=).
LISTING_URL = "https://www.mediamarkt.de/de/category/waschmaschinen-3.html?query=Waschmaschinen"
BSR_URL = LISTING_URL + "&sort=salescount+desc"

MAIN_TARGET_UNIQUE = 300
BSR_TARGET_RANK = 100
OUTPUT_ROOT = base.product_output_root("ldy")
DB_TABLE = base.seg_final_table("SEG_LDY_DB_FINAL_TABLE", "dx_seg.dx_seg_ldy_retail_com")

SPEC_FIELDS = ["ldy_loading_type", "ldy_capacity"]

# ldy_loading_type [수집 후 번역 필요] — load position.
LDY_LOADING_TRANSLATIONS = {
    "Frontlader": "Front load",
    "Toplader": "Top load",
}

# Nominal cotton-wash capacity (kg) — confirmed feature name across products.
CAPACITY_FEATURE = "Füllmenge Baumwolle (Waschen)"


def extract_pdp_spec(features: dict[str, str]) -> dict[str, Any]:
    load = text_clean(features.get("Beladung"))
    cap = text_clean(features.get(CAPACITY_FEATURE))
    return {
        "ldy_loading_type": LDY_LOADING_TRANSLATIONS.get(load, load) if load else None,
        "ldy_capacity": f"{cap}kg" if cap else None,
    }
