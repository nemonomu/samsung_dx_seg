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

_REF_TYPE_PHRASES = [
    (r"\bkompakt\s+ohne\s+gefrierfach\b", "compact without freezer compartment"),
    (r"\bvollformat\s*\(\s*ohne\s+gefrierfach\s*\)", "full-size without freezer compartment"),
    (r"\bvollformat\s*\(\s*gefrierfach\s+unterhalb\s*\)", "full-size freezer-on-bottom"),
    (r"\bvollformat\s*\(\s*gefrierfach\s+oberhalb\s*\)", "full-size freezer-on-top"),
    (r"\bkuehlschrank\s+ohne\s+gefrierfach\b", "refrigerator without freezer compartment"),
    (r"^\s*\"?mit\s+gefrierfach\"?\s*$", "with freezer compartment"),
    (r"\bohne\s+gefrierfach\b", "without freezer compartment"),
    (r"\bgefrierfach\s+innen\b", "internal freezer compartment"),
    (r"\bkompakt\s+freezer[-\s]*on[-\s]*top\b", "compact freezer-on-top"),
    (r"\bkompakter\s+gefrierschrank\s+unten\b", "compact freezer-on-bottom"),
    (r"\bkompakt\s+interner\s+gefrierschrank\b", "compact internal freezer compartment"),
    (r"\bkompakt\b", "compact"),
    (r"\bkuehlfach\s+unten\s*/\s*eiswuerfelfach\s+oben\b", "refrigerator compartment bottom / ice cube compartment top"),
    (r"\bkuehlraum\s+oben\s*/\s*gefrierraum\s+unten\b", "refrigerator compartment top / freezer compartment bottom"),
    (r"\bgefrierraum\s+oben\s*/\s*kuehlraum\s+unten\b", "freezer compartment top / refrigerator compartment bottom"),
    (r"\bfreezer[-\s]*on[-\s]*top\s*/\s*kuehlteil\s+unten\b", "freezer-on-top / refrigerator compartment bottom"),
    (r"\beinbau[-\s]*kuehlgeraet\b", "built-in refrigerator"),
    (r"\bfranzoesische\s+tueren\b", "French Door"),
    (r"\bfreistehend\b", "freestanding"),
    (r"\bmanuell\b", "manual"),
    (r"\bwendbare\s+tuer\b", "reversible door"),
    (r"\bintegrierte\s+auffangschale\b", "integrated drip tray"),
    (r"\bedelstahl\s+antifingerprint\b", "stainless steel anti-fingerprint"),
    (r"\bbosch\s+kuehl\s+gefrier\b", "Bosch refrigerator-freezer"),
    (r"^\s*kuehlschrank\s*$", "refrigerator"),
    (r"\bvollraum\b", "all-refrigerator"),
    (r"\bohne\s+wasserspender\b", "without water dispenser"),
    (r"\bmit\s+eis\s*[-/]?\s*/?\s*wasserspender\b", "with ice/water dispenser"),
    (r"\bmultifach\s+mit\s+schubladen,\s*regalen,\s*flaschenfaechern\s+und\s+eierfaechern\b", "multi-compartment with drawers, shelves, bottle compartments and egg compartments"),
    (r"\bsmart\s+inverter\s+kompressor\b", "Smart Inverter Compressor"),
    (r"\bmini\s+reffrigerator\b", "Mini Refrigerator"),
    (r"\bcompact\s+freezer[-\s]*on[-\s]*bottom\b", "compact freezer-on-bottom"),
    (r"\bcompact\s+side[-\s]*by[-\s]*side\b", "compact Side-by-Side"),
    (r"\bfull[-\s]*sized\s+side[-\s]*by[-\s]*side\b", "full-size Side-by-Side"),
    (r"\bfull[-\s]*sized\s+french\s+door\b", "full-size French Door"),
    (r"\bside[-\s]+by[-\s]+side\b", "Side-by-Side"),
    (r"\bsingle\s+door\b", "Single Door"),
    (r"\bcross\s+door\b", "Cross Door"),
    (r"\bgefrierfach\s+unten\b", "freezer-on-bottom"),
    (r"\bgefrierteil\s+unten\b", "freezer-on-bottom"),
    (r"\bgefrierfach\s+oben\b", "freezer-on-top"),
    (r"\bgefrierteil\s+oben\b", "freezer-on-top"),
    (r"\bkuehl[-\s]*gefrier[-\s]*kombination\b", "refrigerator-freezer combination"),
    (r"\bkuehlschrank\s+mit\s+gefrierfach\b", "refrigerator with freezer compartment"),
    (r"\bsmall\uff0cno\s+freezer\s+compartment\b", "small, no freezer compartment"),
    (r"\u5c0f\u578b\uff0c\u65e0\u51b7\u51bb\u5ba4", "small, no freezer compartment"),
]


def _translate_common(text: str) -> str:
    out = text.translate(_GERMAN_ASCII_MAP)
    out = _translate_dates(out)
    for pattern, repl in _PHRASES:
        out = _replace_case_insensitive(out, pattern, repl)
    return re.sub(r"\s+", " ", out).strip()


def _translate_ref_refrigerator_type(text: str) -> str:
    out = text.translate(_GERMAN_ASCII_MAP)
    for pattern, repl in _REF_TYPE_PHRASES:
        out = _replace_case_insensitive(out, pattern, repl)
    return re.sub(r"\s+", " ", out).strip()


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
    for field in TRANSLATED_FIELDS:
        if field in record:
            record[field] = translate_field(field, record.get(field))
    return record
