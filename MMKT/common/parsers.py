"""Parsers for MediaMarkt captured pages.

The listing page embeds a Redux/Apollo snapshot in `window.__PRELOADED_STATE__`
(NOT pure JSON — it contains JS `undefined`, sanitized here) plus a JSON-LD
`ItemList`. All per-product feature entities in `apolloState` are keyed by the
product id (`Media:de:<id>`), so we index them once and join by id.

Listing → Main Page fields (SEG 데이터셋 No.31-36):
  retailer_sku_name, savings, original_sku_price, final_sku_price,
  sku_status, discount_type
Plus position (display rank) and sku_id for joining to PDP.
"""
from __future__ import annotations

import json
import re
from typing import Any

MMKT_BASE = "https://www.mediamarkt.de"
MULTI_VALUE_DELIMITER = " ||| "

# German marketing/discount labels → English (딕셔너리 방식, [수집 후 번역 필요]).
TEXT_TRANSLATIONS = {
    "Gesponsert": "Sponsored",
    "gesponsert": "Sponsored",
    "Preisheld": "Price champion",
    "Gratis Standard-Lieferung": "Free standard delivery",
    "0% Finanzierung": "0% financing",
    "Deal des Tages": "Deal of the day",
    "Deal der Woche": "Deal of the week",
    "Deal des Monats": "Deal of the month",
    "Tiefpreis": "Lowest price",
    "Neu": "New",
    "Unsere Eigenmarke": "Our own brand",
    "Gewinnspiel": "Prize draw",
    "Inkl. Streaming Content": "Incl. streaming content",
    "WM-Highlight": "World Cup highlight",
    "myMediaMarkt-Rabatt verfügbar": "myMediaMarkt discount available",
    "-30€ mit Kalibrierung": "-30€ with calibration",
    "Auch für Geschäftskunden": "Also for business customers",
    "Mini LED mit QLED": "Mini LED with QLED",
    "Technik Highlight": "Tech highlight",
    "Läuft mit Powerbank": "Runs on power bank",
    "Gratis Versand": "Free shipping",
}


def text_clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or None


def translate_text(value: str | None) -> str | None:
    if not value:
        return None
    return TEXT_TRANSLATIONS.get(value, value)


def format_euro(amount: float | int | None) -> str | None:
    """Render a euro amount in MediaMarkt's native display style (kept as-is per
    user): whole -> '399,– €', with cents -> '116,90 €'."""
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    whole = int(value)
    if abs(value - whole) < 0.005:
        return f"{whole:,}".replace(",", ".") + ",– €"
    text = f"{value:,.2f}".replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text + " €"


def _norm_id(raw: str | None) -> str | None:
    """'Media:de:2988691' or 'Media:de-DE:2988691' -> '2988691'."""
    if not raw:
        return None
    m = re.search(r"(\d+)\s*$", str(raw))
    return m.group(1) if m else None


def extract_preloaded_state(html: str) -> dict[str, Any] | None:
    marker = "window.__PRELOADED_STATE__ = "
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html.find("</script>", start)
    blob = html[start:end].strip().rstrip(";").strip()
    blob = re.sub(r"\bundefined\b", "null", blob)
    try:
        return json.loads(blob)
    except Exception:
        return None


def extract_jsonld_itemlist(html: str) -> dict[str, dict[str, Any]]:
    """Return {sku_id: {name, price, rating_value, review_count, url}} from JSON-LD."""
    out: dict[str, dict[str, Any]] = {}
    for m in re.finditer(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S
    ):
        blob = m.group(1).strip()
        if '"ItemList"' not in blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        for el in data.get("itemListElement", []):
            item = el.get("item") or {}
            url = item.get("url") or ""
            sku_id = _norm_id(re.search(r"-(\d+)\.html", url).group(1)) if re.search(r"-(\d+)\.html", url) else None
            if not sku_id:
                continue
            offers = item.get("offers") or {}
            rating = item.get("aggregateRating") or {}
            out[sku_id] = {
                "position": el.get("position"),
                "name": item.get("name"),
                "price": offers.get("price"),
                "rating_value": rating.get("ratingValue"),
                "review_count": rating.get("reviewCount") or rating.get("ratingCount"),
                "url": url,
            }
    return out


