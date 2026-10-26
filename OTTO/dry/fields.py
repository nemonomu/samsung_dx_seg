"""Dryer-only selection and source precedence. No I/O in this module."""
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from common.model_sku import clean_model
from dry.config import CAPACITY_LABELS, CATEGORY_LABELS


def text(value):
    return re.sub(r"\s+", " ", str(value)).strip() if value not in (None, "") else ""


def name_key(value):
    return text(value).casefold()  # same normalization as LDY targets


DRYER = re.compile(r"\b(?:(?:wärme[- ]?pumpen|waerme[- ]?pumpen|kondens(?:ations)?|abluft|wäsche|waesche)[- ]?)?trockner\b", re.I)
WASHER = re.compile(r"\bwasch(?:maschine[n]?|vollautomat(?:en)?)\b", re.I)
# Selection-only forms: do not change DRYER used by SKU and loading extraction.
_SPECIFIC_SELECTION_FORM = r"(?:wärme[- ]?pumpen|waerme[- ]?pumpen|kondens(?:ations)?|abluft)[- ]?(?:(?:wäsche|waesche)[- ]?)?trockner"
_SPECIFIC_SELECTION_DRYER = re.compile(r"\b" + _SPECIFIC_SELECTION_FORM + r"\b", re.I)
_SELECTION_DRYER = re.compile(r"\b(?:" + _SPECIFIC_SELECTION_FORM + r"|(?:wäsche|waesche)[- ]?trockner|trockner)\b", re.I)
_TRAVEL_DRYER = r"\b(?:reise[- ]?|camping[- ]?)(?:wäsche)?trockner\b"
EXCLUDED = (
    (r"\bwasch[- ]?trockner\b", "washer_dryer"),
    (_TRAVEL_DRYER, "portable"),
    (r"\b(?:haartrockner|haarföhn|föhn|foehn|multi[- ]?haarstyler|haarstyler)\b", "hair_dryer"),
    (r"\b(?:dörrautomat|doerrautomat|lebensmitteltrockner|obsttrockner)\b", "food_dehydrator"),
    (r"\b(?:luftentfeuchter|bautrockner)\b", "dehumidifier"),
    (r"\b(?:waschmaschinenumbauschrank|waschmaschinenschrank)\b", "furniture"),
    (r"\b(?:wäscheschleuder|waescheschleuder|schleuder|zentrifuge)\b", "spin_dryer"),
    (r"\b(?:wäscheständer|waeschestaender|flügelwäschetrockner|standtrockner|wandtrockner|wäscheleine|deckentrockner|turmtrockner|trockengestell|trockensack|trockenschrank)\b", "drying_rack_or_cabinet"),
    (r"\b(?:dampfschrank|kleiderpflegeschrank|kleiderschrank|dampfglätter|kleiderdampfer|styler|airdresser)\b", "garment_care"),
    (r"\b\w*(?:untergestell|unterschrank|sockel|zwischenbaurahmen|verbindungsrahmen|stapelrahmen|trocknerbälle|trocknerball|ersatzteil|ersatzfilter|flusensieb|türgriff|schlauch|abdeckung|antivibrationsmatte|zubehör)\w*\b|\b(?:filter|gestell|rahmen|reiniger|matte|ersatz|halterung|ständer|halter)\b", "accessory"),
)
# These nouns identify a sold object, not a feature after 'mit/inklusive/...'.
_SOLD_TYPES = (
    (r"\b(?:heizstab|heizregister|thermodetektor|temperaturfühler|stellfuß|stellfuss|vibrationsdämpfer|schwingungsdämpfer|schutzhülle|schützhülle)\b|\b(?:sensor|griff)\s+(?:wie|für|fuer)\b|\b(?:wäsche|waesche)trockner[- ]kondensator\b", "sold_accessory"),
    (r"\b(?:kompressor|schraubenkompressor)\b", "sold_compressor"),
    (r"\b(?:filament|filatment|teppich|hundepflege|haustier)[- ]?trockner\b|\bfilament[- ]?dryer\b|\btrockner\s+(?:für|fuer)\s+(?:(?:den|die|das)\s+)?(?:helm(?:e)?|stiefel|filamente?|haustiere?|hunde|katzen|teppiche?)\b", "sold_non_clothing_dryer"),
    (r"\b(?:unterziehshirt|unterhemd|füllwatte|bastelfilz|standregal|dusch[- ]wc|hänge[- ]?wc|bidet|haargel|3d[- ]drucker|hundeschermaschine)\b", "sold_unrelated_product"),
    # A carpet air mover may put its purpose after speed/features in the title.
    (r"\b(?:elektro[- ]gebläse|air\s+mover)\b", "sold_non_clothing_dryer"),
)
_SOLD_REASONS = {reason for _, reason in _SOLD_TYPES} | {"accessory", "drying_rack_or_cabinet", "dryer_accessory_compound", "heated_drying_rack"}
_PORTABLE_WORD = r"(?:tragbar\w*|portabel\w*|portabl\w*|mobil(?:e[rsmn]?)?)"
_HEATED_RACK = re.compile(r"\bheiz[- ]?(?:(?:wäsche|waesche)[- ]?)?trockner\s+mit\s+fl(?:ü|ue)geln\b", re.I)
# Measured/dimensional drying features are not title-only model identifiers.
_NAME_FEATURE = re.compile(r"\b(?:(?:\d+(?:[.,]\d+)?[- ]?(?:grad|minuten?|stunden?|stufen?)|\d+d)"
                           r"[- ]+(?:\w*trocknung|luftstrom|luftzirkulation)"
                           r"|\d+(?:[.,]\d+)?[- ]?(?:watt|kw|w|db)(?:[- ]+(?:heizleistung|leistung))?(?![-\w])"
                           r"|\d+[- ]?(?:programme?|heizstufen?))\b", re.I)


