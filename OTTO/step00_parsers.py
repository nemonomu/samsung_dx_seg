"""Shared parsers for OTTO captured pages."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

OTTO_BASE = "https://www.otto.de"
MULTI_VALUE_DELIMITER = " ||| "
TEXT_TRANSLATIONS = {
    "Sehr beliebt": "Very popular",
    "Gesponsert": "Sponsored",
    "gesponsert": "Sponsored",
    "Deal des Monats": "Deal of the month",
}
DETAIL_FIELD_LABELS = {
    "sku": "Modellbezeichnung",
    "screen_size": "Bildschirmdiagonale in Zoll",
    "estimated_annual_electricity_use": "Leistungsaufnahme im Ein-Zustand bei hohem Dynamikumfang (HDR)",
}


def text_clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def translate_text(value: str | None) -> str | None:
    if not value:
        return None
    return TEXT_TRANSLATIONS.get(value, value)


def int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def decode_base64_url(value: str | None) -> str | None:
    if not value:
        return None
    padded = value + ("=" * (-len(value) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return None
    return urljoin(OTTO_BASE, decoded)


def first_decoded_link(tile) -> str | None:
    links = []
    for tag in tile.find_all(attrs={"base64-href": True}):
        href = decode_base64_url(tag.get("base64-href"))
        if href:
            links.append(href)
    for href in links:
        if "/p/" in href:
            return href
    return links[0] if links else None


def first_image_alt(tile) -> str | None:
    img = tile.find("img", alt=True)
    return text_clean(img.get("alt")) if img else None


def extract_price_texts(tile) -> dict[str, str | None]:
    text = text_clean(tile.get_text(" ", strip=True)) or ""
    euro_values = re.findall(r"\d[\d.]*,\d{2}\s*EUR|\d[\d.]*,\d{2}\s*€|\d[\d.]*,-\s*€|\d[\d.]*\s*€", text)
    discount = re.search(r"-\d{1,3}%", text)
    uvp = None
    m = re.search(r"UVP\s+(\d[\d.]*,\d{2}\s*€|\d[\d.]*,-\s*€|\d[\d.]*\s*€)", text)
    if m:
        uvp = m.group(1)
    final_price = euro_values[0] if euro_values else None
    original_price = uvp or (euro_values[1] if len(euro_values) > 1 else None)
    savings = discount.group(0) if discount else None
    return {
        "final_sku_price": final_price,
        "original_sku_price": original_price,
        "savings": savings,
        "final_sku_price_text": final_price,
        "original_sku_price_text": original_price,
        "savings_text": savings,
    }


def extract_listing_labels(tile) -> dict[str, str | None]:
    text = text_clean(tile.get_text(" ", strip=True)) or ""
    popularity_raw = "Sehr beliebt" if "Sehr beliebt" in text else None
    discount_type_raw = "Deal des Monats" if "Deal des Monats" in text else None
    status_raw = "Gesponsert" if tile.get("data-origin") == "sponsored" or "gesponsert" in text.lower() else None
    return {
        "sku_popularity_raw": popularity_raw,
        "sku_popularity": translate_text(popularity_raw),
        "sku_status_raw": status_raw,
        "sku_status": translate_text(status_raw),
        "discount_type_raw": discount_type_raw,
        "discount_type": translate_text(discount_type_raw),
    }


def parse_listing_html(path: Path, source_name: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    rows: list[dict[str, Any]] = []
    for idx, tile in enumerate(soup.find_all("article", attrs={"data-qa": "reptile-product-tile"}), start=1):
        row: dict[str, Any] = {
            "source": source_name,
            "row_index": idx,
            "article_number": tile.get("data-article-number"),
            "product_id": tile.get("data-product-id"),
            "variation_id": tile.get("data-variation-id"),
            "list_position": int_or_none(tile.get("data-list-position")),
            "local_list_position": int_or_none(tile.get("data-local-list-position")),
            "origin": tile.get("data-origin"),
            "product_url": first_decoded_link(tile),
            "retailer_sku_name": first_image_alt(tile),
        }
        row.update(extract_price_texts(tile))
        row.update(extract_listing_labels(tile))
        rows.append(row)
    return rows


def parse_jsonld(html: str) -> list[Any]:
    values: list[Any] = []
    for match in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.I | re.S):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            values.append(json.loads(raw))
        except json.JSONDecodeError:
            values.append({"_parse_error": True, "sample": raw[:500]})
    return values


def translate_delivery_availability(value: str | None) -> str | None:
    raw = text_clean(value)
    if not raw:
        return None
    lowered = raw.lower()
    if lowered == "lieferbar - am nächsten werktag bei dir":
        return "Available - at your door the next working day"
    match = re.match(r"lieferbar - in ([\d-]+) werktagen bei dir", lowered)
    if match:
        return f"Available - at your door in {match.group(1)} working days"
    if lowered.startswith("lieferbar"):
        if "-" in raw:
            return "Available - " + raw.split("-", 1)[1].strip()
        return "Available"
    return raw


def extract_delivery_availability(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    option = soup.select_one("[data-availability-message]")
    raw = option.get("data-availability-message") if option else None
    if not raw:
        headline = soup.select_one(".pdp_delivery .oc-headline-50")
        raw = headline.get_text(" ", strip=True) if headline else None
    raw = text_clean(raw)
    return raw, translate_delivery_availability(raw)


def extract_detail_characteristics(soup: BeautifulSoup) -> dict[str, str | None]:
    label_values: dict[str, str] = {}
    for row in soup.select(".dv_characteristicsTable tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = text_clean(cells[0].get_text(" ", strip=True))
        value = text_clean(cells[1].get_text(" ", strip=True))
        if label and value:
            label_values[label] = value
    return {field: label_values.get(label) for field, label in DETAIL_FIELD_LABELS.items()}


def extract_recommendation_intent(soup: BeautifulSoup) -> str | None:
    text = text_clean(soup.get_text(" ", strip=True)) or ""
    match = re.search(r"(\d{1,3}%)\s+würden diesen Artikel\s+weiterempfehlen", text)
    return match.group(1) if match else None


def extract_similar_product_names(soup: BeautifulSoup) -> str | None:
    names: list[str] = []
    section = soup.select_one(".js_pdp_reco-alternative")
    if not section:
        return None
    for tile in section.select("[data-testid='recommendation-tile']"):
        tracking = tile.get("data-tracking") or ""
        if "promo_RecoAlternative" not in tracking:
            continue
        brand_node = tile.select_one(".reco_cinema__brand")
        name_node = tile.select_one(".reco_cinema__name")
        brand = text_clean(brand_node.get_text(" ", strip=True)) if brand_node else None
        name = text_clean(name_node.get_text(" ", strip=True)) if name_node else None
        full_name = text_clean(" ".join(part for part in (brand, name) if part))
        if full_name and full_name not in names:
            names.append(full_name)
    return MULTI_VALUE_DELIMITER.join(names) if names else None


def parse_detail_reviews(soup: BeautifulSoup) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in soup.select("[data-review-id], .js_pdp_cr-item"):
        review_id = item.get("data-review-id") or ""
        if review_id and review_id in seen_ids:
            continue
        if review_id:
            seen_ids.add(review_id)
        text_node = item.select_one(".js_pdp_cr-item__reviewText, .pdp_cr-item__reviewText")
        meta_node = item.select_one(".js_pdp_cr-item__review-metadata")
        title_node = item.select_one(".js_pdp_cr-item__title")
        verified_node = item.select_one("[data-qa='cr-review-verifiedPurchase']")
        rows.append({
            "review_id": review_id or None,
            "product_id": item.get("data-product-id"),
            "rating": int_or_none(item.get("data-rating")),
            "review_index": int_or_none(item.get("data-review-item-list-index")),
            "review_title": text_clean(title_node.get_text(" ", strip=True)) if title_node else None,
            "review_text_length": int_or_none(item.get("data-review-text-length")),
            "review_age_in_days": int_or_none(item.get("data-review-age-in-days")),
            "is_verified": item.get("data-review-is-verified"),
            "metadata": text_clean(meta_node.get_text(" ", strip=True)) if meta_node else None,
            "verified_purchase_text": text_clean(verified_node.get_text(" ", strip=True)) if verified_node else None,
            "review_text": text_clean(text_node.get_text(" ", strip=True)) if text_node else None,
        })
    return rows

def format_detailed_review_content(rows: list[dict[str, Any]], limit: int = 20) -> str | None:
    texts = [text_clean(row.get("review_text")) for row in rows]
    texts = [text for text in texts if text]
    if not texts:
        return None
    return MULTI_VALUE_DELIMITER.join(f"review{idx} - {text}" for idx, text in enumerate(texts[:limit], start=1))


def parse_review_html(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    reviews = parse_detail_reviews(soup)
    non_empty_reviews = [row for row in reviews if row.get("review_text")]
    return {
        "path": str(path),
        "title": text_clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "review_rows": len(reviews),
        "review_text_rows": len(non_empty_reviews),
        "reviews": reviews,
        "detailed_review_content": format_detailed_review_content(reviews),
        "detailed_review_count": min(20, len(non_empty_reviews)),
        "recommendation_intent": extract_recommendation_intent(soup),
    }

def parse_detail_html(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    jsonld = parse_jsonld(html)
    product_jsonld = next((item for item in jsonld if isinstance(item, dict) and item.get("@type") == "Product"), {})
    aggregate = product_jsonld.get("aggregateRating") if isinstance(product_jsonld, dict) else None
    offers = product_jsonld.get("offers") if isinstance(product_jsonld, dict) else None
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    top_reviews = parse_detail_reviews(soup)
    brand = product_jsonld.get("brand") if isinstance(product_jsonld, dict) else None
    delivery_raw, delivery_translated = extract_delivery_availability(soup)
    characteristics = extract_detail_characteristics(soup)
    review_count = aggregate.get("reviewCount") if isinstance(aggregate, dict) else None
    detailed_reviews = format_detailed_review_content(top_reviews)
    return {
        "path": str(path),
        "title": text_clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "jsonld_count": len(jsonld),
        "devalue_script_count": html.count("application/json; format=devalue"),
        "data_product_id_count": html.count("data-product-id"),
        "variation_id_count": html.count("variationId"),
        "review_term_count": html.lower().count("review") + html.lower().count("bewertung"),
        "top_review_rows": len(top_reviews),
        "top_reviews": top_reviews,
        "delivery_availability_raw": delivery_raw,
        "delivery_availability": delivery_translated,
        "sku": characteristics.get("sku"),
        "screen_size": characteristics.get("screen_size"),
        "estimated_annual_electricity_use": characteristics.get("estimated_annual_electricity_use"),
        "retailer_sku_name_similar": extract_similar_product_names(soup),
        "star_rating": aggregate.get("ratingValue") if isinstance(aggregate, dict) else None,
        "count_of_star_ratings": review_count,
        "count_of_reviews": review_count,
        "recommendation_intent": extract_recommendation_intent(soup),
        "summarized_review_content": None,
        "detailed_review_content": detailed_reviews,
        "jsonld_name": product_jsonld.get("name") if isinstance(product_jsonld, dict) else None,
        "jsonld_sku": product_jsonld.get("sku") if isinstance(product_jsonld, dict) else None,
        "jsonld_brand": brand.get("name") if isinstance(brand, dict) else brand,
        "jsonld_rating_value": aggregate.get("ratingValue") if isinstance(aggregate, dict) else None,
        "jsonld_review_count": review_count,
        "jsonld_price": offers.get("price") if isinstance(offers, dict) else None,
        "jsonld_price_currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
    }


def parse_compare_html(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    variation_matches = sorted(set(re.findall(r"variationId=(\w+)|data-variation-id=\"(\w+)\"", html)))
    variation_ids = sorted({item for pair in variation_matches for item in pair if item})
    samples = []
    for tag in soup.find_all(attrs={"data-variation-id": True}):
        vid = tag.get("data-variation-id")
        text = text_clean(tag.get_text(" ", strip=True))
        if vid and text:
            samples.append({"variation_id": vid, "text": text[:200]})
    return {
        "path": str(path),
        "title": text_clean(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "variation_ids": variation_ids,
        "variation_id_count": len(variation_ids),
        "product_comparison_term_count": html.lower().count("product-comparison"),
        "vergleich_term_count": html.lower().count("vergleich"),
        "link_text_samples": samples[:50],
    }






