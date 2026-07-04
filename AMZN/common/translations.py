"""Field-level English normalization for Amazon.de SEG outputs."""
from __future__ import annotations

import re
from typing import Any

TRANSLATED_FIELDS = {
    "discount_type",
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
    (r"\bprime\s+exklusiv(?:es)?\s+angebot\b", "Prime Exclusive Offer"),
    (r"\btop\s+angebot\b", "Top Offer"),
    (r"\bangebot\b", "Offer"),
    (r"\bgratis\s+lieferung\b", "FREE delivery"),
    (r"\bkostenlose\s+lieferung\b", "FREE delivery"),
    (r"\bkostenloser\s+versand\b", "FREE delivery"),
    (r"\boder\s+schnellste\s+lieferung\s+frühestens\b", "Or earliest delivery"),
    (r"\boder\s+schnellste\s+lieferung\s+fruehestens\b", "Or earliest delivery"),
    (r"\bschnellste\s+lieferung\s+frühestens\b", "earliest delivery"),
    (r"\bschnellste\s+lieferung\s+fruehestens\b", "earliest delivery"),
    (r"\boder\s+schnellste\s+lieferung\b", "Or fastest delivery"),
    (r"\bschnellste\s+lieferung\b", "fastest delivery"),
    (r"\blieferung\b", "delivery"),
    (r"\bnur\s+noch\s+(\d+)\s+auf\s+lager\b", r"Only \1 left in stock"),
    (r"\bnur\s+noch\s+(\d+)\s+in\s+stock\b", r"Only \1 left in stock"),
    (r"\bvor\u00fcbergehend\s+nicht\s+auf\s+lager\b", "Temporarily out of stock"),
    (r"\bvoruebergehend\s+nicht\s+auf\s+lager\b", "Temporarily out of stock"),
    (r"\bauf\s+lager\b", "In Stock"),
    (r"\bderzeit\s+nicht\s+verf\u00fcgbar\b", "Currently unavailable"),
    (r"\bderzeit\s+nicht\s+verfuegbar\b", "Currently unavailable"),
    (r"\bnicht\s+verf\u00fcgbar\b", "Unavailable"),
    (r"\bnicht\s+verfuegbar\b", "Unavailable"),
    (r"\bbestellung\s+innerhalb\b", "Order within"),
    (r"\bbestelle\s+innerhalb\b", "Order within"),
    (r"\bbestellen\s+sie\s+innerhalb\b", "Order within"),
    (r"\binnerhalb\b", "within"),
    (r"\bstunden?\b", "hours"),
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
    (r"\bgefrierfach\s+unten\b", "freezer-on-bottom"),
    (r"\bgefrierteil\s+unten\b", "freezer-on-bottom"),
    (r"\bgefrierfach\s+oben\b", "freezer-on-top"),
    (r"\bgefrierteil\s+oben\b", "freezer-on-top"),
    (r"\bkuehl[-\s]*gefrier[-\s]*kombination\b", "refrigerator-freezer combination"),
    (r"\bkuehlschrank\s+mit\s+gefrierfach\b", "refrigerator with freezer compartment"),
    (r"\bside\s+by\s+side\b", "Side by Side"),
    (r"\bfrench\s+door\b", "French Door"),
]


def _translate_common(text: str) -> str:
    out = _translate_dates(text)
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
    if field == "screen_size":
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
