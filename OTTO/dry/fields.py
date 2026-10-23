"""Dryer-only selection and source precedence. No I/O in this module."""
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from common.model_sku import clean_model
from dry.config import CAPACITY_LABELS, TYPE_LABELS, CATEGORY_LABELS


def text(value):
    return re.sub(r"\s+", " ", str(value)).strip() if value not in (None, "") else ""


def name_key(value):
    return text(value).casefold()  # same normalization as LDY targets


DRYER = re.compile(r"\b(?:(?:wärme[- ]?pumpen|waerme[- ]?pumpen|kondens(?:ations)?|abluft|wäsche|waesche)[- ]?)?trockner\b", re.I)
SPECIFIC_DRYER = re.compile(r"\b(?:wärme[- ]?pumpen|waerme[- ]?pumpen|kondens(?:ations)?|abluft)[- ]?trockner\b", re.I)
WASHER = re.compile(r"\bwasch(?:maschine[n]?|vollautomat(?:en)?)\b", re.I)
EXCLUDED = (
    (r"\bwasch[- ]?trockner\b", "washer_dryer"),
    (r"\b(?:mini[- ]?|reise[- ]?|camping[- ]?)(?:wäsche)?trockner\b|\b(?:tragbar\w*|portabel\w*|portable|mobil(?:e[rsmn]?)?)\b", "portable"),
    (r"\b(?:haartrockner|haarföhn|föhn|foehn|multi[- ]?haarstyler|haarstyler)\b", "hair_dryer"),
    (r"\b(?:dörrautomat|doerrautomat|lebensmitteltrockner|obsttrockner)\b", "food_dehydrator"),
    (r"\b(?:luftentfeuchter|bautrockner)\b", "dehumidifier"),
    (r"\b(?:waschmaschinenumbauschrank|waschmaschinenschrank)\b", "furniture"),
    (r"\b(?:wäscheschleuder|waescheschleuder|schleuder|zentrifuge)\b", "spin_dryer"),
    (r"\b(?:wäscheständer|waeschestaender|flügelwäschetrockner|standtrockner|wandtrockner|wäscheleine|deckentrockner|turmtrockner|trockengestell|trockensack|trockenschrank)\b", "drying_rack_or_cabinet"),
    (r"\b(?:dampfschrank|kleiderpflegeschrank|kleiderschrank|dampfglätter|kleiderdampfer|styler|airdresser)\b", "garment_care"),
    (r"\b\w*(?:untergestell|unterschrank|sockel|zwischenbaurahmen|verbindungsrahmen|stapelrahmen|trocknerbälle|trocknerball|ersatzteil|ersatzfilter|flusensieb|türgriff|schlauch|abdeckung|antivibrationsmatte|zubehör)\w*\b|\b(?:filter|gestell|rahmen|reiniger|matte|ersatz|halterung|ständer|halter)\b", "accessory"),
)


def title_kind(value):
    """Exclude explicit product types/compositions; keep uncertain search results."""
    value = text(value)
    # Feature/compatibility text is not a list of products being sold.
    head = re.split(r"\b(?:für|passend für|geeignet für|kompatibel mit|mit|inklusive|inkl\.|per|via)\b", value, maxsplit=1, flags=re.I)[0]
    dryer, washer = DRYER.search(head), WASHER.search(head)
    for pattern, reason in EXCLUDED:
        hit = re.search(pattern, head, re.I)
        if hit and (dryer is None or hit.start() <= dryer.start()):
            return "exclude", reason
    if dryer and washer:
        first, second = sorted((dryer, washer), key=lambda match: match.start())
        between = head[first.end():second.start()]
        connector = re.search(r"(?<!\+)\+(?!\+)|&|\bund\b", between, re.I)
        bundle = re.search(r"\b(?:set|paket|bundle|kombination)\b", head, re.I)
        if connector or bundle:
            return "exclude", "washer_dryer_bundle"
        return "ambiguous", "multiple_product_types_without_clear_bundle"
    if washer and dryer is None:
        return "exclude", "washer"
    if dryer:
        if re.match(r"[- ](?:untergestell|abdeckung|ersatzfilter|ersatzteil|filter|schlauch|rahmen|sockel|zubehör)\b", head[dryer.end():], re.I):
            return "exclude", "dryer_accessory_compound"
        # A portable appliance is excluded; mobile app/control is a feature.
        property_text = re.sub(r"\bmobil\w*\s+(?:App|Steuerung|Bedienung)\b", "", head, flags=re.I)
        if re.search(EXCLUDED[1][0], property_text, re.I):
            return "exclude", "portable"
        return "include", "standalone_dryer_title"
    return "ambiguous", "product_type_unconfirmed"


