"""German -> English translation for OTTO listing labels.

Covers sku_popularity, sku_status, discount_type (verified sticker names), and
delivery_availability. Unknown discount names stay NULL; other fields keep raw.
"""
from __future__ import annotations

import re
from datetime import date

WEEKDAYS = {
    "mo": "Monday", "di": "Tuesday", "mi": "Wednesday", "do": "Thursday",
    "fr": "Friday", "sa": "Saturday", "so": "Sunday",
    "morgen": "tomorrow", "heute": "today",
}

DELIVERY_MONTHS = {
    "jan": (1, "January"), "feb": (2, "February"), "mär": (3, "March"),
    "mrz": (3, "March"), "apr": (4, "April"), "mai": (5, "May"),
    "jun": (6, "June"), "jul": (7, "July"), "aug": (8, "August"),
    "sep": (9, "September"), "sept": (9, "September"), "okt": (10, "October"),
    "nov": (11, "November"), "dez": (12, "December"),
}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def translate_popularity(value: str | None) -> str | None:
    s = _clean(value)
    if not s:
        return None
    return "Very popular" if s.lower() == "sehr beliebt" else s


def translate_status(value: str | None) -> str | None:
    s = _clean(value)
    if not s:
        return None
    return "Sponsored" if s.lower() == "gesponsert" else s


STICKER_TRANSLATIONS = {
    "Deal des Monats": "Deal of the month",
    "Deal der Woche": "Deal of the week",
    "Deal & Gewinne": "Deal & Win",
    "Unser Hero": "Our Hero",
    "Heroes": "Our Hero",
    "Premium Hero": "Premium Hero",
    "Technik Highlights": "Tech Highlights",
    "Pre-Sale": "Pre-Sale",
    "OTTO Days": "OTTO Days",
}


def translate_discount_type(value: str | None) -> str | None:
    """Translate verified sticker text; price-period highlights are not stickers."""
    s = _clean(value)
    if not s:
        return None
    for raw, english in STICKER_TRANSLATIONS.items():
        if s.casefold() in (raw.casefold(), english.casefold()):
            return english
    return None


def translate_delivery(value: str | None) -> str | None:
    s = _clean(value)
    if not s:
        return None
    low = s.lower()
    if low == "lieferbar - am nächsten werktag bei dir":
        return "Available - at your door the next working day"
    m = re.match(r"lieferbar - in\s+(\d+(?:-\d+)?) werktagen bei dir", low)
    if m:
        return f"Available - at your door in {m.group(1)} working days"
    working_days = re.fullmatch(
        r"in ([1-9][0-9]*)\s*[-–]\s*([1-9][0-9]*) werktagen bei dir", low,
    )
    if working_days:
        first, last = working_days.groups()
        if (len(first), first) <= (len(last), last):
            return f"Available - at your door in {first}-{last} working days"
    deadline = re.fullmatch(r"bis ([a-z]+)\.?, ([0-9]{1,2})\. ([a-zä]+)\.? bei dir", low)
    if deadline:
        weekday, day, month = deadline.groups()
        if weekday in WEEKDAYS and month in DELIVERY_MONTHS:
            month_number, english_month = DELIVERY_MONTHS[month]
            try:
                # OTTO omits the year, so allow February 29.
                date(2000, month_number, int(day))
            except ValueError:
                return s
            return f"At your door by {WEEKDAYS[weekday]}, {english_month} {int(day)}"
    m = re.match(r"lieferbar in\s+(\d+(?:-\d+)?) wochen", low)
    if m:
        weeks = m.group(1)
        return f"Available in {weeks} week" + ("s" if weeks != "1" else "")
    if "ausverkauft" in low or "nicht lieferbar" in low or "nicht verfügbar" in low:
        return "Sold out"
    if low.startswith("sofort lieferbar"):
        return "Immediately available"
    if low.startswith("lieferbar"):
        tail = s.split("-", 1)[1].strip() if "-" in s else ""
        return f"Available - {tail}" if tail else "Available"
    return s  # unknown -> keep raw
