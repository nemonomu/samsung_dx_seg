"""Amazon.de HTML parsers for listing, BSR, PDP, and review pages."""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from common.config import AMAZON_BASE
from common.translations import resolve_ref_refrigerator_type, translate_field


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", unescape(str(value))).strip()
    return text or None


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    abs_url = urljoin(AMAZON_BASE, url)
    parsed = urlsplit(abs_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def asin_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", url)
    return match.group(1) if match else None



def product_url_for_asin(href: str | None, asin: str | None) -> str | None:
    """Prefer the stable PDP URL when Amazon gives a sponsored click URL."""
    url = canonical_url(href if href else f"/dp/{asin}" if asin else None)
    if asin and asin_from_url(url) != asin:
        return canonical_url(f"/dp/{asin}")
    return url

def _price_text(root) -> str | None:
    offscreen = root.select_one(".a-price .a-offscreen")
    if offscreen:
        return clean_text(offscreen.get_text(" "))
    whole = clean_text(root.select_one(".a-price-whole").get_text(" ") if root.select_one(".a-price-whole") else None)
    frac = clean_text(root.select_one(".a-price-fraction").get_text(" ") if root.select_one(".a-price-fraction") else None)
    if whole:
        return whole + ("," + frac if frac else "") + " €"
    return None


def _original_price(root) -> str | None:
    for selector in (".a-price.a-text-price .a-offscreen", ".a-text-price .a-offscreen"):
        node = root.select_one(selector)
        if node:
            return clean_text(node.get_text(" "))
    return None


def _rating_text(root) -> str | None:
    node = root.select_one("i.a-icon-star-small span.a-icon-alt, i.a-icon-star span.a-icon-alt")
    return clean_text(node.get_text(" ")) if node else None


def _rating_count(root) -> str | None:
    node = root.select_one("span[aria-label][class*='a-size-base']")
    if node:
        label = clean_text(node.get("aria-label"))
        if label and re.search(r"\d", label):
            return label
    for node in root.select("a[href*='customerReviews'] span, span.a-size-base.s-underline-text"):
        text = clean_text(node.get_text(" "))
        if text and re.search(r"\d", text):
            return text
    return None


_INVENTORY_STATUS_RE = re.compile(
    r"\b(?:Nur noch\s+\d+\s+(?:auf Lager|in stock)|Only\s+\d+\s+left\s+in\s+stock)\b",
    re.I,
)


def _inventory_status_text(root) -> str | None:
    text = clean_text(root.get_text(" "))
    if not text:
        return None
    match = _INVENTORY_STATUS_RE.search(text)
    return match.group(0) if match else None


_INVISIBLE_RE = re.compile(r"[\u200e\u200f\u200b\xa0]+")
_KEY_TRANS = str.maketrans({
    "\u00e4": "ae", "\u00c4": "ae", "\u00f6": "oe", "\u00d6": "oe",
    "\u00fc": "ue", "\u00dc": "ue", "\u00df": "ss",
})


def _fact_clean(value: Any) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = _INVISIBLE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" :\uff1a-\u200e\u200f")
    return text or None


def _fact_key(value: Any) -> str:
    text = _fact_clean(value) or ""
    text = text.translate(_KEY_TRANS).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _add_fact(facts: dict[str, str], key: Any, value: Any) -> None:
    k = _fact_clean(key)
    v = _fact_clean(value)
    if not k or not v or _fact_key(k) == _fact_key(v):
        return
    facts.setdefault(k, v)


def _collect_product_facts(soup: BeautifulSoup) -> dict[str, str]:
    facts: dict[str, str] = {}
    row_selectors = ", ".join([
        "#productOverview_feature_div tr",
        "#poExpander tr",
        "#productFactsDesktopExpander tr",
        "#productFactsDesktop_feature_div tr",
        "#productDetails_expanderTables_depthLeftSections tr",
        "#productDetails_expanderTables_depthRightSections tr",
        "#productDetails_techSpec_section_1 tr",
        "#productDetails_detailBullets_sections1 tr",
        "#technicalSpecifications_feature_div tr",
        "#prodDetails tr",
        "#tech tr",
        "table.a-keyvalue tr",
        "table.prodDetTable tr",
    ])
    for row in soup.select(row_selectors):
        cells = [_fact_clean(c.get_text(" ")) for c in row.select("th,td")]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            _add_fact(facts, cells[0], cells[-1])

    for li in soup.select("#detailBullets_feature_div li"):
        key_node = li.select_one("span.a-text-bold")
        text = _fact_clean(li.get_text(" "))
        if key_node and text:
            key = _fact_clean(key_node.get_text(" "))
            value = text
            if key and value.startswith(key):
                value = value[len(key):]
            _add_fact(facts, key, value)
            continue
        if text and re.search(r"[:\uff1a]", text):
            key, value = re.split(r"\s*[:\uff1a]\s*", text, maxsplit=1)
            _add_fact(facts, key, value)
    return facts



