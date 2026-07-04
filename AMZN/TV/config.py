"""Amazon.de SEG TV category config."""
from __future__ import annotations

from common.io_util import ACCOUNT_NAME as _ACCOUNT_NAME, COUNTRY as _COUNTRY, env_value

PRODUCT = "TV"
COUNTRY = _COUNTRY
ACCOUNT_NAME = _ACCOUNT_NAME

MAIN_URL = "https://www.amazon.de/s?k=fernseher&ref=nb_sb_ss"
BSR_URL = "https://www.amazon.de/gp/bestsellers/ce-de/1197292/ref=zg_bs_nav_ce-de_1"
DB_TABLE = env_value("SEG_TV_DB_FINAL_TABLE", "dx_seg.dx_seg_tv_retail_com")
POSTAL_CODE = env_value("AMZN_DE_POSTAL_CODE", "10117")

SPEC_FIELDS = ["screen_size", "model_year", "estimated_annual_electricity_use"]
