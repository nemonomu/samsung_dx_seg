"""DB-backed XPath selectors and Selenium extractors for Amazon.de."""
from __future__ import annotations

import re
from typing import Any

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from common import parsers
from common.io_util import db_config, env_value, split_table


DEFAULT_SELECTORS: dict[str, dict[str, dict[str, str | None]]] = {
    "main": {
        "base_container": {"xpath": "//div[@data-component-type='s-search-result' and @data-asin]", "fallback": None},
        "product_url": {"xpath": ".//a[contains(@href,'/dp/') or contains(@href,'/gp/product/') or contains(@href,'/sspa/click')][1]", "fallback": None},
        "retailer_sku_name": {"xpath": ".//h2//span | .//h2", "fallback": None},
        "final_sku_price": {"xpath": ".//span[contains(@class,'a-price') and not(contains(@class,'a-text-price'))]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "original_sku_price": {"xpath": ".//span[contains(@class,'a-text-price')]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "discount_type": {"xpath": ".//*[contains(@class,'a-badge-text')][1]", "fallback": None},
        "sku_popularity": {"xpath": ".//*[contains(@class,'a-badge-label')][1]", "fallback": None},
        "number_of_units_purchased_past_month": {"xpath": ".//span[contains(normalize-space(text()),'gekauft') or contains(normalize-space(text()),'bought')][1]", "fallback": None},
        "sku_status": {"xpath": ".//*[contains(@class,'puis-sponsored-label-text') or contains(.,'Gesponsert') or contains(.,'Sponsored')][1]", "fallback": None},
        "star_rating": {"xpath": ".//i[contains(@class,'a-icon-star-small')]//span | .//*[contains(@aria-label,'von 5') or contains(@aria-label,'out of 5')][1]", "fallback": None},
        "count_of_star_ratings": {"xpath": ".//a[contains(@href,'customerReviews')]//span[contains(@class,'a-size-base')][1]", "fallback": None},
    },
    "bsr": {
        "base_container": {"xpath": "//div[starts-with(@id,'gridItemRoot')] | //li[contains(@class,'zg-item-immersion')]", "fallback": None},
        "product_url": {"xpath": ".//a[contains(@href,'/dp/') or contains(@href,'/gp/product/')][1]", "fallback": None},
        "retailer_sku_name": {"xpath": ".//img[@alt][1] | .//*[contains(@class,'p13n-sc-truncate')][1] | .//a[contains(@class,'a-link-normal')]//span[1]", "fallback": None},
        "final_sku_price": {"xpath": ".//span[contains(@class,'a-price') and not(contains(@class,'a-text-price'))]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "original_sku_price": {"xpath": ".//span[contains(@class,'a-text-price')]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "star_rating": {"xpath": ".//*[contains(@aria-label,'von 5') or contains(@aria-label,'out of 5')][1]", "fallback": None},
        "count_of_star_ratings": {"xpath": ".//a[contains(@href,'customerReviews')]//span[1]", "fallback": None},
        "bsr_rank": {"xpath": ".//*[contains(@class,'zg-bdg-text')][1]", "fallback": None},
    },
    "detail": {
        "retailer_sku_name": {"xpath": "//*[@id='productTitle']", "fallback": None},
        "product_url": {"xpath": "//link[@rel='canonical'][1]", "fallback": None},
        "final_sku_price": {"xpath": "//*[@id='corePriceDisplay_desktop_feature_div']//span[contains(@class,'a-price') and not(contains(@class,'a-text-price'))]//span[contains(@class,'a-offscreen')][1] | //span[contains(@class,'priceToPay')]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "original_sku_price": {"xpath": "//*[@id='corePriceDisplay_desktop_feature_div']//span[contains(@class,'a-text-price')]//span[contains(@class,'a-offscreen')][1]", "fallback": None},
        "star_rating": {"xpath": "//*[@id='averageCustomerReviews']//*[contains(@class,'a-icon-alt')][1] | //*[@id='acrPopover'][1]", "fallback": None},
        "count_of_star_ratings": {"xpath": "//*[@id='acrCustomerReviewText'][1]", "fallback": None},
        "inventory_status": {"xpath": "//*[@id='availability'][1] | //*[@id='availabilityInsideBuyBox_feature_div'][1]", "fallback": None},
        "delivery_availability": {"xpath": "//*[@id='mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE'][1] | //*[@id='deliveryBlockMessage'][1]", "fallback": None},
        "fastest_delivery": {"xpath": "//*[@id='mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE'][1]", "fallback": None},
        "available_quantity_for_purchase": {"xpath": "//*[contains(.,'Nur noch') and contains(.,'auf Lager')][1]", "fallback": None},
        "screen_size": {"xpath": "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'screen') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'display')]/following-sibling::*[1]", "fallback": None},
        "model_year": {"xpath": "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'modelljahr') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'model year')]/following-sibling::*[1]", "fallback": None},
        "sku": {"xpath": "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'modellnummer') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'model number') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'part number')]/following-sibling::*[1]", "fallback": None},
        "estimated_annual_electricity_use": {"xpath": "//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'elektrische leistung') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'wattage') or contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'energy')]/following-sibling::*[1]", "fallback": None},
        "retailer_sku_name_similar": {"xpath": "//*[@id='sp_detail']//a[contains(@href,'/dp/')] | //*[@id='similarities_feature_div']//a[contains(@href,'/dp/')] | //*[@id='anonCarousel1']//a[contains(@href,'/dp/')]", "fallback": None},
        "detailed_review_content": {"xpath": "//div[@data-hook='review']//*[@data-hook='reviewRichContentContainer' or @data-hook='reviewText' or @data-hook='review-body']", "fallback": None},
    },
}

ATTR_FIELDS = {"product_url": "href"}
MULTI_FIELDS = {"retailer_sku_name_similar", "detailed_review_content"}


def _quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _table_sql(table_name: str) -> str:
    schema, table = split_table(table_name)
    return f"{_quote(schema)}.{_quote(table)}"


def load_selectors(stage: str, *, domain: str = "product") -> dict[str, dict[str, str | None]]:
    """Load selectors from DB, falling back to built-in Amazon.de defaults."""
    stage_key = "detail" if stage in {"detail", "product"} else stage
    selectors = {k: dict(v) for k, v in DEFAULT_SELECTORS.get(stage_key, {}).items()}
    config = db_config()
    if not config:
        return selectors
    table_name = (
        env_value("AMZN_SELECTOR_TABLE")
        or env_value("SEG_XPATH_SELECTOR_TABLE")
        or "dx_seg.dx_seg_xpath_selectors"
    )
    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(
            host=config.get("host"),
            port=int(config.get("port") or 5432),
            user=config.get("user"),
            password=config.get("password"),
            dbname=config.get("database"),
            connect_timeout=6,
        )
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT data_field, xpath_primary, fallback_xpath "
                    f"FROM {_table_sql(table_name)} "
                    "WHERE site_account IN ('Amazon', 'Amazon.de') "
                    "AND page_type = %s "
                    "AND domain IN (%s, 'product', 'listing') "
                    "AND is_active = TRUE",
                    (stage_key, domain),
                )
                for row in cur.fetchall():
                    field = row["data_field"]
                    xpath = row["xpath_primary"]
                    if not field or not xpath:
                        continue
                    selectors[str(field)] = {
                        "xpath": str(xpath),
                        "fallback": row["fallback_xpath"],
                    }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[selectors] DB selectors unavailable; using defaults: {type(exc).__name__}: {exc}")
    return selectors


def clean(value: Any) -> str | None:
    return parsers.clean_text(value)


def _element_text(el) -> str | None:
    try:
        text = clean(el.text)
        if text:
            return text
        for attr in ("aria-label", "alt", "title", "value", "content"):
            value = clean(el.get_attribute(attr))
            if value:
                return value
    except (StaleElementReferenceException, WebDriverException):
        return None
    return None


def _find(root, xpath: str | None):
    if not xpath:
        return []
    try:
        return root.find_elements(By.XPATH, xpath)
    except (StaleElementReferenceException, WebDriverException):
        return []


def extract_single(root, selector: dict[str, str | None] | None, *, attr: str | None = None) -> str | None:
    selector = selector or {}
    for xpath in (selector.get("xpath"), selector.get("fallback")):
        for el in _find(root, xpath):
            try:
                value = clean(el.get_attribute(attr)) if attr else _element_text(el)
            except (StaleElementReferenceException, WebDriverException):
                value = None
            if value:
                return value
    return None


def extract_multi(root, selector: dict[str, str | None] | None, *, limit: int = 20) -> list[str]:
    selector = selector or {}
    values: list[str] = []
    for xpath in (selector.get("xpath"), selector.get("fallback")):
        for el in _find(root, xpath):
            value = _element_text(el)
            if value and value not in values:
                values.append(value)
            if len(values) >= limit:
                return values
    return values


def normalize_field(field: str, value: str | None) -> str | None:
    if not value:
        return None
    if field == "sku_status":
        if "gesponsert" in value.casefold() or "sponsored" in value.casefold():
            return "Sponsored"
        return None
    if field == "star_rating":
        text = clean(value)
        if not text:
            return None
        low = text.casefold()
        if "von 5" in low or "out of 5" in low:
            return text
        match = re.search(r"\d+(?:[,.]\d+)?", text)
        if match:
            try:
                numeric = float(match.group(0).replace(",", "."))
            except ValueError:
                numeric = None
            if numeric is not None and 0 <= numeric <= 5:
                return text
        return None
    if field in {"count_of_star_ratings", "bsr_rank"}:
        return clean(value)
    return value


def extract_card(card, selectors: dict[str, dict[str, str | None]], *, sort: str, rank: int) -> dict[str, Any] | None:
    try:
        asin = clean(card.get_attribute("data-asin"))
    except (StaleElementReferenceException, WebDriverException):
        asin = None
    product_url = extract_single(card, selectors.get("product_url"), attr=ATTR_FIELDS["product_url"])
    asin = asin or parsers.asin_from_url(product_url)
    if not asin:
        return None
    product_url = parsers.product_url_for_asin(product_url, asin)
    row: dict[str, Any] = {
        "source": sort,
        "stage": sort,
        "asin": asin,
        "item": asin,
        "product_url": product_url,
    }
    for field, selector in selectors.items():
        if field in {"base_container", "product_url"}:
            continue
        value = extract_single(card, selector, attr=ATTR_FIELDS.get(field))
        if value:
            row[field] = normalize_field(field, value)
    if sort == "bsr":
        rank_text = row.get("bsr_rank")
        parsed_rank = None
        if rank_text:
            match = re.search(r"\d+", str(rank_text))
            parsed_rank = int(match.group(0)) if match else None
        row["bsr_rank"] = parsed_rank or rank
    else:
        row["main_rank"] = rank
    return row


def extract_cards(driver, selectors: dict[str, dict[str, str | None]], *, sort: str, start_rank: int = 1) -> list[dict[str, Any]]:
    cards = _find(driver, (selectors.get("base_container") or {}).get("xpath"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank = start_rank
    for card in cards:
        row = extract_card(card, selectors, sort=sort, rank=rank)
        if not row:
            continue
        asin = str(row.get("asin") or "")
        if asin in seen:
            continue
        seen.add(asin)
        rows.append(row)
        rank += 1
    return rows


def extract_detail(driver, selectors: dict[str, dict[str, str | None]], *, product: str = "TV") -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field, selector in selectors.items():
        if field == "base_container":
            continue
        if field in MULTI_FIELDS:
            values = extract_multi(driver, selector, limit=20)
            if field == "detailed_review_content":
                data[field] = " ||| ".join(f"review{i} - {v}" for i, v in enumerate(values, start=1)) if values else None
                data["count_of_reviews"] = len(values) if values else None
            else:
                data[field] = " ||| ".join(values) if values else None
            continue
        value = extract_single(driver, selector, attr=ATTR_FIELDS.get(field))
        if value:
            data[field] = normalize_field(field, value)

    html = ""
    try:
        html = driver.page_source or ""
    except WebDriverException:
        pass
    fallback = parsers.parse_product_detail_html(html) if html else {}
    for key, value in fallback.items():
        if key == "facts_json":
            continue
        if data.get(key) in (None, "") and value not in (None, ""):
            data[key] = value
    if not data.get("detailed_review_content") and html:
        review = parsers.parse_review_html(html)
        for key, value in review.items():
            if data.get(key) in (None, "") and value not in (None, ""):
                data[key] = value
    return data