_FACT_ROW_SELECTORS = ", ".join([
    "#productOverview_feature_div tr",
    "#poExpander tr",
    "#productFactsDesktopExpander tr",
    "#productFactsDesktop_feature_div tr",
    "#productDetails_expanderTables_depthLeftSections tr",
    "#productDetails_expanderTables_depthRightSections tr",
    "#productDetails_techSpec_section_1 tr",
    "#productDetails_detailBullets_sections1 tr",
    "#technicalSpecifications_feature_div tr",
    "#prodDetails tr",
    "#tech tr",
    "table.a-keyvalue tr",
    "table.prodDetTable tr",
])


def _append_unique(values: list[str], value: Any) -> None:
    text = _fact_clean(value)
    if text and text not in values:
        values.append(text)


def _fact_values_from_soup(soup: BeautifulSoup, *keys: str) -> list[str]:
    wanted = {_fact_key(key) for key in keys if _fact_key(key)}
    values: list[str] = []
    if not wanted:
        return values
    for row in soup.select(_FACT_ROW_SELECTORS):
        cells = [_fact_clean(c.get_text(" ")) for c in row.select("th,td")]
        cells = [c for c in cells if c]
        if len(cells) >= 2 and _fact_key(cells[0]) in wanted:
            _append_unique(values, cells[-1])
    for li in soup.select("#detailBullets_feature_div li"):
        key_node = li.select_one("span.a-text-bold")
        text = _fact_clean(li.get_text(" "))
        if key_node and text:
            key = _fact_clean(key_node.get_text(" "))
            value = text
            if key and value.startswith(key):
                value = value[len(key):]
            if _fact_key(key) in wanted:
                _append_unique(values, value)
            continue
        if text and re.search(r"[:\uff1a]", text):
            key, value = re.split(r"\s*[:\uff1a]\s*", text, maxsplit=1)
            if _fact_key(key) in wanted:
                _append_unique(values, value)
    return values


def _detail_asin_from_page(soup: BeautifulSoup, facts: dict[str, str | None]) -> str | None:
    for node in soup.select("input#ASIN, #averageCustomerReviews[data-asin], #cerberus-data-metrics[data-asin]"):
        value = node.get("value") or node.get("data-asin")
        text = clean_text(value)
        if text and re.fullmatch(r"[A-Z0-9]{10}", text):
            return text
    asin = first_by_key(facts, "ASIN")
    if asin and re.fullmatch(r"[A-Z0-9]{10}", asin.strip()):
        return asin.strip()
    return None


def _is_asin_sku_candidate(value: Any, page_asin: str | None) -> bool:
    text = clean_text(value)
    if not text:
        return False
    text = text.strip().upper()
    if page_asin and text == page_asin.strip().upper():
        return True
    return bool(re.fullmatch(r"B0[A-Z0-9]{8}", text))


_TV_SKU_KEY_PRIORITY = (
    ("Hersteller-Modellnummer",),
    ("Manufacturer Model Number",),
    ("Modellnummer",),
    ("Model Number",),
    ("Item Model Number", "Item model number"),
    ("SKU Number",),
    ("Herstellerreferenz",),
    ("Manufacturer reference",),
    ("Mfr Part Number",),
    ("Manufacturer Part Number",),
    ("Hersteller-Teilenummer",),
    ("Part Number",),
    ("Item Part Number", "Item part number"),
    ("Teilenummer",),
    ("Artikelnummer",),
    ("Modellname",),
    ("Model Name",),
)

_REF_SKU_KEY_PRIORITY = (
    ("Modellnummer",),
    ("Model Number",),
    ("Item Model Number", "Item model number"),
    ("Hersteller-Modellnummer",),
    ("Manufacturer Model Number",),
    ("SKU Number",),
    ("Herstellerreferenz",),
    ("Manufacturer reference",),
    ("Mfr Part Number",),
    ("Manufacturer Part Number",),
    ("Hersteller-Teilenummer",),
    ("Part Number",),
    ("Item Part Number", "Item part number"),
    ("Teilenummer",),
    ("Artikelnummer",),
    ("Modellname",),
    ("Model Name",),
)

