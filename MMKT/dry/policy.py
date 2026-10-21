"""Evidence-based eligibility. Apply to occurrences BEFORE product-ID dedup."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

from common.listing_policy import is_banner_ad


def normalized(value) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").casefold())
                   if not unicodedata.combining(c))


# A qualifier alone does not prove an accessory: "mit Combo" can still be a
# bundle. Shorten a title only for an explicit part/feature description.
_MODIFIER = re.compile(r"\b(?:mit|with|inkl(?:usive)?|incl(?:uding)?|einschliesslich|fur|fuer|for)\b|\+")
_DRYER = re.compile(r"waschetrockner|warmepumpentrockner|konden(?:sations|s)trockner|ablufttrockner|\btrockner\b|tumble[\s-]*dryer")
_GENERIC_COMBINATION = re.compile(r"all[\s-]*in[\s-]*one|\b2[\s-]*in[\s-]*1\b")
_INCLUDED_PART = re.compile(r"\bzubehor\b|accessor(?:y|ies)|filter|anschluss|verbindungssatz|montage|ablaufschlauch")


def _identity_head(name: str) -> str:
    parts = _MODIFIER.split(name, maxsplit=1)
    if len(parts) == 2:
        tail_head = _MODIFIER.split(parts[1], maxsplit=1)[0]
        if _INCLUDED_PART.search(tail_head) and not _DRYER.search(tail_head):
            return parts[0]
    return name


def exclusion_reason(row: dict, *, features: dict | None = None,
                     detail_name: str | None = None, require_evidence: bool = True) -> str | None:
    if is_banner_ad(row):
        return "advertisement"
    features = features or {}
    names = [normalized(v) for v in (row.get("retailer_sku_name"), detail_name) if v]
    product_type = normalized(" ".join(str(v or "") for v in (
        row.get("product_type"), features.get("Produkttyp"), features.get("Trockner-Typ"))))
    text = " ".join([*names, product_type])
    if not text.strip():
        text = normalized(unquote(str(row.get("product_url") or "")))
        names = [text]
    identity = " ".join([*(_identity_head(name) for name in names), product_type])
    if re.search(r"\bwaschturm\b|wash[\s-]*tower|laundry[\s-]*tower", text):
        return "set_or_combo"
    if re.search(r"waschtrockner|wasch[\s/+-]*(?:und[\s-]*)?trock(?:ner|en)|washer[\s/-]*dryer", text):
        return "washer_dryer"
    if re.search(r"waschmaschine|washing[\s-]*machine|\bwasher\b", text):
        return "washing_machine"
    if re.search(r"haartrockner|fon\b|hair[\s-]*dryer|handetrockner|schuhtrockner|luftentfeuchter|geschirrtrockner", text):
        return "other_appliance"
    if re.search(r"\b(bundle|set|sets|paket|combo|kombo|kombination)\b", identity):
        return "set_or_combo"
    if re.search(r"zwischenbaurahmen|verbindungssatz|ersatzteil|ersatzfilter|trocknerball|\bzubehor\b|waschestander|waschespinne", identity):
        return "accessory"
    if _GENERIC_COMBINATION.search(text) and not _DRYER.search(product_type):
        # "2-in-1" alone cannot prove washing capability. Let the initial
        # listing pass reach the specification API; require type evidence later.
        return "unconfirmed_product_type" if require_evidence else None
    if _DRYER.search(identity):
        return None
    return "unconfirmed_product_type" if require_evidence else None