def classification_evidence(name, chars=None, infos=None, selection_name=None):
    chars, infos = chars or {}, infos or {}
    sources = [("listing_title", name), ("search_title", selection_name),
               ("comparison_title", chars.get("_name"))]
    sources += [("comparison:" + k, chars.get(k)) for k in TYPE_LABELS]
    sources += [("listing_spec:" + k, infos.get(k)) for k in TYPE_LABELS]
    result = []
    for source, raw in sources:
        if not text(raw):
            continue
        decision, reason = title_kind(raw)
        if source.endswith("Set-Bestandteile"):
            if WASHER.search(text(raw)) and DRYER.search(text(raw)):
                decision, reason = "exclude", "washer_dryer_bundle"
            else:
                decision, reason = "ambiguous", "included_components_not_product_type"
        result.append(dict(source=source, raw=text(raw)[:500], decision=decision, reason=reason))
    return result


def classify(name, chars=None, infos=None, selection_name=None):
    chars, infos = chars or {}, infos or {}
    evidence = classification_evidence(name, chars, infos, selection_name)
    # Explicit sold composition is stronger than a title mentioning only one component.
    bundle = next((e for e in evidence if e["source"].endswith("Set-Bestandteile")
                   and e["decision"] == "exclude"), None)
    if bundle:
        return False, bundle["source"] + ":" + bundle["reason"]
    positive = any(e["decision"] == "include" for e in evidence)
    negative = next((e for e in evidence if e["decision"] == "exclude"), None)
    if positive and negative:
        return True, "ambiguous_included_conflicting_product_types"
    if negative:
        return False, negative["source"] + ":" + negative["reason"]
    if positive:
        titles = [name, selection_name, chars.get("_name")]
        machine_evidence = (any(SPECIFIC_DRYER.search(text(v)) for v in titles)
                            or any(capacity(v) for v in titles)
                            or bool(loading(chars.get("Beladung")))
                            or any(capacity(v, labelled=True) for k, v in {**infos, **chars}.items() if capacity_label(k)))
        return True, "standalone_dryer_evidence" if machine_evidence else "ambiguous_included_generic_dryer"
    return True, "ambiguous_included_product_type"


def public_product_url(value, variation_id):
    parts = urlsplit(urljoin("https://www.otto.de", str(value or "")))
    if parts.hostname != "www.otto.de" or not re.fullmatch(r"/p/[A-Za-z0-9_%./~-]+", parts.path):
        return None
    return urlunsplit(("https", "www.otto.de", parts.path,
                       urlencode({"variationId": variation_id}), ""))


def capacity(raw, *, labelled=False):
    value = text(raw)
    if not value:
        return None
    candidates = []
    for match in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*kg\b", value, re.I):
        prefix = value[max(0, match.start() - 40):match.start()]
        suffix = value[match.end():match.end()+30]
        if (re.search(r"(?:gewicht|weight|waschen|waschkapazität)[^,;]{0,20}$", prefix, re.I)
                or re.match(r"\s*(?:Produktgewicht|Eigengewicht|Gewicht|weight)\b", suffix, re.I)):
            continue
        if re.search(r"^\s*(?:Produktgewicht|Gewicht|Waschkapazität)", value, re.I):
            continue
        candidates.append(match[1].replace(",", "."))
    if not candidates and labelled and re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        candidates = [value.replace(",", ".")]
    values = {format(float(v), "g") for v in candidates if float(v) > 0}
    return next(iter(values)) + " kg" if len(values) == 1 else None


def capacity_label(label):
    key = name_key(label)
    return (not any(w in key for w in ("wasch", "gewicht", "weight"))
            and any(name_key(v) in key for v in CAPACITY_LABELS))


def loading(raw):
    value = text(raw).casefold()
    front = bool(re.search(r"frontlader|frontbeladung|front[- ]?loading|von vorne", value))
    top = bool(re.search(r"toplader|topbeladung|top[- ]?loading|von oben", value))
    return ("Front loader" if front else "Top-loading") if front != top else None


