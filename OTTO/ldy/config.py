"""OTTO SEG LDY (washing machine) category config.

ldy_loading_type (Bauart) is collected Kasada-free from the OTTO product-comparison
page (/vergleich/?variationIds=...). That page renders each product's Details
characteristics side by side and is NOT Kasada-protected; every Bauart cell carries
its own data-variation-id, so we read each SKU's *own* value (no positional guessing,
no category inference). We batch the run's target variation_ids in prepare_context and
translate Frontlader -> Front loader, Toplader -> Top-loading (other values -> NULL).
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any
from urllib.parse import urlencode

from common import compare
from common.io_util import RETAILER, COUNTRY as _COUNTRY, env_value, top_info, transliterate

EVERGLADES_URL = "https://www.otto.de/everglades/products"
_EVER_HDR = {
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": "https://www.otto.de/suche/waschmaschinen/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}

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
    # anti-vibration mats / pads
    "gummimatte", "antivibration", "anti-vibration", "vibrationsdämpfer",
    "schwingungsdämpfer", "dämpfer", "waschschutz",
    # spin dryers / wash basins / toys / generic accessories
    # (use specific compounds, not bare "schleuder", to never hit a real washer model)
    "wäscheschleuder", "schleuderfunktion", "waschschüssel",
    "spielzeug", "spielküche", "lernspiel", "kinder-waschmaschine", "kinderwaschmaschine",
    "ballonmütze", "mütze",
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


# /vergleich/ characteristic labels we read per SKU (Kasada-free). Capacity is the wash
# capacity; prefer the "(Waschen)" variant (washer-dryers also list a drying capacity).
VERGLEICH_LABELS = ["Bauart", "Modellbezeichnung",
                    "Nennkapazität (Waschen)", "Fassungsvermögen", "Nennkapazität"]
_CAPACITY_LABELS = ["Nennkapazität (Waschen)", "Fassungsvermögen", "Nennkapazität"]
_COLOR_SUFFIX = re.compile(r"\s+(weiss|weiß|schwarz|grau|silber|anthrazit|edelstahl|inox|titan)\s*$", re.I)


def _capacity_from_name(name: str | None) -> str | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*kg", name or "", re.I)
    return f"{m.group(1)} kg" if m else None


def _capacity_from_datasheet(ds: dict[str, Any] | None) -> str | None:
    """EU energy datasheet 'Nennkapazität(a) 9,0 ... (kg)' — authoritative rated wash
    capacity, used when no listing/name/comparison source has it."""
    text = (ds or {}).get("text") or ""
    m = re.search(r"Nennkapazit[äa]t\D*?(\d+(?:[.,]\d+)?)", text)
    return f"{m.group(1)} kg" if m else None


def _ever_fetch(rule: str, offset: int, timeout: int = 45, attempts: int = 3) -> dict | None:
    url = EVERGLADES_URL + "?" + urlencode([("rule", rule), ("intents", "ranked"), ("ranked.offset", str(offset))])
    for attempt in range(attempts):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=_EVER_HDR), timeout=timeout).read())
        except Exception:
            time.sleep(1.5)
    return None


def _category_vids(cat: str, hard_cap: int = 4000) -> set[str]:
    """All bestVariationIds in the waschmaschinen>{cat} subcategory (Kasada-free everglades).
    The loading_type fallback when OTTO ships no Bauart. Pages are retried so a transient
    failure does not silently truncate the set (an incomplete set = missed frontloaders)."""
    vids: set[str] = set()
    offset = 0
    total = None
    consecutive_fail = 0
    rule = f"(und.(sind.kategorien.waschmaschinen.{cat}).(suchbegriff.waschmaschinen).(~.(v.1)))"
    while offset < hard_cap:
        data = _ever_fetch(rule, offset)
        if data is None:
            consecutive_fail += 1
            if consecutive_fail >= 3:
                break  # give up rather than loop forever; partial set still helps
            continue  # retry the same offset
        consecutive_fail = 0
        intent = next((it for it in data.get("intents", []) if it.get("intent") == "ranked"), {})
        products = intent.get("products", []) or []
        if total is None:
            total = intent.get("count")
        if not products:
            break
        for p in products:
            vid = p.get("bestVariationId") or p.get("id")
            if vid:
                vids.add(str(vid))
        offset += len(products)
        if total and len(vids) >= total:
            break
    return vids


def prepare_context(targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per-SKU {Bauart, Modellbezeichnung, capacity} from the Kasada-free /vergleich/ page,
    plus listing subtitles, plus everglades frontlader/toplader category sets — the
    loading_type fallback for washers OTTO ships with no Bauart."""
    vids = []
    for t in (targets or []):
        vid = str(t.get("variation_id") or "").strip()
        if vid and vid not in vids:
            vids.append(vid)
    chars = compare.characteristics_map(vids, VERGLEICH_LABELS) if vids else {}
    bauart = {v: chars.get(v, {}).get("Bauart") for v in vids}
    # full product name (with subtitle) comes from the same /vergleich/ page; it is the
    # Bauart fallback and the capacity ("N kg") fallback.
    names = {v: chars.get(v, {}).get(compare.NAME_KEY) for v in vids}
    frontlader = _category_vids("frontlader")
    toplader = _category_vids("toplader")
    # capacity gap recovery: for the few washers with no listing/name/comparison capacity,
    # read the EU energy datasheet (Nennkapazität). Fetched only for the gaps.
    ds_capacity: dict[str, str] = {}
    for t in (targets or []):
        vid = str(t.get("variation_id") or "").strip()
        if not vid:
            continue
        if _has_value(top_info(t, "Kapazität Waschen", "Füllmenge", "Fassungsvermögen")):
            continue
        if _capacity_from_name(names.get(vid)) or _capacity_from_name(t.get("retailer_sku_name")):
            continue
        if _vergleich_capacity(chars.get(vid, {})):
            continue
        uri = (t.get("energy_datasheet_uri") or "").strip()
        if not uri:
            continue
        from common import datasheet
        body, _st, _ = datasheet.fetch_datasheet_bytes(uri, 45)
        cap = _capacity_from_datasheet(datasheet.parse(body))
        if cap:
            ds_capacity[vid] = cap
    labeled = sum(1 for v in bauart.values() if _has_value(v))
    rendered = sum(1 for v in vids if chars.get(v))
    print(f"[ldy] /vergleich/: {rendered}/{len(vids)} columns; Bauart {labeled} structured; "
          f"category sets front={len(frontlader)} top={len(toplader)}; datasheet capacity {len(ds_capacity)}", flush=True)
    return {"chars": chars, "bauart": bauart, "name": names,
            "frontlader": frontlader, "toplader": toplader, "ds_capacity": ds_capacity}


