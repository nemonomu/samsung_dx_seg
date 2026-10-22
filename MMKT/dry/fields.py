"""DRY-only extraction; keep source units and reject non-model measurements."""
from __future__ import annotations

import re

from common.parsers import text_clean
from dry import config
from dry.policy import normalized


def label_key(value) -> str:
    return re.sub(r"[^a-z0-9]", "", normalized(value))


MODEL_LABELS = tuple(map(label_key, (
    "Modelkennung", "Modellkennung", "Modellname / -kennzeichen", "Modellname",
    "Modellkennzeichen", "Modellbezeichnung", "Model name", "Model number")))
_MEASURE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:k?w|watt|kg|g|v|hz|rpm|u/min|cm|mm|l|db|%)", re.I)
_FUNCTION = re.compile(r"(?:\d+\s*[- ]?in[- ]?\s*\d+|all[- ]in[- ]one)", re.I)
_CAPACITY = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(kg|g)\b", re.I)


def valid_sku(value) -> str | None:
    value = text_clean(value)
    if not value or value.casefold() in {"-", "--", "n/a", "null", "none", "unknown"}:
        return None
    if _FUNCTION.fullmatch(value):
        return None
    parts = re.split(r"\s*[/+–-]\s*", value)
    if parts and all(_MEASURE.fullmatch(part) for part in parts):
        return None
    return value


def title_sku(title) -> str | None:
    tokens = re.findall(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*", text_clean(title) or "")
    for index, token in enumerate(tokens):
        if (len(token) < 5 or not any(c.isalpha() for c in token)
                or not any(c.isdigit() for c in token) or not valid_sku(token)):
            continue
        picked = [token]
        for tail in tokens[index + 1:index + 4]:
            if (len(tail) < 2 or not valid_sku(tail)
                    or normalized(tail) in {"weiss", "white", "schwarz", "black", "grau", "silver"}):
                break
            if tail.isupper() and tail.isalpha():
                picked.append(tail)
            else:
                break
        return " ".join(picked)
    return None


def evidence(source, raw=None, unit=None, *, label=None, value=None):
    return {"source": source, "label": label, "raw_value": raw, "raw_unit": unit, "value": value}


def capacity_candidate(value, *, unit=None, label=None, source="spec"):
    raw = text_clean(value)
    if not raw:
        return None
    text = raw
    if re.fullmatch(r"\d+(?:[.,]\d+)?", raw):
        suffix = text_clean(unit)
        if not suffix and re.search(r"\bin\s+(kg|g)\b", label or "", re.I):
            suffix = re.search(r"\bin\s+(kg|g)\b", label, re.I).group(1)
        text += " " + (suffix or "")
    candidates = []
    for match in _CAPACITY.finditer(text):
        if (re.search(r"(?:gewicht|weight|verpackung)\s*[:=]?\s*(?:ca\.?\s*)?$",
                      text[max(0, match.start()-40):match.start()], re.I)
                or re.match(r"\s*(?:eigengewicht|gewicht|weight|versandgewicht|verpackung)\b", text[match.end():], re.I)):
            continue
        amount, picked_unit = match.groups()
        if float(amount.replace(",", ".")) <= 0:
            continue
        candidates.append((picked_unit.casefold(), amount, match.group(0), picked_unit))
    if not candidates:
        return None
    picked_unit, amount, raw_quantity, raw_unit = min(candidates, key=lambda pair: pair[0] != "kg")
    return evidence(source, raw_quantity if source == "title" else raw,
                    raw_unit if source == "title" else unit, label=label, value=amount + picked_unit)


def loading_candidate(value):
    text = normalized(value)
    if text in config.LOADING_TRANSLATIONS:
        return config.LOADING_TRANSLATIONS[text]
    if re.search(r"\bfront(?:lader|loader|[ -]+load(?:er)?)\b", text):
        return "Front load"
    if re.search(r"\btop(?:lader|loader|[ -]+load(?:er)?)\b", text):
        return "Top load"
    return None


def select_fields(features: list[dict], titles: list[str]) -> tuple[dict, dict]:
    """features contain only name/value/unit, never an entire API product."""
    sources = {key: evidence("missing") for key in ("sku", "capacity", "loading_type")}
    for key in MODEL_LABELS:
        match = next((f for f in features if label_key(f["name"]) == key and valid_sku(f["value"])), None)
        if match:
            sources["sku"] = evidence("spec", match["value"], label=match["name"], value=valid_sku(match["value"]))
            break
    for title in titles:
        if sources["sku"]["value"] is None and title_sku(title):
            sources["sku"] = evidence("title", title_sku(title), value=title_sku(title))
        if sources["loading_type"]["value"] is None and loading_candidate(title):
            sources["loading_type"] = evidence("title", loading_candidate(title), value=loading_candidate(title))
    title_capacities = [capacity_candidate(title, source="title") for title in titles]
    title_capacities = [candidate for candidate in title_capacities if candidate]
    if title_capacities:
        sources["capacity"] = min(title_capacities, key=lambda c: not c["value"].endswith("kg"))
    capacity_labels = {label_key(key): index for index, key in enumerate(config.CAPACITY_FEATURES)}
    if sources["capacity"]["value"] is None:
        candidates = [capacity_candidate(f["value"], unit=f.get("unit"), label=f["name"])
                      for f in sorted(features, key=lambda f: capacity_labels.get(label_key(f["name"]), 99))
                      if label_key(f["name"]) in capacity_labels]
        candidates = [candidate for candidate in candidates if candidate]
        if candidates:
            sources["capacity"] = min(candidates, key=lambda c: not c["value"].endswith("kg"))
    if sources["loading_type"]["value"] is None:
        for feature in features:
            if label_key(feature["name"]) in {label_key(k) for k in config.LOADING_FEATURES}:
                value = loading_candidate(feature["value"])
                if value:
                    sources["loading_type"] = evidence("spec", feature["value"], label=feature["name"], value=value)
                    break
    return {key: record["value"] for key, record in sources.items()}, sources