_MODEL_NAME_KEYS = (("Modellname",), ("Model Name",))


def _first_sku_for_priority(
    soup: BeautifulSoup,
    page_asin: str | None,
    priority: tuple[tuple[str, ...], ...],
) -> str | None:
    for keys in priority:
        for value in _fact_values_from_soup(soup, *keys):
            if not _is_asin_sku_candidate(value, page_asin):
                return value
    return None


def _first_sku_value(
    soup: BeautifulSoup,
    facts: dict[str, str | None],
    *,
    product: str | None = None,
) -> str | None:
    page_asin = _detail_asin_from_page(soup, facts)
    priority = _REF_SKU_KEY_PRIORITY if str(product or "").upper() == "REF" else _TV_SKU_KEY_PRIORITY
    sku = _first_sku_for_priority(soup, page_asin, priority)
    if sku and "BNDL_" in sku.upper():
        return _first_sku_for_priority(soup, page_asin, _MODEL_NAME_KEYS)
    return sku


def _screen_size_from_text(*values: Any) -> str | None:
    patterns = (
        r"(\d{2,3}(?:[,.]\d+)?)\s*(?:zoll|inch(?:es)?|[\"\u201d])",
        r"(?:zoll|inch(?:es)?)\s*(\d{2,3}(?:[,.]\d+)?)",
    )
    for value in values:
        text = _fact_clean(value)
        if not text:
            continue
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                size = match.group(1).replace(",", ".")
                try:
                    numeric = float(size)
                except ValueError:
                    continue
                if 10 <= numeric <= 150:
                    return f"{size} inches"
    return None


def _model_year_from_text(*values: Any) -> str | None:
    for value in values:
        text = _fact_clean(value)
        if not text:
            continue
        match = re.search(r"(?:modelljahr|model\s+year)[^0-9]{0,30}(20[0-3]\d)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        paren_match = re.search(r"[\[(]\s*(20[0-3]\d)\s*[\])]", text)
        if paren_match:
            return paren_match.group(1)
    return None


_REF_TITLE_CAPACITY_RE = re.compile(
    r"(?<![\w.,])(\d{1,4}(?:[.,]\d+)?)\s*(l|liter)\b",
    flags=re.IGNORECASE,
)
_REF_COOLING_CONTEXT_RE = re.compile(
    r"(?:(?:k(?:ü|ue)hl(?:en|teil|raum|fach|volumen|bereich|zone)\b|k(?:ü|ue)hl\b)|"
    r"refrigerator\s+(?:compartment|capacity|volume)|fridge\s+(?:compartment|capacity|volume))",
    flags=re.IGNORECASE,
)
_REF_FREEZER_CONTEXT_RE = re.compile(
    r"(?:gefrier(?:en|teil|raum|fach|volumen|bereich|zone)?|freezer)",
    flags=re.IGNORECASE,
)


def _ref_capacity_from_title(value: Any) -> str | None:
    """Use one title capacity, preferring an explicitly labelled refrigerator capacity."""
    text = _fact_clean(value)
    if not text:
        return None
    matches = list(_REF_TITLE_CAPACITY_RE.finditer(text))
    if not matches:
        return None
    if len(matches) == 1:
        match = matches[0]
        return f"{match.group(1)} {match.group(2)}"
    for match in matches:
        start, end = match.span()
        context = text[max(0, start - 14) : min(len(text), end + 14)]
        if _REF_COOLING_CONTEXT_RE.search(context) and not _REF_FREEZER_CONTEXT_RE.search(context):
            return f"{match.group(1)} {match.group(2)}"
    return None


def parse_listing_html(html: str, *, page: int, sort: str, start_rank: int = 1) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    rows: list[dict[str, Any]] = []
    rank = start_rank
    for item in soup.select("div[data-component-type='s-search-result'][data-asin]"):
        asin = clean_text(item.get("data-asin"))
        if not asin:
            continue
        link = item.select_one("a.a-link-normal.s-no-outline[href], h2 a[href], a[href*='/dp/']")
        name_node = item.select_one("h2 span, h2")
        url = product_url_for_asin(link.get("href") if link else None, asin)
        row = {
            "source": sort,
            "page": page,
            "asin": asin,
            "item": asin,
            "product_url": url,
            "retailer_sku_name": clean_text(name_node.get_text(" ")) if name_node else None,
            "final_sku_price": _price_text(item),
            "original_sku_price": _original_price(item),
            "discount_type": translate_field("discount_type", clean_text(item.select_one(".a-badge-text").get_text(" ") if item.select_one(".a-badge-text") else None)),
            "sku_popularity": translate_field("sku_popularity", clean_text(item.select_one(".a-badge-label .a-badge-text, .a-badge-label").get_text(" ") if item.select_one(".a-badge-label .a-badge-text, .a-badge-label") else None)),
            "number_of_units_purchased_past_month": clean_text(item.select_one("span.a-size-base.a-color-secondary").get_text(" ") if item.select_one("span.a-size-base.a-color-secondary") else None),
            "sku_status": "Sponsored" if (item.select_one(".puis-sponsored-label-text") or "Gesponsert" in item.get_text(" ")) else None,
            "inventory_status": translate_field("inventory_status", _inventory_status_text(item)),
            "star_rating": _rating_text(item),
            "count_of_star_ratings": _rating_count(item),
        }
        row["main_rank" if sort == "main" else "bsr_rank"] = rank
        rows.append(row)
        rank += 1
    return rows


def parse_bsr_html(html: str, *, start_rank: int = 1) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "lxml")
    rows: list[dict[str, Any]] = []
    rank = start_rank
    selectors = [
        "div.p13n-gridRow div[id^='gridItemRoot']",
        "div[data-client-recs-list] div[id^='gridItemRoot']",
        "li.zg-item-immersion",
    ]
    seen: set[str] = set()
    for selector in selectors:
        for item in soup.select(selector):
            link = item.select_one("a[href*='/dp/'], a[href*='/gp/product/']")
            url = canonical_url(link.get("href") if link else None)
            asin = clean_text(item.get("data-asin")) or asin_from_url(url)
            if asin and asin_from_url(url) != asin:
                url = canonical_url(f"/dp/{asin}")
            if not asin or asin in seen:
                continue
            seen.add(asin)
            rank_node = item.select_one(".zg-bdg-text")
            rank_text = clean_text(rank_node.get_text(" ")) if rank_node else None
            parsed_rank = None
            if rank_text:
                m = re.search(r"(\d+)", rank_text)
                parsed_rank = int(m.group(1)) if m else None
            row = {
                "source": "bsr",
                "asin": asin,
                "item": asin,
                "product_url": url,
                "bsr_rank": parsed_rank or rank,
            }
            rows.append(row)
            rank = max(rank + 1, (parsed_rank or rank) + 1)
    return rows


