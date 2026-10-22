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
_FUNCTION = re.compile(r"(?<![A-Za-z0-9])(?:\d+\s*[- ]?in[- ]?\s*\d+|all[- ]in[- ]one)(?![A-Za-z0-9])", re.I)
_CAPACITY = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(kg|g)\b", re.I)
_ENERGY_CLASS = re.compile(r"[A-G]\+{0,3}", re.I)
_PROGRAM_OR_SERIES = re.compile(r"(?:\d+\s*programme?n?|\d+\s*programs?|(?:serie|series|iq)[-_ ]?\d+)", re.I)
# Semantic exclusions, not product IDs or manufacturer-specific exceptions.
_NON_MODEL_WORDS = {
    "serie", "series", "serien", "iq", "programm", "programme", "programmen",
    "programs", "ptc", "led", "uv", "eek", "klasse", "class", "modell", "model",
    "mini", "dryer", "trockner", "eco", "speed",
}
# Keep accented German words whole: splitting Wärmepumpentrockner at ä would
# produce a standalone W and incorrectly treat the next word as a watt unit.
_TOKEN = re.compile(r"[^\W_]+(?:[-_/.][^\W_]+)*(?:\+{1,3})?")


def valid_sku(value) -> str | None:
    value = text_clean(value)
    if not value or value.casefold() in {"-", "--", "n/a", "null", "none", "unknown"}:
        return None
    if _FUNCTION.search(value) or _ENERGY_CLASS.fullmatch(value) or _PROGRAM_OR_SERIES.fullmatch(value):
        return None
    parts = [part for part in re.split(r"\s*[/+–-]\s*", value) if part]
    if parts and all(_MEASURE.fullmatch(part) for part in parts):
        return None
    return value


def title_sku(title, model_hints=()) -> str | None:
    title = text_clean(title) or ""
    confirmed_title = None
    # A specification may confirm the boundaries of a spaced title model, but
    # never replace it. Reject partial matches (e.g. a missing regional suffix).
    for hint in model_hints:
        hint = valid_sku(hint)
        if not hint:
            continue
        parts = hint.split()
        if len(parts) > 1 and re.fullmatch(r"\d{7,12}", parts[-1]):
            hint = " ".join(parts[:-1])  # separate catalogue/PNC code
        compact = re.sub(r"\s+", "", hint)
        if (len(compact) < 5 or not any(c.isalpha() for c in compact)
                or not any(c.isdigit() for c in compact)
                or any(_MEASURE.fullmatch(p) or ("+" in p and _ENERGY_CLASS.fullmatch(p)) for p in hint.split())):
            continue
        match = re.search(r"(?<!\w)" + r"\s*".join(map(re.escape, compact)) + r"(?![\w/+.-])", title, re.I)
        if match and (confirmed_title is None or match.start() < confirmed_title[0]):
            confirmed_title = (match.start(), match.group())

    def choose(candidate, start):
        # A matching API hint can complete the first title model's boundaries,
        # never select a different model mentioned later in the same title.
        return confirmed_title[1] if confirmed_title and confirmed_title[0] <= start else candidate
    matches = list(_TOKEN.finditer(title))
    tokens = [match.group() for match in matches]
    for index, token in enumerate(tokens):
        # A short uppercase prefix plus a separate 4..6 digit identifier, e.g.
        # WPT 7155. Require whitespace only and reject quantity/series contexts.
        if (index > 0 and re.fullmatch(r"[A-Z]{2,4}", token) and normalized(token) not in _NON_MODEL_WORDS
                and index + 1 < len(tokens) and re.fullmatch(r"\d{4,6}", tokens[index + 1])
                and title[matches[index].end():matches[index + 1].start()].isspace()):
            following = tokens[index + 2] if index + 2 < len(tokens) else ""
            if not re.fullmatch(r"(?:kg|g|k?w|watt|v|hz|rpm|cm|mm|l|db|programme?n?|programs)", following, re.I):
                return choose(token + " " + tokens[index + 1], matches[index].start())
        # Spaced alphanumeric models such as TR 9 W 80690 or DV 91 DG 52 A 0 AEEG.
        # Require multiple numeric components, not a lone series/program count.
        if (index > 0 and re.fullmatch(r"[A-Z]{2,4}", token)
                and normalized(token) not in _NON_MODEL_WORDS
                and index + 1 < len(tokens) and re.fullmatch(r"\d{1,3}", tokens[index + 1])):
            end = index
            for pos in range(index + 1, min(len(tokens), index + 8)):
                if (not title[matches[pos-1].end():matches[pos].start()].isspace()
                        or not re.fullmatch(r"[A-Z]{1,4}|\d{1,6}", tokens[pos])
                        or normalized(tokens[pos]) in _NON_MODEL_WORDS):
                    break
                end = pos
            picked = tokens[index:end+1]
            following = tokens[end+1] if end + 1 < len(tokens) else ""
            if (sum(part.isdigit() for part in picked) >= 2
                    and sum(c.isdigit() for part in picked for c in part) >= 4
                    and not re.fullmatch(r"kg|g|k?w|watt|hz|rpm|cm|mm|l|db|programme?n?", following, re.I)):
                candidate = title[matches[index].start():matches[end].end()]
                if valid_sku(candidate):
                    return choose(candidate, matches[index].start())
        if (len(token) < 5 or not any(c.isalpha() for c in token)
                or not any(c.isdigit() for c in token) or not valid_sku(token)
                or re.fullmatch(r"(?:iq|serie|series|programm[e]?)[-_]?\d+", token, re.I)):
            continue
        picked = [token]
        # CSOE H8A2DE-S / NDE H8A2TCEXS-S: the short prefix belongs to the model,
        # while the first title token (normally the brand) is never prepended.
        if (index > 1 and re.fullmatch(r"[A-Z]{2,4}", tokens[index-1])
                and normalized(tokens[index-1]) not in _NON_MODEL_WORDS
                and title[matches[index-1].end():matches[index].start()].isspace()):
            picked.insert(0, tokens[index-1])
        for tail in tokens[index + 1:index + 4]:
            if (len(tail) < 2 or not valid_sku(tail)
                    or normalized(tail) in _NON_MODEL_WORDS
                    or normalized(tail) in {"weiss", "white", "schwarz", "black", "grau", "silver"}):
                break
            if tail.isupper() and tail.isalpha():
                picked.append(tail)
            else:
                break
        start = matches[index-1].start() if picked[0] != token else matches[index].start()
        return choose(" ".join(picked), start)
    return confirmed_title[1] if confirmed_title else None


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
    model = None
    for key in MODEL_LABELS:
        match = next((f for f in features if label_key(f["name"]) == key and valid_sku(f["value"])), None)
        if match:
            model = match
            break
    for title in titles:
        candidate = title_sku(title, (model["value"],) if model else ())
        if sources["sku"]["value"] is None and candidate:
            sources["sku"] = evidence("title", candidate, value=candidate)
        if sources["loading_type"]["value"] is None and loading_candidate(title):
            sources["loading_type"] = evidence("title", loading_candidate(title), value=loading_candidate(title))
    if model:
        if sources["sku"]["value"] is None:
            sources["sku"] = evidence("spec", model["value"], label=model["name"], value=valid_sku(model["value"]))
        else:
            signature = lambda value: re.sub(r"\s+", "", value).casefold()
            sources["sku"].update(spec_value=model["value"], spec_label=model["name"],
                                  conflict=signature(sources["sku"]["value"]) != signature(model["value"]))
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
