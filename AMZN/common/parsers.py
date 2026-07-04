"""Amazon.de HTML parsers for listing, BSR, PDP, and review pages."""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from common.config import AMAZON_BASE
from common.translations import translate_field


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
            "sku_popularity": clean_text(item.select_one(".a-badge-label .a-badge-text, .a-badge-label").get_text(" ") if item.select_one(".a-badge-label .a-badge-text, .a-badge-label") else None),
            "number_of_units_purchased_past_month": clean_text(item.select_one("span.a-size-base.a-color-secondary").get_text(" ") if item.select_one("span.a-size-base.a-color-secondary") else None),
            "sku_status": "Sponsored" if (item.select_one(".puis-sponsored-label-text") or "Gesponsert" in item.get_text(" ")) else None,
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
            name_node = item.select_one("img[alt], .p13n-sc-truncate, a.a-link-normal span")
            name = clean_text(name_node.get("alt") if name_node and name_node.name == "img" else name_node.get_text(" ") if name_node else None)
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
                "retailer_sku_name": name,
                "final_sku_price": _price_text(item),
                "original_sku_price": _original_price(item),
                "star_rating": _rating_text(item),
                "count_of_star_ratings": _rating_count(item),
                "bsr_rank": parsed_rank or rank,
            }
            rows.append(row)
            rank = max(rank + 1, (parsed_rank or rank) + 1)
    return rows


def parse_product_detail_html(html: str) -> dict[str, Any]:
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
    qty = soup.find(string=re.compile(r"Nur noch \d+ auf Lager", re.I))
    data["available_quantity_for_purchase"] = clean_text(qty)

    facts = {}
    for row in soup.select("#productDetails_techSpec_section_1 tr, #productDetails_detailBullets_sections1 tr"):
        cells = [clean_text(c.get_text(" ")) for c in row.select("th,td")]
        if len(cells) >= 2:
            facts[cells[0].rstrip(":")] = cells[1]
    for li in soup.select("#detailBullets_feature_div li"):
        text = clean_text(li.get_text(" "))
        if text and ":" in text:
            key, value = text.split(":", 1)
            facts[clean_text(key).rstrip(":")] = clean_text(value)
    data["facts_json"] = json.dumps(facts, ensure_ascii=False)
    data["sku"] = first_by_key(facts, "Hersteller", "Modellnummer", "Manufacturer", "Model")
    data["model_year"] = first_by_key(facts, "Modelljahr", "Model Year")
    data["screen_size"] = first_by_key(facts, "Bildschirmgröße", "Displaygröße", "Standing screen display size")
    data["estimated_annual_electricity_use"] = first_by_key(facts, "Elektrische Leistung", "Wattage", "Energy Consumption")

    similar = []
    for node in soup.select("#sp_detail a[href*='/dp/'], #similarities_feature_div a[href*='/dp/'], #anonCarousel1 a[href*='/dp/']"):
        text = clean_text(node.get_text(" "))
        if text and text not in similar:
            similar.append(text)
        if len(similar) >= 20:
            break
    data["retailer_sku_name_similar"] = " ||| ".join(similar) if similar else None
    return data


def first_by_key(facts: dict[str, str | None], *keys: str) -> str | None:
    for wanted in keys:
        for key, value in facts.items():
            if wanted.lower() in key.lower() and value:
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
        "count_of_reviews": len(reviews) if reviews else None,
        "summarized_review_content": summary,
        "detailed_review_content": " ||| ".join(f"review{i} - {text}" for i, text in enumerate(reviews, start=1)) if reviews else None,
    }