def model_from_name(name):
    value = text(name)
    dryer = DRYER.search(value)
    if dryer:
        value = value[dryer.end():]
    value = re.split(r"[,;]|\b(?:mit|für|inklusive|inkl\.|Frontlader|Toplader)\b", value, maxsplit=1, flags=re.I)[0]
    value = re.split(r"\d+(?:[.,]\d+)?\s*kg\b", value, maxsplit=1, flags=re.I)[0]
    # Series descriptors are not model identifiers, regardless of brand.
    value = re.sub(r"\b[A-Za-z]*\d+[A-Za-z]*\s+Serie\b|\b(?:Serie|Series)\s+[A-Za-z0-9]+\b", " ", value, flags=re.I)
    tokens = text(value).split()
    mixed = [i for i, token in enumerate(tokens)
             if re.fullmatch(r"[A-Za-z0-9/_.+\-]{3,}", token)
             and any(c.isalpha() for c in token) and any(c.isdigit() for c in token)]
    if len(mixed) == 1:
        start = end = mixed[0]
        # A compact uppercase prefix adjacent to the model is part of split models.
        if dryer and start and re.fullmatch(r"[A-Z]{1,6}", tokens[start-1]):
            start -= 1
        while end + 1 < len(tokens) and re.fullmatch(r"[A-Z]{1,3}", tokens[end+1]):
            end += 1
        return clean_model(" ".join(tokens[start:end+1]))
    if not mixed:
        # Split model such as WAM 914 A. Never return a naked number or a series.
        matches = re.findall(r"\b[A-Z]{1,6}\s+\d{2,5}(?:\s+[A-Z]{1,3})?\b", value)
        if len(matches) == 1:
            return clean_model(matches[0])
    # A model before the appliance type is the shared LDY-style last fallback.
    if dryer and not tokens:
        return model_from_name(text(name)[:dryer.start()])
    return None


def savings(raw):
    value = text(raw)
    match = re.fullmatch(r"[-−–+]?\s*(\d+(?:[.,]\d+)?)\s*(%?)", value)
    if not match or not 0 <= float(match[1].replace(",", ".")) <= 100:
        return None
    return match[1] + match[2]


def candidates(target, chars, pdf=None):
    pdf = pdf or {}
    infos = target.get("top_infos") or {}
    caps = [("comparison_title", chars.get("_name"), False),
            ("listing_title", target.get("retailer_sku_name"), False)]
    caps += [("listing_spec:" + k, v, True) for k, v in infos.items() if capacity_label(k)]
    caps += [("comparison_spec:" + k, chars[k], True) for k in CAPACITY_LABELS if chars.get(k)]
    caps += [("product_pdf", pdf.get("capacity_raw") or pdf.get("capacity"), True)]
    loads = [("comparison_title", chars.get("_name")), ("Beladung", chars.get("Beladung")), ("Bauart", chars.get("Bauart"))]
    # Only actually supplied dryer categories. Never query washer categories or infer front.
    loads += [("dryer_category:" + k, chars.get(k) or infos.get(k)) for k in CATEGORY_LABELS
              if DRYER.search(text(chars.get(k) or infos.get(k)))]
    models = [("comparison_model", chars.get("Modellbezeichnung"), clean_model(chars.get("Modellbezeichnung"))),
              ("product_pdf", pdf.get("sku_raw") or pdf.get("sku"), clean_model(pdf.get("sku"))),
              ("listing_title", target.get("retailer_sku_name"), model_from_name(target.get("retailer_sku_name")))]
    return {
        "sku": models,
        "capacity": [(source, raw, capacity(raw, labelled=labelled)) for source, raw, labelled in caps],
        "loading_type": [(source, raw, loading(raw)) for source, raw in loads]
                        + [("product_pdf", pdf.get("loading_type_raw"), pdf.get("loading_type"))],
    }


def choose(target, chars, pdf=None, events=None):
    result = {}
    for field, choices in candidates(target, chars, pdf).items():
        winner = next(((source, raw, value) for source, raw, value in choices if value), None)
        result[field] = winner[2] if winner else None
        if events:
            events.emit("field", item=target["product_id"], variation_id=target["variation_id"], field=field,
                        selected=result[field], source=winner[0] if winner else None,
                        reason=None if winner else "no_valid_value_in_available_sources",
                        candidates=[dict(source=s, raw=r, normalized=v) for s, r, v in choices if r],
                        conflict=bool(winner and any(v and v != winner[2] for _, _, v in choices)))
    return result
