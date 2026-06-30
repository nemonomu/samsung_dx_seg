"""German -> English translation for OTTO listing labels (pattern-based, full coverage).

Covers sku_popularity, sku_status, discount_type (deal highlights), and
delivery_availability. Unknown values fall back to the raw string.
"""
from __future__ import annotations

import re

WEEKDAYS = {
    "montag": "Monday", "dienstag": "Tuesday", "mittwoch": "Wednesday",
    "donnerstag": "Thursday", "freitag": "Friday", "samstag": "Saturday",
    "sonntag": "Sunday", "morgen": "tomorrow", "heute": "today",
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


def translate_discount_type(value: str | None) -> str | None:
    s = _clean(value)
    if not s:
        return None
    low = s.lower()
    if low == "nur für kurze zeit":
        return "Only for a short time"
    if low == "nur diesen monat":
        return "Only this month"
    if low == "deal des monats":
        return "Deal of the month"
    if low in ("nur noch heute", "nur heute"):
        return "Only today"
    m = re.fullmatch(r"nur bis (\w+)", low)
    if m:
        wd = WEEKDAYS.get(m.group(1))
        return f"Only until {wd}" if wd else s
    return s  # unknown deal highlight -> keep raw


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
