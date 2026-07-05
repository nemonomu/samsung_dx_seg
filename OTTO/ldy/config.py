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

from common import compare, eprel
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
    # laundry balls / lint removers (not a washing machine)
    "wäschekugel", "waschkugel", "fusselball", "fusselbälle", "tierhaarentferner",
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


def resolve_loading(beladung: str | None, bauart: str | None, name: str | None) -> str | None:
    """loading_type priority (customer spec):
      1) Beladung (Frontlader/Toplader) — the actual load position
      2) a Frontlader/Toplader mention in the product name/subtitle
      3) Bauart — Frontlader/Toplader translated; ANY other value (freistehend,
         unterbaufähig, ...) kept as-is (customer collects the Bauart field verbatim)
    None -> caller falls back to everglades frontlader/toplader category membership.
    """
    v = _loading_from_text(beladung) or _loading_from_text(name)
    if v:
        return v
    if _has_value(bauart):
        return _loading_from_text(bauart) or bauart.strip()
    return None


# /vergleich/ characteristic labels we read per SKU (Kasada-free). Beladung (load position)
# is preferred for loading_type; capacity is the WASH capacity — explicit "(Waschen)" labels
# first so a washer-dryer's drying capacity is never used.
VERGLEICH_LABELS = ["Beladung", "Bauart", "Modellbezeichnung",
                    "Füllmenge Baumwolle (Waschen)", "Nennkapazität (Waschen)",
                    "Fassungsvermögen", "Nennkapazität"]
_CAPACITY_WASCHEN = ["Füllmenge Baumwolle (Waschen)", "Nennkapazität (Waschen)"]  # explicit wash
_CAPACITY_GENERIC = ["Fassungsvermögen", "Nennkapazität"]  # single-capacity plain washers only
_COLOR_SUFFIX = re.compile(r"\s+(weiss|weiß|schwarz|grau|silber|anthrazit|edelstahl|inox|titan)\s*$", re.I)


def _capacity_from_name(name: str | None) -> str | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*kg", name or "", re.I)
    return f"{m.group(1)} kg" if m else None


def _model_from_name(name: str | None) -> str | None:
    """The model that follows the washer product-type noun in the OTTO name, e.g.
    'Camry Mini-Waschmaschine CR 8052' -> 'CR 8052', 'BAUKNECHT Waschmaschine WAM 914 A'
    -> 'WAM 914 A'. Used when /vergleich/ omits Modellbezeichnung for this product."""
    m = re.search(r"wasch(?:maschine|vollautomat|trockner)\s+(.+?)\s*(?:,|$)", name or "", re.I)
    if not m:
        return None
    model = _COLOR_SUFFIX.sub("", re.sub(r"\s+", " ", m.group(1)).strip()).strip()
    return model or None


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


def _pid_vid_map(hard_cap: int = 4000) -> dict[str, str]:
    """{product_id: current bestVariationId} from the everglades waschmaschinen listing.
    Target variation_ids captured earlier go stale (OTTO switches bestVariationId), and
    /vergleich/ + category membership only resolve with the CURRENT id."""
    out: dict[str, str] = {}
    offset = 0
    total = None
    fails = 0
    rule = "(und.(suchbegriff.waschmaschinen).(~.(v.1)))"
    while offset < hard_cap:
        data = _ever_fetch(rule, offset)
        if data is None:
            fails += 1
            if fails >= 3:
                break
            continue
        fails = 0
        intent = next((it for it in data.get("intents", []) if it.get("intent") == "ranked"), {})
        products = intent.get("products", []) or []
        if total is None:
            total = intent.get("count")
        if not products:
            break
        for p in products:
            pid = str(p.get("id") or "")
            vid = p.get("bestVariationId") or p.get("id")
            if pid and vid:
                out.setdefault(pid, str(vid))
        offset += len(products)
        if total and offset >= total:
            break
    return out


