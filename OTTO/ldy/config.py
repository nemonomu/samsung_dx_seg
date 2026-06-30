"""OTTO SEG LDY (washing machine) category config.

ldy_loading_type (Bauart) is collected Kasada-free from the OTTO product-comparison
page (/vergleich/?variationIds=...). That page renders each product's Details
characteristics side by side and is NOT Kasada-protected; every Bauart cell carries
its own data-variation-id, so we read each SKU's *own* value (no positional guessing,
no category inference). We batch the run's target variation_ids in prepare_context and
translate Frontlader -> Front loader, Toplader -> Top-loading (other values -> NULL).
"""
from __future__ import annotations

from typing import Any

from common import compare
from common.io_util import RETAILER, COUNTRY as _COUNTRY, env_value, top_info, transliterate

PRODUCT = "LDY"
COUNTRY = _COUNTRY
ACCOUNT_NAME = RETAILER
SEARCH_TERM = "waschmaschinen"
SUCHBEGRIFF = transliterate(SEARCH_TERM)
WARMUP_LISTING_URL = "https://www.otto.de/suche/waschmaschinen/"
DB_TABLE = env_value("SEG_LDY_DB_FINAL_TABLE", "dx_seg.dx_seg_ldy_retail_com")

SPEC_FIELDS = ["ldy_loading_type", "ldy_capacity"]
USE_DATASHEET = False
PDP_SUPPLEMENT_FIELDS: list[str] = []  # loading_type is Kasada-free via /vergleich/

# Bauart (German) -> ldy_loading_type (English). Other Bauart values (e.g. a
# Waschtrockner that reports neither) translate to NULL.
LOADING_MAP = {"frontlader": "Front loader", "toplader": "Top-loading"}

POSITIVE_KEYWORDS = ("waschmaschine", "waschvollautomat", "waschtrockner", "frontlader", "toplader")
# Hard excludes win over a positive keyword: accessories/toys carry "waschmaschine"
# in their name (e.g. "Waschmaschinenuntergestell") but are not washers. A real
# brand+model washer name never contains these tokens.
EXCLUDE_KEYWORDS = (
    # furniture / stands / housings
    "untergestell", "unterschrank", "sockel", "schrank", "umbau", "gestell",
    "bodenwanne", "rahmen", "zwischenbaurahmen",
    # hoses / connections / plumbing
    "schlauch", "anschluss", "siphon", "aquastop-set", "wasserfilter", "filter",
    # covers / mats / locks / boxes / care  (specific compounds only, no bare "matte")
    "abdeckung", "bezug", "antirutsch", "rutschmatte", "kabelschloss",
    "steckerschloss", "transportbehälter", "pulverbox", "perle", "parfüm",
    # spin dryers / wash basins / toys / generic accessories
    # (use specific compounds, not bare "schleuder", to never hit a real washer model)
    "wäscheschleuder", "schleuderfunktion", "waschschüssel",
    "spielzeug", "spielküche", "lernspiel",
    "griff", "türgriff", "zubehör", "ersatzteil", "ersatz",
)


def _norm(value: str | None) -> str | None:
    import re
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip().casefold() or None


def classify(name: str | None) -> tuple[bool, str]:
    key = _norm(name)
    if not key:
        return False, "missing_retailer_sku_name"
    # hard excludes win, even when a positive keyword is also present
    # (e.g. "Waschmaschinenuntergestell" -> excluded)
    hits = [t for t in EXCLUDE_KEYWORDS if t in key]
    if hits:
        return False, "exclude_keyword:" + ",".join(hits)
    if any(t in key for t in POSITIVE_KEYWORDS):
        return True, "ldy_keyword"
    return False, "missing_ldy_keyword"


# OTTO "no data" placeholders that look like a value but aren't one
_BLANK_VALUES = {"", "-", "--", "—", "–", "k.a.", "n/a", "keine angabe"}


def _has_value(raw: str | None) -> bool:
    return bool(raw) and (raw.strip().casefold() not in _BLANK_VALUES)


def _loading_from_text(text: str | None) -> str | None:
    """Map a German Frontlader/Toplader mention to English; None if neither present."""
    key = _norm(text)
    if not key:
        return None
    for german, english in LOADING_MAP.items():
        if german in key:
            return english
    return None


def resolve_loading(bauart: str | None, name: str | None) -> str | None:
    """Structured Bauart is authoritative:
      - Frontlader/Toplader  -> Front loader / Top-loading
      - any other value (e.g. freistehend, unterbaufähig) -> kept as-is
    When Bauart is blank, fall back to the listing subtitle (name) for Frontlader/Toplader.
    """
    raw = (bauart or "").strip()
    if _has_value(raw):
        return _loading_from_text(raw) or raw
    return _loading_from_text(name)


def prepare_context(targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build {variation_id: Bauart} (+ subtitle fallback names) for the run's targets,
    via the Kasada-free /vergleich/ comparison page."""
    vids = []
    for t in (targets or []):
        vid = str(t.get("variation_id") or "").strip()
        if vid and vid not in vids:
            vids.append(vid)
    bauart = compare.characteristic_map(vids, "Bauart") if vids else {}
    # subtitle fallback only for SKUs whose structured Bauart came back blank
    # (empty, or an OTTO "no data" placeholder like "-")
    missing = [v for v in vids if not _has_value(bauart.get(v))]
    names = compare.name_map(missing) if missing else {}
    labeled = sum(1 for v in bauart.values() if (v or "").strip())
    print(f"[ldy] Bauart via /vergleich/: {labeled}/{len(vids)} structured; {len(names)} subtitle fallbacks", flush=True)
    return {"bauart": bauart, "name": names}


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = ctx or {}
    vid = str(target.get("variation_id") or "")
    loading = resolve_loading(ctx.get("bauart", {}).get(vid), ctx.get("name", {}).get(vid))
    capacity = top_info(target, "Kapazität Waschen", "Füllmenge", "Fassungsvermögen")
    return {"ldy_loading_type": loading, "ldy_capacity": capacity}


def extract_pdp_spec(soup) -> dict[str, Any]:
    return {}