def product_phrase(value):
    """Keep sold purpose ('für Helm'), stop at features/compatibility clauses."""
    return re.split(r"\b(?:(?:mit|inklusive|passend|geeignet|kompatibel|ohne|per|via)\b|inkl\.(?=\s|$))",
                    value, maxsplit=1, flags=re.I)[0]


def specific_appliance(value):
    # 'Heizstab wie Brand Wärmepumpentrockner' describes a replacement part.
    head = re.split(r"\b(?:für|fuer|wie)\b", product_phrase(value), maxsplit=1, flags=re.I)[0]
    return bool(_SPECIFIC_SELECTION_DRYER.search(head))


def accessory_description(value, dryer, accessory_start):
    """Care/delivery wording after an appliance is not a clear sale of a part."""
    if not dryer or accessory_start < dryer.end():
        return False
    # A connected product noun such as 'Trockner-Filter' still names a part.
    if value[dryer.end():accessory_start].strip() == "-":
        return False
    # Keep the accessory's own clause, including a postposed 'inklusive' that
    # product_phrase() cuts off. Do not borrow another feature's care claim.
    left = re.split(r"[,;|]", value[dryer.end():accessory_start])[-1]
    right = re.split(r"[,;|]", value[accessory_start:], maxsplit=1)[0]
    clause = left + right
    return bool(re.search(r"\b(?:leicht|einfach)\s+zu\s+reinig(?:en|end\w*)\b"
                          r"|\b(?:inklusive|inbegriffen|enthalten|beiliegend\w*)\b|\binkl\.(?=\s|$)", clause, re.I))


def portable_appliance(value):
    """Bind a portable adjective to the appliance, not any other noun in its title."""
    for dryer in _SELECTION_DRYER.finditer(value):
        # Adjectives immediately modifying the appliance; no arbitrary noun gap.
        before = re.search(r"\b" + _PORTABLE_WORD
                           + r"(?:\s+(?:elektrisch\w*|kompakt\w*|klein\w*))*[- ]+$",
                           value[:dryer.start()], re.I)
        if before and not re.search(r"\b(?:nicht|kein\w*)\s*$", value[:before.start()], re.I):
            return True
        # Postposed property: 'Trockner DR900, portabel'. An ensuing noun such as
        # 'Fernsteuerung' or 'App' instead makes the adjective describe that noun.
        after = re.match(r"\s*(?:(?=[A-Z0-9/_.+-]*\d)[A-Z0-9/_.+-]+\s*)?[,;: -]*"
                         + r"(?:ist\s+)?" + _PORTABLE_WORD + r"\b\s*(?=$|[,;])",
                         value[dryer.end():], re.I)
        if after:
            return True
    return False