def _vergleich_capacity(vid_chars: dict[str, str | None]) -> str | None:
    for label in _CAPACITY_LABELS:
        if _has_value(vid_chars.get(label)):
            return vid_chars[label]
    return None


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = ctx or {}
    vid = str(target.get("variation_id") or "")
    vid_chars = ctx.get("chars", {}).get(vid, {})
    name = ctx.get("name", {}).get(vid)
    loading = resolve_loading(ctx.get("bauart", {}).get(vid), name)
    if loading is None:
        # OTTO ships some frontloaders with no Bauart/name hint; fall back to its own
        # frontlader/toplader subcategory membership (real data, not a guess).
        if vid in ctx.get("toplader", set()):
            loading = LOADING_MAP["toplader"]
        elif vid in ctx.get("frontlader", set()):
            loading = LOADING_MAP["frontlader"]
    # capacity: listing top_info, then the product name's "N kg" (reliable, same page),
    # then the /vergleich/ capacity labels as a last resort (batched labels are flaky).
    capacity = (top_info(target, "Kapazität Waschen", "Füllmenge", "Fassungsvermögen")
                or _capacity_from_name(name)
                or _capacity_from_name(target.get("retailer_sku_name"))
                or _vergleich_capacity(vid_chars)
                or ctx.get("ds_capacity", {}).get(vid))
    return {"ldy_loading_type": loading, "ldy_capacity": capacity}


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    """LDY has no datasheet; use the /vergleich/ Modellbezeichnung (handles space-separated
    models like 'BPW 814 A' that the name-token heuristic misses)."""
    ctx = ctx or {}
    vid = str(target.get("variation_id") or "")
    model = ctx.get("chars", {}).get(vid, {}).get("Modellbezeichnung")
    if not _has_value(model):
        return None
    return _COLOR_SUFFIX.sub("", model.strip()).strip() or None


def extract_pdp_spec(soup) -> dict[str, Any]:
    return {}