def _direct_text(node: Any) -> str | None:
    strings = []
    for child in getattr(node, "contents", []) or []:
        if isinstance(child, str):
            strings.append(child)
    return clean_text(" ".join(strings))


def _sp_detail2_title_value(node: Any) -> str | None:
    divs = [child for child in getattr(node, "children", []) if getattr(child, "name", None) == "div"]
    if len(divs) >= 2:
        text = _direct_text(divs[1]) or clean_text(divs[1].get_text(" "))
        if text:
            return text
    return clean_text(node.get("title") or node.get("aria-label"))


def _extract_sp_detail2_titles_from_soup(soup: BeautifulSoup, *, limit: int = 20) -> list[str]:
    titles: list[str] = []
    for node in soup.select("#sp_detail2 [id^='sp_detail2_'][id$='_title']"):
        text = _sp_detail2_title_value(node)
        if text and text not in titles:
            titles.append(text)
        if len(titles) >= limit:
            break
    return titles


def extract_sp_detail2_titles(html: str, *, limit: int = 20) -> list[str]:
    soup = BeautifulSoup(html or "", "lxml")
    return _extract_sp_detail2_titles_from_soup(soup, limit=limit)


def parse_product_detail_html(html: str, *, product: str | None = None) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    data: dict[str, Any] = {}
    title = soup.select_one("#productTitle")
    if title:
        data["retailer_sku_name"] = clean_text(title.get_text(" "))
    data["final_sku_price"] = _price_text(soup) or data.get("final_sku_price")
    data["original_sku_price"] = _original_price(soup)
    data["star_rating"] = _rating_text(soup)
    rating_count = soup.select_one("#acrCustomerReviewText")
    if rating_count:
        data["count_of_star_ratings"] = clean_text(rating_count.get_text(" "))
    availability = soup.select_one("#availability, #availabilityInsideBuyBox_feature_div")
    data["inventory_status"] = translate_field("inventory_status", clean_text(availability.get_text(" ")) if availability else None)
    delivery = soup.select_one("#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE, #deliveryBlockMessage")
    fastest = soup.select_one("#mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE")
    data["delivery_availability"] = translate_field("delivery_availability", clean_text(delivery.get_text(" ")) if delivery else None)
    data["fastest_delivery"] = translate_field("fastest_delivery", clean_text(fastest.get_text(" ")) if fastest else None)
    bought = soup.select_one("#social-proofing-faceout-title-tk_bought")
    data["number_of_units_purchased_past_month"] = clean_text(bought.get_text(" ")) if bought else None

    facts = _collect_product_facts(soup)
    fact_text = " | ".join([*facts.keys(), *facts.values()])
    data["facts_json"] = json.dumps(facts, ensure_ascii=False)
    data["sku"] = _first_sku_value(soup, facts, product=product)
    data["model_year"] = first_by_key(facts, "Modelljahr", "Model Year") or _model_year_from_text(data.get("retailer_sku_name"), fact_text)
    data["screen_size"] = first_by_key(
        facts,
        "Bildschirmgr\u00f6\u00dfe", "Bildschirmgroesse", "Bildschirmdiagonale", "Displaygr\u00f6\u00dfe", "Displaygroesse",
        "Screen Size", "Display Size", "Standing screen display size",
    ) or _screen_size_from_text(data.get("retailer_sku_name"), fact_text)
    data["estimated_annual_electricity_use"] = first_by_key(
        facts,
        "J\u00e4hrlicher Energieverbrauch", "Jaehrlicher Energieverbrauch",
    ) or first_by_key(facts, "Elektrische Leistung")
    ref_type_value = resolve_ref_refrigerator_type(
        data.get("retailer_sku_name"),
        first_exact_by_key(facts, "Aufbautyp"),
        first_exact_by_key(facts, "Aufbau"),
    )
    ref_capacity_value = _ref_capacity_from_title(data.get("retailer_sku_name")) or first_exact_by_key(
        facts,
        "Fassungsverm\u00f6gen", "Fassungsvermoegen",
    )
    data["ref_refrigerator_type"] = ref_type_value
    data["ref_capacity"] = ref_capacity_value

    similar = []

    def add_similar(value: Any) -> bool:
        text = clean_text(value)
        if text and text not in similar:
            similar.append(text)
        return len(similar) >= 20

    for value in _extract_sp_detail2_titles_from_soup(soup):
        if add_similar(value):
            break
    if len(similar) < 20:
        for node in soup.select("#sp_detail2 [data-adfeedbackdetails]"):
            raw = node.get("data-adfeedbackdetails")
            title = None
            if raw:
                try:
                    payload = json.loads(raw)
                    title = payload.get("title") if isinstance(payload, dict) else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    title = None
            if add_similar(title):
                break
    if len(similar) < 20:
        for node in soup.select("#sp_detail a[href*='/dp/'], #similarities_feature_div a[href*='/dp/'], #anonCarousel1 a[href*='/dp/']"):
            if add_similar(node.get("title") or node.get("aria-label") or node.get_text(" ")):
                break
    data["retailer_sku_name_similar"] = " ||| ".join(similar) if similar else None
    return data