def explicit_product_exclusion(value):
    # Inspect the sold product phrase before feature/compatibility clauses. Keep
    # 'für' here: it is part of the actual type 'Lüfter für Wäscheständer'.
    head = product_phrase(value)
    fan = re.search(r"\b(?:lüfter|luefter|ventilator|gebläse|geblaese)\s+(?:für|fuer)\s+(?:den\s+|einen\s+)?(?:wäscheständer|waeschestaender)\b", head, re.I)
    if fan and not re.search(r"\b(?:nicht|kein\w*)\s*$", head[:fan.start()], re.I):
        return "explicit_drying_rack_fan"
    head = re.split(r"\b(?:für|fuer|wie)\b", head, maxsplit=1, flags=re.I)[0]
    if portable_appliance(head) or re.search(_TRAVEL_DRYER, head, re.I):
        return "explicit_portable_dryer"
    # A later independent product phrase may follow features ('mit Timer, ...').
    # Keep compatibility/delivery lists protected across comma-separated clauses.
    protected = False
    for clause in re.split(r"[,;|]", value):
        head = re.split(r"\b(?:für|fuer|wie)\b", product_phrase(clause), maxsplit=1, flags=re.I)[0]
        if not protected and (portable_appliance(head) or re.search(_TRAVEL_DRYER, head, re.I)):
            return "explicit_portable_dryer"
        protected = protected or bool(re.search(
            r"\b(?:für|fuer|wie|passend|geeignet|kompatibel|inklusive|inbegriffen|beiliegend\w*)\b|\binkl\.",
            clause, re.I))
    return None


def heated_rack_kind(value):
    """Keep the type's own 'mit Flügeln', without searching feature clauses as goods."""
    for hit in _HEATED_RACK.finditer(value):
        prefix, suffix = value[:hit.start()], value[hit.end():]
        if product_phrase(prefix) != prefix or re.search(r"\b(?:nicht|kein\w*)\s*$", prefix, re.I):
            continue
        dryer = _SELECTION_DRYER.search(prefix)
        if dryer and re.match(r"\s*(?:im\s+Lieferumfang\s+)?(?:inklusive|inkl\.|inbegriffen|enthalten|beiliegend\w*)"
                              r"\s*(?=$|[,;])", suffix, re.I):
            continue
        if specific_appliance(prefix + suffix) or (dryer and re.search(r"[,;]", prefix[dryer.end():])):
            return "ambiguous", "conflicting_product_types"
        return "exclude", "heated_drying_rack"
    return None


def title_kind(value):
    """Exclude explicit product types/compositions; keep uncertain search results."""
    value = text(value)
    explicit = explicit_product_exclusion(value)
    if explicit:
        return "exclude", explicit
    rack = heated_rack_kind(value)
    if rack:
        return rack
    # Feature/compatibility text is not a list of products being sold.
    phrase = product_phrase(value)
    head = re.split(r"\b(?:(?:für|fuer|passend für|geeignet für|kompatibel mit|mit|inklusive|ohne|per|via)\b|inkl\.(?=\s|$))",
                    value, maxsplit=1, flags=re.I)[0]
    dryer, washer = _SELECTION_DRYER.search(head), WASHER.search(head)
    has_accessory_description = False
    # Explicit accessory compounds are sold parts even when preceded by a type.
    compound = re.match(r"[- ](untergestell|abdeckung|ersatzfilter|ersatzteil|filter|schlauch|rahmen|sockel|zubehör)\b",
                        head[dryer.end():], re.I) if dryer else None
    if compound:
        if compound[1].casefold() != "filter" or not accessory_description(value, dryer, dryer.end() + compound.start(1)):
            return "exclude", "dryer_accessory_compound"
    for pattern, reason in _SOLD_TYPES:
        hit = re.search(pattern, phrase, re.I)
        if hit and hit.start() < len(head):
            if specific_appliance(head) or (dryer and hit.start() > dryer.end()
                                           and re.search(r"[,;]", head[dryer.end():hit.start()])):
                # An unintroduced feature after a dryer title is not a clear sale of a part.
                return "ambiguous", "conflicting_product_types"
            return "exclude", reason
    for pattern, reason in EXCLUDED:
        hit = re.search(pattern, head, re.I)
        if hit:
            if reason == "accessory" and accessory_description(value, dryer, hit.start()):
                has_accessory_description = True
                continue
            if reason in _SOLD_REASONS:
                if specific_appliance(head):
                    return "ambiguous", "conflicting_product_types"
                return "exclude", reason
            if dryer is None or hit.start() <= dryer.start():
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
    if has_accessory_description:
        return "ambiguous", "accessory_description"
    if dryer:
        return "include", "standalone_dryer_title"
    return "ambiguous", "product_type_unconfirmed"


