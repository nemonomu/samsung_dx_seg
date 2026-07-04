"""Shared Amazon crawler constants."""
from __future__ import annotations

from datetime import datetime

from common.io_util import ACCOUNT_NAME, COUNTRY, RETAILER, env_value

AMAZON_BASE = "https://www.amazon.de"
PAGE_TYPE = "main"
DEFAULT_TIMEOUT = int(env_value("AMZN_TIMEOUT", "45") or "45")
DEFAULT_SLEEP = float(env_value("AMZN_SLEEP", "1.5") or "1.5")
LISTING_TARGET = int(env_value("AMZN_MAIN_TARGET", "300") or "300")
BSR_TARGET = int(env_value("AMZN_BSR_TARGET", "100") or "100")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


def run_meta(prefix: str = "a") -> dict[str, str]:
    now = datetime.now()
    return {
        "crawl_strdatetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "calendar_week": "w" + str(now.isocalendar().week),
        "batch_id": prefix + "_" + now.strftime("%Y%m%d_%H%M%S"),
    }