def first_by_key(facts: dict[str, str | None], *keys: str) -> str | None:
    normalized_facts = [(_fact_key(key), value) for key, value in facts.items()]
    for wanted in (_fact_key(key) for key in keys):
        if not wanted:
            continue
        for normalized, value in normalized_facts:
            if value and wanted in normalized:
                return value
    return None


def first_exact_by_key(facts: dict[str, str | None], *keys: str) -> str | None:
    normalized_facts = {_fact_key(key): value for key, value in facts.items()}
    for key in keys:
        value = normalized_facts.get(_fact_key(key))
        if value:
            return value
    return None

def parse_review_html(html: str, *, limit: int = 20) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    reviews = []
    selectors = (
        "[data-hook='reviewRichContentContainer']",
        "[data-hook='reviewText']",
        "span[data-hook='review-body']",
    )
    for block in soup.select("div[data-hook='review']"):
        text = None
        for selector in selectors:
            body = block.select_one(selector)
            text = clean_text(body.get_text(" ")) if body else None
            if text:
                break
        if text and text not in reviews:
            reviews.append(text)
        if len(reviews) >= limit:
            break
    summary_node = soup.select_one("[data-hook='cr-insights-widget'] span, .reviewSummary")
    summary = clean_text(summary_node.get_text(" ")) if summary_node else None
    return {
        "summarized_review_content": summary,
        "detailed_review_content": " ||| ".join(f"review{i} - {text}" for i, text in enumerate(reviews, start=1)) if reviews else None,
    }