def _index_apollo(apollo: dict[str, Any]) -> dict[str, dict[str, dict]]:
    """Group apollo entities by __typename, then by normalized product id."""
    by_type: dict[str, dict[str, dict]] = {}
    order: list[dict] = []
    for key, val in apollo.items():
        if not isinstance(val, dict):
            continue
        tn = val.get("__typename")
        if tn == "ProductListPage":
            order.append(val)
            continue
        pid = _norm_id(val.get("id")) or _norm_id(val.get("productId")) or _norm_id(key)
        if not pid:
            continue
        by_type.setdefault(tn, {})[pid] = val
    by_type["__ORDER__"] = {"pages": order}  # type: ignore[assignment]
    return by_type


def _price_fields(price_feat: dict | None) -> dict[str, Any]:
    if not price_feat:
        return {"final": None, "original": None, "savings": None}
    price = price_feat.get("price") or {}
    amount = price.get("amount")
    discount = price.get("discount") or 0
    pct = price.get("discountPercentage")
    original = (amount + discount) if (amount is not None and discount) else amount
    savings = f"-{int(pct)}%" if pct else None
    return {
        "final": amount,
        "original": original if discount else None,
        "savings": savings,
    }


def _discount_type(badges_feat: dict | None) -> tuple[str | None, str | None]:
    """Return (raw German labels, English translation) joined by delimiter."""
    if not badges_feat:
        return None, None
    names: list[str] = []
    for badge in badges_feat.get("computedBadges") or []:
        name = text_clean(badge.get("name"))
        if name and name not in names:
            names.append(name)
    if not names:
        return None, None
    raw = MULTI_VALUE_DELIMITER.join(names)
    eng = MULTI_VALUE_DELIMITER.join(translate_text(n) for n in names)
    return raw, eng