def prepare_context(targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per-SKU {Bauart, Modellbezeichnung, capacity} from the Kasada-free /vergleich/ page,
    plus listing subtitles + everglades frontlader/toplader category membership. Everything
    is keyed by product_id and queried with the CURRENT bestVariationId (stored target vids
    drift and then /vergleich/ returns an empty column)."""
    targets = targets or []
    pid_vid = _pid_vid_map()
    # (product_id, current bestVariationId) — fresh preferred, stored as fallback
    q: list[tuple[str, str]] = []
    seen: set[str] = set()
    for t in targets:
        pid = str(t.get("product_id") or "").strip()
        vid = pid_vid.get(pid) or str(t.get("variation_id") or "").strip()
        if pid and vid and pid not in seen:
            seen.add(pid)
            q.append((pid, vid))
    query_vids = [vid for _, vid in q]
    # Bauart is the key loading-type signal; require it so batch-dropped cells are re-fetched
    chars_by_vid = compare.characteristics_map(query_vids, VERGLEICH_LABELS, required=["Bauart"]) if query_vids else {}
    frontlader = _category_vids("frontlader")
    toplader = _category_vids("toplader")

    chars: dict[str, dict[str, str | None]] = {}
    bauart: dict[str, str | None] = {}
    names: dict[str, str | None] = {}
    is_front: dict[str, bool] = {}
    is_top: dict[str, bool] = {}
    for pid, vid in q:
        c = chars_by_vid.get(vid, {})
        chars[pid] = c
        bauart[pid] = c.get("Bauart")
        names[pid] = c.get(compare.NAME_KEY)
        is_top[pid] = vid in toplader
        is_front[pid] = vid in frontlader

    # capacity gap recovery via the EU energy datasheet (Nennkapazität), only for gaps
    ds_capacity: dict[str, str] = {}
    for t in targets:
        pid = str(t.get("product_id") or "").strip()
        if not pid or pid in ds_capacity:
            continue
        if _has_value(top_info(t, "Kapazität Waschen", "Füllmenge", "Fassungsvermögen")):
            continue
        if _capacity_from_name(names.get(pid)) or _capacity_from_name(t.get("retailer_sku_name")):
            continue
        if _vergleich_capacity(chars.get(pid, {})):
            continue
        uri = (t.get("energy_datasheet_uri") or "").strip()
        if not uri:
            continue
        from common import datasheet
        body, _st, _ = datasheet.fetch_datasheet_bytes(uri, 45)
        cap = _capacity_from_datasheet(datasheet.parse(body))
        if cap:
            ds_capacity[pid] = cap

    labeled = sum(1 for v in bauart.values() if _has_value(v))
    refreshed = sum(1 for pid, vid in q if pid_vid.get(pid))
    print(f"[ldy] /vergleich/: {sum(1 for c in chars.values() if c)}/{len(q)} columns; Bauart {labeled}; "
          f"vids refreshed {refreshed}/{len(q)}; category front={len(frontlader)} top={len(toplader)}; ds-cap {len(ds_capacity)}", flush=True)
    return {"chars": chars, "bauart": bauart, "name": names,
            "is_front": is_front, "is_top": is_top, "ds_capacity": ds_capacity}


def _is_waschtrockner(*texts: str | None) -> bool:
    return any("waschtrockner" in (t or "").lower() for t in texts)


def _vergleich_capacity(vid_chars: dict[str, str | None], *, allow_generic: bool = True) -> str | None:
    labels = list(_CAPACITY_WASCHEN) + (list(_CAPACITY_GENERIC) if allow_generic else [])
    for label in labels:
        if _has_value(vid_chars.get(label)):
            return vid_chars[label]
    return None


def extract_spec(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None,
                 sku: str | None = None) -> dict[str, Any]:
    ctx = ctx or {}
    pid = str(target.get("product_id") or "")
    vid_chars = ctx.get("chars", {}).get(pid, {})
    name = ctx.get("name", {}).get(pid)
    beladung = vid_chars.get("Beladung")
    loading = resolve_loading(beladung, ctx.get("bauart", {}).get(pid), name)
    if loading is None:
        # OTTO ships some frontloaders with no Bauart/name hint; fall back to its own
        # frontlader/toplader subcategory membership (real data, not a guess).
        if ctx.get("is_top", {}).get(pid):
            loading = LOADING_MAP["toplader"]
        elif ctx.get("is_front", {}).get(pid):
            loading = LOADING_MAP["frontlader"]
    # capacity = WASH capacity. For a washer-dryer, use only "(Waschen)"-explicit sources so
    # the drying capacity is never picked. top_info "Kapazität Waschen" and the name's first
    # "N kg" (wash is listed first) are wash-safe; generic /vergleich/ labels only for plain
    # washers.
    wt = _is_waschtrockner(name, target.get("retailer_sku_name"), vid_chars.get("Produkttyp"))
    capacity = (top_info(target, "Kapazität Waschen", "Füllmenge", "Fassungsvermögen")
                or _capacity_from_name(name)
                or _capacity_from_name(target.get("retailer_sku_name"))
                or _vergleich_capacity(vid_chars, allow_generic=not wt)
                or ctx.get("ds_capacity", {}).get(pid)
                or eprel.washer_rated_capacity(sku))
    return {"ldy_loading_type": loading, "ldy_capacity": capacity}


def extract_sku(target: dict[str, Any], ds: dict[str, Any], ctx: dict[str, Any] | None = None) -> str | None:
    """LDY has no datasheet; use the /vergleich/ Modellbezeichnung (handles space-separated
    models like 'BPW 814 A' the name-token heuristic misses). clean_model drops a trailing
    EAN/EPREL number and colour."""
    ctx = ctx or {}
    pid = str(target.get("product_id") or "")
    from common import model_sku
    model = model_sku.clean_model(ctx.get("chars", {}).get(pid, {}).get("Modellbezeichnung"))
    if model:
        return model
    # /vergleich/ omitted Modellbezeichnung (reduced comparison, e.g. mini washers) — the
    # model is still in the product name after "Waschmaschine".
    return _model_from_name(target.get("retailer_sku_name"))


def extract_pdp_spec(soup) -> dict[str, Any]:
    return {}
