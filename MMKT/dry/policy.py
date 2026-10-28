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
# MediaMarkt also labels heat-pump dryers "Wärmepumpetrockner" (without n).
# Recognize that spelling for eligibility only; keep original source values.
_DRYER = re.compile(r"waschetrockner|warmepumpen?trockner|konden(?:sations|s)trockner|ablufttrockner|\btrockner\b|tumble[\s-]*dryer")
_GENERIC_COMBINATION = re.compile(r"all[\s-]*in[\s-]*one|\b2[\s-]*in[\s-]*1\b")
_INCLUDED_PART = re.compile(r"\bzubehor\b|accessor(?:y|ies)|filter|anschluss|verbindungssatz|montage|ablaufschlauch")
_WASHER_DRYER = re.compile(r"waschtrockner|wasch[\s/+-]*(?:und[\s-]*)?trock(?:ner|en)|washer[\s/-]*dryer")
_CAPACITY_PAIR = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?\s*kg\s*/\s*\d+(?:[.,]\d+)?\s*kg\b")
_SPIN_SPEED = re.compile(r"(?<!\d)[1-9]\d{2,3}\s*(?:u\s*/\s*min\.?|rpm|umdrehungen\s*/\s*min\.?)\b")


def washer_dryer_evidence(row: dict, *, features: dict | None = None,
                          detail_name: str | None = None) -> dict | None:
    """Require explicit identity or capacity-pair AND spin in the same title.

    Do not combine unrelated specifications, recommendation text or accessory
    compatibility clauses. Equal wash/dry capacities are still a valid pair.
    """
    features = features or {}
    values = [("title", None, v) for v in (row.get("retailer_sku_name"), detail_name) if v]
    values.extend(("spec", label, value) for label, value in (
        ("product_type", row.get("product_type")), ("Produkttyp", features.get("Produkttyp")),
        ("Trockner-Typ", features.get("Trockner-Typ"))) if value)
    for source, label, raw in values:
        text = normalized(raw)
        explicit = _WASHER_DRYER.search(text)
        if explicit:
            return {"basis": "explicit_washer_dryer", "source": source,
                    "label": label, "matched_text": explicit.group()}
        if source == "title":
            identity = _MODIFIER.split(text, maxsplit=1)[0]
            pair, spin = _CAPACITY_PAIR.search(identity), _SPIN_SPEED.search(identity)
            if pair and spin:
                return {"basis": "capacity_pair_and_spin_speed", "source": source,
                        "capacity_pair": pair.group(), "spin_speed": spin.group()}
    return None


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
    if any("portabel" in name for name in names):
        return "portabel_title"
    product_type = normalized(" ".join(str(v or "") for v in (
        row.get("product_type"), features.get("Produkttyp"), features.get("Trockner-Typ"))))
    text = " ".join([*names, product_type])
    if not text.strip():
        text = normalized(unquote(str(row.get("product_url") or "")))
        names = [text]
    identity = " ".join([*(_identity_head(name) for name in names), product_type])
    if re.search(r"\bwaschturm\b|wash[\s-]*tower|laundry[\s-]*tower", text):
        return "set_or_combo"
    if washer_dryer_evidence(row, features=features, detail_name=detail_name) or _WASHER_DRYER.search(text):
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