def parse_listing_html(html: str, *, page: int = 1) -> list[dict[str, Any]]:
    """Parse one MediaMarkt listing page into ordered per-SKU Main-field rows."""
    state = extract_preloaded_state(html)
    if not state:
        return []
    apollo = state.get("apolloState") or {}
    by_type = _index_apollo(apollo)
    jsonld = extract_jsonld_itemlist(html)

    products = by_type.get("GraphqlProduct", {})
    prices = by_type.get("CofrPriceFeature", {})
    badges = by_type.get("CofrBadgesFeature", {})
    online = by_type.get("CofrOnlineStatusFeature", {})

    # Display order + sponsored flag from ProductListPage.products[].
    ordered: list[tuple[str, dict]] = []
    for pagestate in by_type.get("__ORDER__", {}).get("pages", []):  # type: ignore[union-attr]
        for entry in pagestate.get("products") or []:
            pid = _norm_id(entry.get("productId"))
            if pid:
                ordered.append((pid, entry))
    if not ordered:  # fallback: JSON-LD order
        ordered = [(pid, {}) for pid in sorted(jsonld, key=lambda k: jsonld[k].get("position") or 0)]

    base_rank = (page - 1) * 12
    rows: list[dict[str, Any]] = []
    for idx, (pid, entry) in enumerate(ordered, start=1):
        prod = products.get(pid) or {}
        ld = jsonld.get(pid) or {}
        pf = _price_fields(prices.get(pid))
        raw_dt, eng_dt = _discount_type(badges.get(pid))
        is_sponsored = bool(entry.get("adData"))
        url = prod.get("url") or ld.get("url") or ""
        if url and not url.startswith("http"):
            url = MMKT_BASE + url
        rows.append(
            {
                "position": base_rank + idx,
                "sku_id": pid,
                "retailer_sku_name": text_clean(prod.get("title")) or text_clean(ld.get("name")),
                "manufacturer": text_clean(prod.get("manufacturer")),
                "final_sku_price": format_euro(pf["final"] if pf["final"] is not None else ld.get("price")),
                "original_sku_price": format_euro(pf["original"]),
                "savings": pf["savings"],
                "sku_status": "Sponsored" if is_sponsored else None,
                "discount_type": raw_dt,
                "discount_type_en": eng_dt,
                "star_rating": round(ld["rating_value"], 1) if ld.get("rating_value") is not None else None,
                "count_of_reviews": ld.get("review_count"),
                "product_url": url,
                "is_available": (online.get(pid) or {}).get("isAvailableAndBuyable"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Product Page (PDP) — SEG 데이터셋 No.37-48
# ---------------------------------------------------------------------------

# CofrDeliveryFeature / CofrPickupFeature displayStatus enums → (DE, EN).
DELIVERY_STATUS_LABELS = {
    "AVAILABLE": ("Lieferbar", "Available for delivery"),
    "PARTIALLY_AVAILABLE": ("Eingeschränkt lieferbar", "Partially available for delivery"),
    "NOT_AVAILABLE": ("Nicht lieferbar", "Not available for delivery"),
    "UNAVAILABLE": ("Nicht lieferbar", "Not available for delivery"),
}
# pick_up_availability (Option B): pickup-ability status, no store-specific time.
PICKUP_AVAILABLE_EN = "Available for store pickup"
PICKUP_UNAVAILABLE_EN = "Not available for store pickup"

# Phrase-level DE->EN for the real delivery text scraped from the PDP. Longest
# phrases first; dates/prices pass through unchanged.
DELIVERY_PHRASE_TRANSLATIONS = [
    ("Leider keine Lieferung möglich", "Unfortunately no delivery possible"),
    ("Nur in einigen Regionen verfügbar", "Only available in some regions"),
    ("Bitte gib deine Postleitzahl ein", "Please enter your zip code"),
    ("PLZ eingeben", "Enter zip code"),
    ("Lieferung nach Hause", "Home delivery"),
    ("Kostenlose Standard-Lieferung", "Free standard delivery"),
    ("Standard-Lieferung", "Standard delivery"),
    ("Sofort-Lieferung", "Express delivery"),
    ("Sofort lieferbar", "Immediately available"),
    ("Lieferung ab", "Delivery from"),
    ("Lieferung", "Delivery"),
    ("Abholung im Markt", "Store pickup"),
    ("Marktabholung", "Store pickup"),
    ("Nicht lieferbar", "Not available for delivery"),
    ("lieferbar", "available"),
]


def translate_delivery(text: str | None) -> str | None:
    if not text:
        return None
    out = text
    for de, en in DELIVERY_PHRASE_TRANSLATIONS:
        out = out.replace(de, en)
    return text_clean(out)


def extract_delivery_text(html: str) -> tuple[str | None, str | None]:
    """Real displayed delivery text from the PDP DOM (German, English). The
    primary `[data-test^="mms-cofr-delivery"]` block, e.g.
    'Lieferung nach Hause Lieferung ab 01.07.2026 + 39,90 €'."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None, None
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.select('[data-test^="mms-cofr-delivery"]')
    if not blocks:
        return None, None
    de = text_clean(blocks[0].get_text(" ", strip=True))
    return de, translate_delivery(de)

# Technical-spec feature names (GraphqlProductFeature.name) we map to SEG fields.
SPEC_SCREEN_SIZE = "Bildschirmdiagonale (Zoll)"
SPEC_SCREEN_SIZE_ALT = "Bildschirmdiagonale (cm/Zoll)"
SPEC_MODEL_YEAR = "Modelljahr"
SPEC_POWER_HDR = "Leistungsaufnahme in Ein-Zustand (HDR)"
SPEC_POWER_SDR = "Leistungsaufnahme in Ein-Zustand (SDR)"
# SKU target per the dev spec = "Modelkennung" (model identifier, e.g. "32HV02V",
# "MF110W90B-14A10"). NOT "Hersteller Artikelnummer" (a manufacturer/internal id
# like 10002386) and NOT the EAN barcode — both are wrong sources. If Modelkennung
# is absent the product has no SKU → leave it NULL (no fallback).
SPEC_SKU = "Modelkennung"


def _find_main_product(apollo: dict[str, Any], sku_id: str) -> dict[str, Any]:
    """The GraphqlProduct entity for sku_id that carries its own featureGroups."""
    best: dict[str, Any] = {}
    for val in apollo.values():
        if not isinstance(val, dict) or val.get("__typename") != "GraphqlProduct":
            continue
        if _norm_id(val.get("id")) != str(sku_id):
            continue
        if val.get("featureGroups"):
            return val
        best = best or val
    return best


def _resolve_features(apollo: dict[str, Any], product: dict[str, Any]) -> dict[str, str]:
    """name -> value map of the main product's technical features only."""
    features: dict[str, str] = {}
    for group in product.get("featureGroups") or []:
        for ref in group.get("features") or []:
            ent = apollo.get(ref.get("__ref")) if isinstance(ref, dict) else None
            if isinstance(ent, dict) and ent.get("name") is not None:
                name = ent["name"]
                value = ent.get("value")
                if name not in features or features[name] in (None, ""):
                    features[name] = value
    return features


def _entity_by_id(apollo: dict[str, Any], typename: str, sku_id: str) -> dict[str, Any]:
    target = f"Media:de:{sku_id}"
    for val in apollo.values():
        if isinstance(val, dict) and val.get("__typename") == typename and val.get("id") == target:
            return val
    return {}


def _screen_size(features: dict[str, str]) -> str | None:
    """Screen diagonal in English 'inches' unit (per user), e.g. '43 inches'."""
    zoll = features.get(SPEC_SCREEN_SIZE)
    if zoll:
        num = text_clean(str(zoll)).replace(",", ".")
        return f"{num} inches" if num else None
    alt = features.get(SPEC_SCREEN_SIZE_ALT)  # e.g. "108 cm / 43 Zoll"
    if alt:
        m = re.search(r"([\d.,]+)\s*Zoll", alt)
        if m:
            return f"{m.group(1).replace(',', '.')} inches"
        return text_clean(alt)
    return None


def _power_use(features: dict[str, str]) -> str | None:
    """HDR on-mode power only — NO SDR fallback (per user)."""
    val = features.get(SPEC_POWER_HDR)
    if val in (None, ""):
        return None
    return f"{text_clean(str(val))} W"


def _review_stats(apollo: dict[str, Any], sku_id: str) -> dict[str, Any]:
    """Bazaarvoice stats from CofrCoreFeature.reviewStatistics for this product."""
    core = _entity_by_id(apollo, "CofrCoreFeature", sku_id)
    stats = core.get("reviewStatistics") if isinstance(core, dict) else None
    if isinstance(stats, dict):
        return {
            "average": stats.get("averageOverallRating"),
            "total": stats.get("totalReviewCount"),
        }
    return {"average": None, "total": None}


def _jsonld_rating(html: str) -> tuple[float | None, int | None]:
    m = re.search(
        r'"aggregateRating":\{"@type":"AggregateRating","ratingValue":"([\d.]+)","ratingCount":"(\d+)"\}',
        html,
    )
    if not m:
        return None, None
    return float(m.group(1)), int(m.group(2))


def _embedded_reviews(apollo: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Server-embedded GraphqlReview entities (only ~10; full top-20 needs the reviews API)."""
    reviews: list[dict[str, Any]] = []
    for val in apollo.values():
        if not isinstance(val, dict) or val.get("__typename") != "GraphqlReview":
            continue
        feedback = val.get("feedback") or {}
        reviews.append(
            {
                "rating": val.get("rating"),
                "title": text_clean(val.get("title")),
                "text": text_clean(feedback.get("full")),
                "date": (val.get("date") or "")[:10],
                "verified": val.get("isVerifiedPurchaser"),
                "ratings_only": val.get("isRatingsOnly"),
            }
        )
    # written reviews first (those with text), then by date desc
    reviews.sort(key=lambda r: (bool(r["text"]), r["date"] or ""), reverse=True)
    return reviews[:limit]


def _format_reviews(reviews: list[dict[str, Any]], limit: int = 20) -> str | None:
    """OTTO format: 'review1 - <text> ||| review2 - <text> ...' (text only)."""
    texts = [text_clean(r.get("text")) for r in reviews]
    texts = [t for t in texts if t]
    if not texts:
        return None
    return MULTI_VALUE_DELIMITER.join(
        f"review{idx} - {text}" for idx, text in enumerate(texts[:limit], start=1)
    )


def tv_extract_pdp_spec(features: dict[str, str]) -> dict[str, Any]:
    """TV product-specific PDP spec fields (No.40-43)."""
    return {
        "screen_size": _screen_size(features),
        "estimated_annual_electricity_use": _power_use(features),
        "model_year": text_clean(features.get(SPEC_MODEL_YEAR)),
    }


def parse_pdp_html(html: str, sku_id: str, cfg: Any = None) -> dict[str, Any] | None:
    """Parse a MediaMarkt PDP into the SEG detail fields. Common fields
    (delivery, pickup, sku, ratings, reviews) are product-agnostic; the
    product-specific spec fields come from cfg.extract_pdp_spec(features) — TV:
    screen_size/model_year/electricity, REF: ref_refrigerator_type/ref_capacity,
    LDY: ldy_loading_type/ldy_capacity. cfg=None falls back to the TV extractor.
    """
    sku_id = str(sku_id)
    state = extract_preloaded_state(html)
    if not state:
        return None
    apollo = (state or {}).get("apolloState") or {}

    product = _find_main_product(apollo, sku_id)
    if not product:
        return None
    features = _resolve_features(apollo, product)
    pickup = _entity_by_id(apollo, "CofrPickupFeature", sku_id)

    spec_extractor = getattr(cfg, "extract_pdp_spec", None) or tv_extract_pdp_spec
    spec = spec_extractor(features)

    # delivery_availability = the REAL displayed text (DOM), German + English.
    d_de, d_en = extract_delivery_text(html)
    # pick_up_availability (Option B) = pickup-ability status, no store time.
    pickable = pickup.get("isProductPickable") if pickup else None
    p_de = "Im Markt abholbar" if pickable else "Nicht im Markt abholbar"
    p_en = PICKUP_AVAILABLE_EN if pickable else PICKUP_UNAVAILABLE_EN

    stats = _review_stats(apollo, sku_id)
    ld_rating, ld_count = _jsonld_rating(html)
    avg = stats["average"] if stats["average"] is not None else ld_rating
    total = stats["total"] if stats["total"] is not None else ld_count
    reviews = _embedded_reviews(apollo)

    return {
        "sku_id": sku_id,
        # No.37-38 delivery / pickup
        "delivery_availability": d_de,
        "delivery_availability_en": d_en,
        "pick_up_availability": p_de,
        "pick_up_availability_en": p_en,
        # No.39 similar — needs Alternativen/reco source
        "retailer_sku_name_similar": None,
        # sku is common; product-specific specs come from cfg.extract_pdp_spec
        "sku": text_clean(features.get(SPEC_SKU)),
        **spec,
        # No.44-46 ratings
        "star_rating": round(avg, 1) if avg is not None else None,
        "count_of_star_ratings": total,
        "count_of_reviews": None,  # written-review count needs reviews API
        # No.47-48 reviews
        "summarized_review_content": None,  # KI summary not server-rendered
        "detailed_review_content": _format_reviews(reviews),
        "_embedded_review_count": len(reviews),
        "_needs_review_api": True,
    }


# ---------------------------------------------------------------------------
# Lazy GraphQL responses (captured in-browser) — fill the 3 PDP fields that are
# NOT in the PDP SSR. See MMKT/step00_capture_pdp_har.py for how they are fetched.
# ---------------------------------------------------------------------------

def _unwrap_data(resp: Any) -> dict[str, Any]:
    """Accept a full GraphQL response ({data:{...}}) or the inner data dict."""
    if isinstance(resp, dict):
        return resp.get("data", resp) if "data" in resp else resp
    return {}


def parse_reviews_summary(resp: Any) -> str | None:
    """GetReviewsSummary → summarized_review_content (KI German paragraph). No.47."""
    data = _unwrap_data(resp)
    return text_clean(data.get("reviewsSummary"))


def _rating_distribution_stats(distribution: Any) -> tuple[int | None, float | None]:
    """Return (rating count, weighted average) for a complete 1..5 distribution.

    A missing or malformed distribution is unknown (None, None). An explicitly
    present all-zero distribution is a known unrated product (0, None).
    """
    if not isinstance(distribution, list) or not distribution:
        return None, None
    pairs: list[tuple[float, int]] = []
    for item in distribution:
        if not isinstance(item, dict):
            return None, None
        try:
            value = float(item.get("value"))
            count = int(item.get("count"))
        except (TypeError, ValueError):
            return None, None
        if not 1 <= value <= 5 or count < 0:
            return None, None
        pairs.append((value, count))
    total = sum(count for _, count in pairs)
    if total == 0:
        return 0, None
    average = sum(value * count for value, count in pairs) / total
    return total, round(average, 1)


def parse_product_reviews(resp_pages: Any, *, top: int = 20) -> dict[str, Any]:
    """GetProductReviews (one or more reviewPage responses) → review fields.

    Returns star_rating + count_of_star_ratings from the rating distribution,
    count_of_reviews from totalResults (written reviews), and
    detailed_review_content (top-N written reviews joined). No.44-48.
    """
    pages = resp_pages if isinstance(resp_pages, list) else [resp_pages]
    total_results: int | None = None
    distribution_sum: int | None = None
    distribution_average: float | None = None
    merged: dict[str, dict[str, Any]] = {}  # review id -> review
    for page in pages:
        data = _unwrap_data(page)
        reviews_obj = data.get("reviews") or {}
        if total_results is None and reviews_obj.get("totalResults") is not None:
            total_results = reviews_obj.get("totalResults")
        dist = (reviews_obj.get("rating") or {}).get("distribution") or []
        if dist and distribution_sum is None:
            distribution_sum, distribution_average = _rating_distribution_stats(dist)
        for rv in reviews_obj.get("reviews") or []:
            rid = rv.get("id") or rv.get("cid")
            if rid and rid not in merged:
                merged[rid] = rv

    ordered = sorted(
        merged.values(),
        key=lambda r: (bool((r.get("feedback") or {}).get("full")), r.get("date") or ""),
        reverse=True,
    )
    written = [
        {
            "rating": r.get("rating"),
            "title": text_clean(r.get("title")),
            "text": text_clean((r.get("feedback") or {}).get("full")),
        }
        for r in ordered
        if text_clean((r.get("feedback") or {}).get("full"))
    ]
    return {
        "star_rating": distribution_average,
        # totalResults is the written-review count, not the number of ratings.
        # Keep rating count unknown when no valid distribution was returned so
        # merge_detail can preserve comparison's count and step09 can use the
        # listing AggregateRating fallback.
        "count_of_star_ratings": distribution_sum,
        "count_of_reviews": total_results,
        "detailed_review_content": _format_reviews(written[:top]),
        "_written_review_count": len(written),
    }


def parse_similar(resp: Any, *, self_sku_id: str | None = None) -> str | None:
    """GetComparisonTableRecommendations → retailer_sku_name_similar. No.39.

    "Alternativen im Vergleich" product titles, excluding the product itself.
    """
    data = _unwrap_data(resp)
    table = ((data.get("comparisonTableRecommendations") or {}).get("tableData") or {})
    titles: list[str] = []
    for prod in table.get("products") or []:
        agg = prod.get("productAggregate") or {}
        pid = _norm_id(agg.get("productId")) or _norm_id(
            (prod.get("cofrProductAggregate") or {}).get("productId")
        )
        if self_sku_id and pid == str(self_sku_id):
            continue
        title = text_clean((agg.get("product") or {}).get("title"))
        if title and title not in titles:
            titles.append(title)
    return MULTI_VALUE_DELIMITER.join(titles) if titles else None


def _reconstruct_delivery(deliv: dict) -> tuple[str | None, str | None]:
    """Delivery text reconstructed from cofrDeliveryFeature (GraphQL-only path):
    displayStatus + earliest fulfillment date — not the store-specific DOM text."""
    status = (deliv or {}).get("displayStatus")
    de, en = DELIVERY_STATUS_LABELS.get(status, (status, None))
    earliest = ((deliv or {}).get("fulfillmentTime") or {}).get("earliest")
    if status in ("AVAILABLE", "PARTIALLY_AVAILABLE") and earliest:
        ymd = earliest[:10].split("-")
        if len(ymd) == 3:
            date = f"{ymd[2]}.{ymd[1]}.{ymd[0]}"
            return f"Lieferung nach Hause ab {date}", f"Home delivery from {date}"
    return de, en


def _comparison_main(resp: Any, sku_id: str) -> tuple[dict | None, list[dict]]:
    data = _unwrap_data(resp)
    prods = (((data.get("comparisonTableRecommendations") or {}).get("tableData") or {}).get("products") or [])
    if not prods:
        return None, []
    main = None
    for p in prods:
        aggregate = p.get("productAggregate") or {}
        cofr = p.get("cofrProductAggregate") or {}
        candidate_ids = {
            _norm_id(aggregate.get("productId")),
            _norm_id(cofr.get("productId")),
            _norm_id(cofr.get("id")),
        }
        if str(sku_id) in candidate_ids:
            main = p
            break
    if main is None:
        return None, []
    others = [p for p in prods if p is not main]
    return main, others


def parse_comparison_detail(resp: Any, sku_id: str, cfg: Any = None) -> dict[str, Any] | None:
    """GraphQL-only PDP detail from GetComparisonTableRecommendations — NO page
    navigation. The main product (matched by sku_id) carries specs
    (featureGroupsWithProductId), delivery, pickup, ratings; the rest are the
    similar items. Returns None if the response has no products (caller falls back).
    Reviews/summary are filled separately by merge_detail.
    """
    sku_id = str(sku_id)
    main, others = _comparison_main(resp, sku_id)
    if not main:
        return None
    pa = (main.get("productAggregate") or {}).get("product") or {}
    fg = (pa.get("featureGroupsWithProductId") or {}).get("featureGroups") or []
    feats: dict[str, str] = {}
    for g in fg:
        for f in g.get("features") or []:
            if isinstance(f, dict) and f.get("name") is not None:
                name = f["name"]
                value = f.get("value")
                if name not in feats or feats[name] in (None, ""):
                    feats[name] = value

    spec_extractor = getattr(cfg, "extract_pdp_spec", None) or tv_extract_pdp_spec
    spec = spec_extractor(feats)

    agg = main.get("cofrProductAggregate") or {}
    deliv = (agg.get("cofrDeliveryFeature") or {}).get("delivery") or {}
    pickable = (agg.get("cofrPickupFeature") or {}).get("isProductPickable")
    stats = (agg.get("cofrCoreFeature") or {}).get("reviewStatistics") or {}

    d_de, d_en = _reconstruct_delivery(deliv)
    p_de = "Im Markt abholbar" if pickable else "Nicht im Markt abholbar"
    p_en = PICKUP_AVAILABLE_EN if pickable else PICKUP_UNAVAILABLE_EN
    avg = stats.get("averageOverallRating")
    total = stats.get("totalReviewCount")

    similar_titles: list[str] = []
    for o in others:
        t = text_clean(((o.get("productAggregate") or {}).get("product") or {}).get("title"))
        if t and t not in similar_titles:
            similar_titles.append(t)

    return {
        "sku_id": sku_id,
        "delivery_availability": d_de, "delivery_availability_en": d_en,
        "pick_up_availability": p_de, "pick_up_availability_en": p_en,
        "sku": text_clean(feats.get(SPEC_SKU)),
        **spec,
        "retailer_sku_name_similar": MULTI_VALUE_DELIMITER.join(similar_titles) or None,
        "star_rating": round(avg, 1) if avg is not None else None,
        "count_of_star_ratings": total,
        "count_of_reviews": None,
        "summarized_review_content": None,
        "detailed_review_content": None,
    }
