"""Field-level English normalization for Amazon.de SEG outputs."""
from __future__ import annotations

import re
from typing import Any

_EURO = chr(8364)

TRANSLATED_FIELDS = {
    "discount_type",
    "sku_status",
    "delivery_availability",
    "fastest_delivery",
    "inventory_status",
    "screen_size",
    "ref_refrigerator_type",
}
_WEEKDAYS = {
    "montag": "Monday",
    "dienstag": "Tuesday",
    "mittwoch": "Wednesday",
    "donnerstag": "Thursday",
    "freitag": "Friday",
    "samstag": "Saturday",
    "sonntag": "Sunday",
}

_MONTHS = {
    "januar": "January",
    "februar": "February",
    "maerz": "March",
    "marz": "March",
    "märz": "March",
    "april": "April",
    "mai": "May",
    "juni": "June",
    "juli": "July",
    "august": "August",
    "september": "September",
    "oktober": "October",
    "november": "November",
    "dezember": "December",
}

_PHRASES = [
    (r"\bbefristetes\s+angebot\b", "Limited Time Offer"),
    (r"\bzeitlich\s+begrenztes\s+angebot\b", "Limited Time Offer"),
    (r"\blimited\s+time\s+offer\b", "Limited Time Offer"),
    (r"\bendet\s+in\b", "Ends in"),
    (r"\bprime\s+exklusiv(?:es)?\s+angebot\b", "Prime Exclusive Offer"),
    (r"\btop\s+angebot\b", "Top Offer"),
    (r"\bangebot\b", "Offer"),
    (r"\bdu\s+zahlst\b", "You pay"),
    (r"\bcoupon\s+mit\s+(\d+)\s*%\s+rabatt\s+angewendet\b", r"Coupon with \1% discount applied"),
    (r"\bcoupon\s+mit\s+([\d.,]+)\s*(?:\u20ac|EUR)?\s+rabatt\s+angewendet\b", r"Coupon with \1 € discount applied"),
    (r"\bgratis\s+lieferung\b", "FREE delivery"),
    (r"\bkostenlose\s+lieferung\b", "FREE delivery"),
    (r"\bkostenloser\s+versand\b", "FREE delivery"),
    (r"\bzum\s+wunschtermin\s+an\s+einen\s+ort\s+deiner\s+wahl\b", "by appointment to a location of your choice"),
    (r"\bfuer\s+qualifizierte\s+erstbestellung\b", "for qualifying first order"),
    (r"\bfür\s+qualifizierte\s+erstbestellung\b", "for qualifying first order"),
    (r"\bfuer\b", "for"),
    (r"\boder\s+schnellste\s+lieferung\s+frühestens\b", "Or earliest delivery"),
    (r"\boder\s+schnellste\s+lieferung\s+fruehestens\b", "Or earliest delivery"),
    (r"\bschnellste\s+lieferung\s+frühestens\b", "earliest delivery"),
    (r"\bschnellste\s+lieferung\s+fruehestens\b", "earliest delivery"),
    (r"\boder\s+schnellste\s+lieferung\b", "Or fastest delivery"),
    (r"\bschnellste\s+lieferung\b", "fastest delivery"),
    (r"\blieferung\b", "delivery"),
    (r"\bnur\s+noch\s+(\d+)\s+auf\s+lager\b", r"Only \1 left in stock"),
    (r"\bnur\s+noch\s+(\d+)\s+in\s+stock\b", r"Only \1 left in stock"),
    (r"\bmehr\s+ist\s+unterwegs\b", "more on the way"),
    (r"\bvor\u00fcbergehend\s+nicht\s+auf\s+lager\b", "Temporarily out of stock"),
    (r"\bvoruebergehend\s+nicht\s+auf\s+lager\b", "Temporarily out of stock"),
    (r"\bderzeit\s+nicht\s+auf\s+lager\b", "Currently out of stock"),
    (r"\bderzeit\s+nicht\s+in\s+stock\b", "Currently out of stock"),
    (r"\bnicht\s+auf\s+lager\b", "Out of stock"),
    (r"\bauf\s+lager\b", "In Stock"),
    (r"\bderzeit\s+nicht\s+verf\u00fcgbar\b", "Currently unavailable"),
    (r"\bderzeit\s+nicht\s+verfuegbar\b", "Currently unavailable"),
    (r"\bgew\u00f6hnlich\s+versandfertig\s+in\s+(\d+)\s+bis\s+(\d+)\s+tag(?:en)?\b", r"Usually ready to ship in \1 to \2 days"),
    (r"\bgewoehnlich\s+versandfertig\s+in\s+(\d+)\s+bis\s+(\d+)\s+tag(?:en)?\b", r"Usually ready to ship in \1 to \2 days"),
    (r"\bgew\u00f6hnlich\s+versandfertig\s+in\s+(\d+)\s+bis\s+(\d+)\s+monat(?:en)?\b", r"Usually ready to ship in \1 to \2 months"),
    (r"\bgewoehnlich\s+versandfertig\s+in\s+(\d+)\s+bis\s+(\d+)\s+monat(?:en)?\b", r"Usually ready to ship in \1 to \2 months"),
    (r"\bnicht\s+verf\u00fcgbar\b", "Unavailable"),
    (r"\bnicht\s+verfuegbar\b", "Unavailable"),
    (r"\bbestellung\s+innerhalb\b", "Order within"),
    (r"\bbestelle\s+innerhalb\b", "Order within"),
    (r"\bbestellen\s+sie\s+innerhalb\b", "Order within"),
    (r"\binnerhalb\b", "within"),
    (r"\bstunden?\b", "hours"),
    (r"\bstdn\.?\b", "hrs"),
    (r"\bstd\.?\b", "hrs"),
    (r"\bminuten?\b", "mins"),
    (r"\bmin\.?\b", "mins"),
    (r"\bsekunden?\b", "secs"),
    (r"\bsek\.?\b", "secs"),
    (r"\bmorgen\b", "tomorrow"),
    (r"\bheute\b", "today"),
    (r"\bzwischen\b", "between"),
    (r"\bund\b", "and"),
]


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _clean(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _replace_case_insensitive(text: str, pattern: str, repl: str) -> str:
    return re.sub(pattern, repl, text, flags=re.IGNORECASE)


def _translate_dates(text: str) -> str:
    month_names = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
    weekday_names = "|".join(sorted(map(re.escape, _WEEKDAYS), key=len, reverse=True))

    def full_date(match: re.Match[str]) -> str:
        weekday = _WEEKDAYS[match.group("weekday").casefold()]
        day = int(match.group("day"))
        month = _MONTHS[match.group("month").casefold()]
        return f"{weekday}, {month} {_ordinal(day)}"

    def month_day(match: re.Match[str]) -> str:
        day = int(match.group("day"))
        month = _MONTHS[match.group("month").casefold()]
        return f"{month} {_ordinal(day)}"

    text = re.sub(
        rf"\b(?P<weekday>{weekday_names}),\s*(?P<day>\d{{1,2}})\.\s*(?P<month>{month_names})\b",
        full_date,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\b(?P<day>\d{{1,2}})\.\s*(?P<month>{month_names})\b",
        month_day,
        text,
        flags=re.IGNORECASE,
    )

    def english_range(match: re.Match[str]) -> str:
        month = match.group("month")
        start_day = int(match.group("start"))
        end_day = match.group("end")
        suffix = match.group("suffix")
        return f"{month} {_ordinal(start_day)} - {month} {end_day}{suffix}"

    text = re.sub(
        r"\b(?P<start>\d{1,2})\.\s*-\s*(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<end>\d{1,2})(?P<suffix>st|nd|rd|th)\b",
        english_range,
        text,
        flags=re.IGNORECASE,
    )
    return text


_GERMAN_ASCII_MAP = {
    0x00e4: "ae",
    0x00c4: "Ae",
    0x00f6: "oe",
    0x00d6: "Oe",
    0x00fc: "ue",
    0x00dc: "Ue",
    0x00df: "ss",
}

_REF_TYPE_EXCLUDES = (
    r"\b(?:getraenke?\s*kuehlschrank|getraenkekuehler|beverage\s+(?:cooler|refrigerator|fridge))\b",
    r"\b(?:fleischreifeschrank|meat\s+aging\s+cabinet)\b",
    r"\b(?:kuehlvitrine|display\s+(?:refrigerator|fridge|cooler))\b",
    r"\b(?:kuehlbox|cooler\s+box)\b",
)

_REF_TYPE_PATTERNS = (
    (r"\bfrench[-\s]*door\b", "French Door"),
    (r"\bside[-\s]*by[-\s]*side\b", "Side-by-Side"),
    (
        r"\bkuehl[-/\s]*(?:und\s+)?gefrier[-/\s]*(?:kombination|kombi|schrank)\b|"
        r"\b(?:fridge|refrigerator)[-/\s]+freezer(?:\s+combination)?\b|"
        r"\bkuehlschrank\s+mit\s+gefrierfach\b",
        "Fridge-freezer Combination",
    ),
    (r"\bfreezer[-\s]*on[-\s]*bottom\b|\bgefrier(?:fach|teil)\s+unten\b", "Freezer-on-bottom"),
    (r"\bfreezer[-\s]*on[-\s]*top\b|\bgefrier(?:fach|teil)\s+oben\b", "Freezer-on-top"),
    (
        r"\b(?:einbau[-\s]*kuehlschrank|built[-\s]*in\s+refrigerator|integrated\s+refrigerator)\b",
        "Built-in Refrigerator",
    ),
    (r"\b(?:single[-\s]*door|kuehlschrank\s+mit\s+(?:einer|1)\s+tuer)\b", "Single Door"),
    (r"\b(?:counter[-\s]*depth|thekentiefe)\b", "Counter Depth"),
    (r"\b(?:tisch|vollraum|unterbau|stand|mini|kompakt)?[-\s]*kuehlschrank\b|\b(?:refrigerator|fridge)\b", "Refrigerator"),
)

_REF_TYPE_WEAK_FACT_PATTERNS = (
    (r"^(?:eingebaut|einbau|eingebettet|built[-\s]*in|integrated)$", "Built-in Refrigerator"),
    (r"^(?:zaehler\s+tie|counter[-\s]*depth|thekentiefe)$", "Counter Depth"),
)

def _translate_common(text: str) -> str:
    out = text.translate(_GERMAN_ASCII_MAP)
    out = _translate_dates(out)
    for pattern, repl in _PHRASES:
        out = _replace_case_insensitive(out, pattern, repl)
    return re.sub(r"\s+", " ", out).strip()


def classify_ref_refrigerator_type(value: Any, *, allow_weak_fact: bool = False) -> tuple[str, str | None]:
    """Return (valid|excluded|unknown, normalized refrigerator form)."""
    text = _clean(value)
    if text is None:
        return "unknown", None
    key = text.translate(_GERMAN_ASCII_MAP).casefold()
    key = re.sub(r"[\u2010-\u2015]", "-", key)
    key = re.sub(r"\s+", " ", key).strip()

    for pattern in _REF_TYPE_EXCLUDES:
        if re.search(pattern, key, flags=re.IGNORECASE):
            return "excluded", None
    valid_freezer_form = re.search(
        r"\bfreezer[-\s]*on[-\s]*(?:top|bottom)\b|"
        r"\b(?:fridge|refrigerator)[-/\s]+freezer\b|"
        r"\bkuehl[-/\s]*(?:und\s+)?gefrier[-/\s]*(?:kombination|kombi|schrank)\b|"
        r"\bkuehlschrank\s+mit\s+gefrierfach\b",
        key,
    )
    if re.search(r"\b(?:freezer|gefrierschrank|gefriertruhe|tiefkuehlschrank|tiefkuehltruhe|gefriergeraet)\b", key) and not valid_freezer_form:
        return "excluded", None
    for pattern, normalized in _REF_TYPE_PATTERNS:
        if re.search(pattern, key, flags=re.IGNORECASE):
            return "valid", normalized
    if allow_weak_fact:
        for pattern, normalized in _REF_TYPE_WEAK_FACT_PATTERNS:
            if re.search(pattern, key, flags=re.IGNORECASE):
                return "valid", normalized
    return "unknown", None


def extract_ref_refrigerator_type(value: Any, *, allow_weak_fact: bool = False) -> str | None:
    return classify_ref_refrigerator_type(value, allow_weak_fact=allow_weak_fact)[1]


def resolve_ref_refrigerator_type(title: Any, *fact_values: Any) -> str | None:
    """Resolve title first, then exact Aufbau facts; never use Konfiguration."""
    state, normalized = classify_ref_refrigerator_type(title)
    if state == "valid":
        return normalized
    if state == "excluded":
        return None
    for value in fact_values:
        state, normalized = classify_ref_refrigerator_type(value, allow_weak_fact=True)
        if state == "valid":
            return normalized
        if state == "excluded":
            return None
    return None


def _translate_ref_refrigerator_type(text: str) -> str | None:
    return extract_ref_refrigerator_type(text)


def translate_field(field: str, value: Any) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    if field not in TRANSLATED_FIELDS:
        return text
    if field == "sku_popularity":
        normalized = re.sub(r"\s+", " ", text).strip()
        folded = normalized.casefold()
        if "bestseller" in folded or "best seller" in folded:
            return "Bestseller"
        if "amazons tipp" in folded or "amazon's choice" in folded or "amazon\u2019s choice" in folded:
            return "Amazon's Choice"
        return _translate_common(normalized)
    if field == "screen_size":
        match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:zoll|inch(?:es)?|[\"\u201d])", text, flags=re.IGNORECASE)
        if match:
            size = match.group(1).replace(",", ".")
            return f"{size} inches"
        text = _replace_case_insensitive(text, r"\bzoll\b", "inches")
        return re.sub(r"\s+", " ", text).strip()
    if field == "ref_refrigerator_type":
        return _translate_ref_refrigerator_type(text)
    return _translate_common(text)


def translate_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    for field in TRANSLATED_FIELDS - {"ref_refrigerator_type"}:
        if field in record:
            record[field] = translate_field(field, record.get(field))
    if "ref_refrigerator_type" in record:
        record["ref_refrigerator_type"] = resolve_ref_refrigerator_type(
            record.get("retailer_sku_name"),
            record.get("ref_refrigerator_type"),
        )
    return record
