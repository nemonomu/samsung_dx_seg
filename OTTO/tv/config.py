"""OTTO SEG TV category config (search term, target filter, spec extraction)."""
from __future__ import annotations

import re
from typing import Any

from common import datasheet, eprel, model_sku
from common.io_util import RETAILER, COUNTRY as _COUNTRY, env_value, top_info, transliterate

PRODUCT = "TV"
COUNTRY = _COUNTRY
ACCOUNT_NAME = RETAILER
SEARCH_TERM = "fernseher"
SUCHBEGRIFF = transliterate(SEARCH_TERM)
WARMUP_LISTING_URL = "https://www.otto.de/suche/fernseher/"
DB_TABLE = env_value("SEG_TV_DB_FINAL_TABLE", "dx_seg.dx_seg_tv_retail_com")

SPEC_FIELDS = ["screen_size", "estimated_annual_electricity_use"]
USE_DATASHEET = True
PDP_SUPPLEMENT_FIELDS: list[str] = []  # summarized_review_content is PDP-only; left blank by default

TV_POSITIVE_KEYWORDS = (
    "fernseher", "smart-tv", "smart tv", "oled-tv", "oled tv", "qled-tv", "qled tv",
    "led-tv", "led tv", "lcd-tv", "lcd tv",
)
TV_PRODUCT_PATTERNS = (
    r"\b(?:mini-led|lcd-led|dled|qled|oled|led|lcd)-fernseher\b",
    r"\b(?:mini-led|lcd-led|dled|qled|oled|led|lcd) fernseher\b",
    r"\b(?:oled|qled|led|lcd)-tv\b",
)
HARD_NON_TV_EXCLUDE_KEYWORDS = (
    "wandhalter", "halterung", "tv-schrank", "fernsehschrank", "schrank", "lowboard",
    "tv-ständer", "tv staender", "tv-staender", "ständer", "staender", "tv-board", "tv board",
    "led stripe", "hintergrundbeleuchtung", "beleuchtung", "projektor", "beamer", "leinwand",
    "monitor", "receiver", "antenne", "kabel", "streaming-stick", "streaming stick",
    "streaming-box", "streaming box", "ci+-modul",
)
ACCESSORY_EXCLUDE_KEYWORDS = ("fernbedienung", "soundbar")
_TV_REGEXES = tuple(re.compile(p) for p in TV_PRODUCT_PATTERNS)


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().casefold() or None


def classify(name: str | None) -> tuple[bool, str]:
    key = _norm(name)
    if not key:
        return False, "missing_retailer_sku_name"
    signature = any(rx.search(key) for rx in _TV_REGEXES)
    hard = [t for t in HARD_NON_TV_EXCLUDE_KEYWORDS if t in key]
    if hard:
        return False, "exclude_keyword:" + ",".join(hard)
    accessory = [t for t in ACCESSORY_EXCLUDE_KEYWORDS if t in key]
    if accessory and not signature:
        return False, "exclude_accessory_keyword:" + ",".join(accessory)
    if signature:
        return True, "tv_product_signature_with_accessory_bundle" if accessory else "tv_product_signature"
    if any(t in key for t in TV_POSITIVE_KEYWORDS):
        return True, "tv_keyword"
    return False, "missing_tv_positive_keyword"


def _watt(value):
    return value if (value and value != "NA") else None


def _screen_from_topinfo(target: dict[str, Any]) -> str | None:
    raw = top_info(target, "Diagonale", "Bildschirm")
    if not raw:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*Zoll", raw)
    return m.group(1).replace(",", ".") if m else None


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None,
                 sku: str | None = None) -> dict[str, Any]:
    screen = datasheet.screen_inches(ds) or _screen_from_topinfo(target)
    # HDR on-mode power only (SDR is not a collection target)
    electricity = _watt(datasheet.power_by_label(ds, hdr=True))
    if not electricity:
        # datasheet PDF was image-only / unparseable -> EU EPREL registry by model (sku)
        electricity = eprel.display_on_mode_power(sku)
    return {"screen_size": screen, "estimated_annual_electricity_use": electricity}


def prepare_context(targets=None) -> dict[str, Any]:
    # /vergleich/ Modellbezeichnung as a sku fallback for space-separated models
    return model_sku.model_context(targets)


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    return model_sku.model_sku(target, ctx)


def extract_pdp_spec(soup) -> dict[str, Any]:
    return {}
