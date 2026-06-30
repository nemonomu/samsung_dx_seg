"""MMKT TV product-line config (Fernseher). SEG 데이터셋 No.31-49."""
from __future__ import annotations

from common import config as base
from common.parsers import tv_extract_pdp_spec

PRODUCT = "TV"
ACCOUNT_NAME = base.ACCOUNT_NAME
COUNTRY = base.COUNTRY
PAGE_TYPE = base.PAGE_TYPE

# Fernseher category. Main = "Beste Ergebnisse"; BSR = Topseller (salescount desc).
LISTING_URL = "https://www.mediamarkt.de/de/category/fernseher-nach-gr%C3%B6%C3%9Fen-4708.html"
BSR_URL = LISTING_URL + "?sort=salescount+desc"

MAIN_TARGET_UNIQUE = 300
BSR_TARGET_RANK = 100
OUTPUT_ROOT = base.product_output_root("tv")
DB_TABLE = base.seg_final_table("SEG_TV_DB_FINAL_TABLE", "dx_seg.dx_seg_tv_retail_com")

# Product-specific PDP spec columns (No.40-43).
SPEC_FIELDS = ["screen_size", "estimated_annual_electricity_use", "model_year"]
extract_pdp_spec = tv_extract_pdp_spec