def classification_evidence(name, *, selection_name=None):
    """One listing title only: prefer the original long search title."""
    raw = text(selection_name) or text(name)
    source = "search_title" if text(selection_name) else "listing_title"
    decision, reason = title_kind(raw)
    return [dict(source=source, raw=raw[:500], decision=decision, reason=reason)]


def classify(name, *, selection_name=None):
    evidence = classification_evidence(name, selection_name=selection_name)[0]
    decision, reason = evidence["decision"], evidence["reason"]
    if decision == "exclude":
        return False, evidence["source"] + ":" + reason
    if decision == "ambiguous":
        return True, "ambiguous_included_" + ("product_type" if reason == "product_type_unconfirmed" else reason)
    raw = text(selection_name) or text(name)
    machine_evidence = specific_appliance(raw) or capacity(raw)
    return True, "standalone_dryer_evidence" if machine_evidence else "ambiguous_included_generic_dryer"


def public_product_url(value, variation_id):
    parts = urlsplit(urljoin("https://www.otto.de", str(value or "")))
    if parts.hostname != "www.otto.de" or not re.fullmatch(r"/p/[A-Za-z0-9_%./~-]+", parts.path):
        return None
    return urlunsplit(("https", "www.otto.de", parts.path,
                       urlencode({"variationId": variation_id}), ""))


def capacity_candidate(raw, *, labelled=False):
    """Return a value and rejection reason, retaining ambiguous numeric context."""
    value = text(raw)
    if not value:
        return None, "no_valid_capacity"
    if labelled and re.fullmatch(r"[\d.,]+(?:\s+[\d.,]+)+", value):
        return None, "ambiguous_numeric_capacity"
    if labelled and re.fullmatch(r"\d[\d.,]*(?:\s*kg)?(?:\s*(?:[-–—~/]|\bbis\b|\bto\b)\s*\d[\d.,]*(?:\s*kg)?)+", value, re.I):
        return None, "ambiguous_numeric_capacity"
    candidates = []
    ambiguous = False
    for match in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*kg\b", value, re.I):
        prefix = value[max(0, match.start() - 40):match.start()]
        suffix = value[match.end():match.end()+30]
        preceding_number = re.search(r"(?<![\w.,])(?=\d)[\d.,]+\s+$", prefix)
        model_number = (re.search(r"\b(?:Serie|Series|Modell(?:bezeichnung)?|Model(?: identifier)?)\s+\S+\s*(?:[-–—]\s*)?$", prefix, re.I)
                        or re.search(r"\b[A-Z]{1,6}\s+\d{2,5}[,;]?\s*(?:[-–—]\s*)?$", prefix))
        range_before = re.search(r"\b\d[\d.,]*(?:\s*kg)?\s*(?:[-–—~/]|\bbis\b|\bto\b)\s*$", prefix, re.I)
        range_after = re.match(r"\s*(?:[-–—~/]|\bbis\b|\bto\b)\s*\d", suffix, re.I)
        if (range_before and not model_number) or range_after:
            ambiguous = True
            continue
        if preceding_number and not model_number:
            ambiguous = True
            continue
        if (re.search(r"(?:gewicht|weight|waschen|waschkapazität)[^,;]{0,20}$", prefix, re.I)
                or re.match(r"\s*(?:Produktgewicht|Eigengewicht|Gewicht|weight)\b", suffix, re.I)):
            continue
        if re.search(r"^\s*(?:Produktgewicht|Gewicht|Waschkapazität)", value, re.I):
            continue
        candidates.append(match[1].replace(",", "."))
    if not candidates and labelled and re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        candidates = [value.replace(",", ".")]
    values = {format(float(v), "g") for v in candidates if float(v) > 0}
    if len(values) == 1:
        return next(iter(values)) + " kg", None
    return None, "ambiguous_numeric_capacity" if ambiguous else "multiple_capacity_values" if len(values) > 1 else "no_valid_capacity"


def capacity(raw, *, labelled=False):
    return capacity_candidate(raw, labelled=labelled)[0]


