"""Amazon.de SEG REF category config."""
from __future__ import annotations

from common.io_util import ACCOUNT_NAME as _ACCOUNT_NAME, COUNTRY as _COUNTRY, env_value

PRODUCT = "REF"
COUNTRY = _COUNTRY
ACCOUNT_NAME = _ACCOUNT_NAME

MAIN_URL = "https://www.amazon.de/s?k=K%C3%BChlschr%C3%A4nke&ref=nb_sb_ss"
BSR_URL = "https://www.amazon.de/gp/bestsellers/appliances/16075791/ref=zg_bs_nav_appliances_3_16231641"
DB_TABLE = env_value("SEG_REF_DB_FINAL_TABLE", "dx_seg.dx_seg_ref_retail_com")
POSTAL_CODE = env_value("AMZN_DE_POSTAL_CODE", "10117")

SPEC_FIELDS = ["ref_refrigerator_type", "ref_capacity"]