def capacity_label(label):
    key = name_key(label)
    return (not any(w in key for w in ("wasch", "gewicht", "weight"))
            and any(name_key(v) in key for v in CAPACITY_LABELS))


def loading(raw):
    value = text(raw).casefold()
    front = bool(re.search(r"frontlader|frontbeladung|front[- ]?(?:loading|loaded)|von vorne", value))
    top = bool(re.search(r"toplader|topbeladung|top[- ]?(?:loading|loaded)|von oben", value))
    return ("Front loader" if front else "Top-loading") if front != top else None


def valid_sku(raw):
    value = clean_model(text(raw))
    return value if value and len(value) > 3 else None


def sku_rejection(raw, *, from_name=False):
    if from_name and _NAME_FEATURE.search(text(raw)):
        return "sku_feature_description"
    value = clean_model(model_from_name(raw) if from_name else text(raw))
    return "sku_too_short" if value and len(value) <= 3 else "no_valid_model"


def model_from_name(name):
    value = text(name)
    dryer = DRYER.search(value)
    if dryer:
        value = value[dryer.end():]
    value = re.split(r"[,;]|\b(?:mit|für|inklusive|inkl\.|Frontlader|Toplader)\b", value, maxsplit=1, flags=re.I)[0]
    value = re.split(r"\d+(?:[.,]\d+)?\s*kg\b", value, maxsplit=1, flags=re.I)[0]
    # Series descriptors are not model identifiers, regardless of brand.
    value = re.sub(r"\b[A-Za-z]*\d+[A-Za-z]*\s+Serie\b|\b(?:Serie|Series)\s+[A-Za-z0-9]+\b", " ", value, flags=re.I)
    value = _NAME_FEATURE.sub(" ", value)
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
            ("listing_title", target.get("retailer_sku_name"), False),
            ("search_title", target.get("selection_name"), False)]
    caps += [("listing_spec:" + k, v, True) for k, v in infos.items() if capacity_label(k)]
    caps += [("comparison_spec:" + k, chars[k], True) for k in CAPACITY_LABELS if chars.get(k)]
    caps += [("product_pdf", pdf.get("capacity_raw") or pdf.get("capacity"), True)]
    loads = [("comparison_title", chars.get("_name")), ("Beladung", chars.get("Beladung")), ("Bauart", chars.get("Bauart"))]
    # Only actually supplied dryer categories. Never query washer categories or infer front.
    loads += [("dryer_category:" + k, chars.get(k) or infos.get(k)) for k in CATEGORY_LABELS
              if DRYER.search(text(chars.get(k) or infos.get(k)))]
    models = [("comparison_model", chars.get("Modellbezeichnung"), valid_sku(chars.get("Modellbezeichnung"))),
              ("product_pdf", pdf.get("sku_raw") or pdf.get("sku"), valid_sku(pdf.get("sku"))),
              ("listing_title", target.get("retailer_sku_name"), valid_sku(model_from_name(target.get("retailer_sku_name"))))]
    return {
        "sku": models,
        "capacity": [(source, raw, capacity(raw, labelled=labelled)) for source, raw, labelled in caps],
        "loading_type": [(source, raw, loading(raw)) for source, raw in loads]
                        + [("product_pdf", pdf.get("loading_type_raw"), pdf.get("loading_type"))],
    }


def choose(target, chars, pdf=None, events=None, *, options=None):
    result = {}
    for field, choices in (candidates(target, chars, pdf) if options is None else options).items():
        winner = next(((source, raw, value) for source, raw, value in choices if value), None)
        result[field] = winner[2] if winner else None
        if events:
            events.emit("field", item=target["product_id"], variation_id=target["variation_id"], field=field,
                        selected=result[field], source=winner[0] if winner else None,
                        reason=None if winner else "no_valid_value_in_available_sources",
                        candidates=[dict(source=s, raw=r, normalized=v, **(
                            {"reason": sku_rejection(r, from_name=s == "listing_title") if field == "sku"
                             else capacity_candidate(r, labelled=s.startswith(("listing_spec:", "comparison_spec:")) or s == "product_pdf")[1]}
                            if field in {"sku", "capacity"} and not v else {})) for s, r, v in choices if r],
                        conflict=bool(winner and any(v and v != winner[2] for _, _, v in choices)))
    return result
