"""DB-backed XPath selectors and Selenium extractors for Amazon.de."""
from __future__ import annotations

import re
import time
from typing import Any

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from common import parsers, siel_logging
from common.translations import translate_field
from common.io_util import db_config, split_table


ATTR_FIELDS = {"product_url": "href"}
MULTI_FIELDS = {"retailer_sku_name_similar", "detailed_review_content"}
EXPAND_FIELDS = {"expand_additional_details", "expand_item_details"}


def _quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _table_sql(table_name: str) -> str:
    schema, table = split_table(table_name)
    return f"{_quote(schema)}.{_quote(table)}"


def load_selectors(stage: str, *, domain: str) -> dict[str, dict[str, str | None]]:
    """Load SEG selectors with the same exact-match contract as SIEL."""
    stage_key = "detail" if stage in {"detail", "product"} else stage
    table_name = "dx_seg.dx_seg_xpath_selectors"
    config = db_config()
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
                "WHERE site_account = %s "
                "AND page_type = %s "
                "AND domain = %s "
                "AND is_active = TRUE",
                ("Amazon", stage_key, domain),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    selectors = {
        str(row["data_field"]): {
            "xpath": str(row["xpath_primary"]),
            "fallback": row["fallback_xpath"],
        }
        for row in rows
        if row["data_field"] and row["xpath_primary"]
    }
    if not selectors:
        raise RuntimeError(
            f"no selectors loaded for site=Amazon stage={stage_key} domain={domain} table={table_name}"
        )
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


def click_expand(root, selector: dict[str, str | None] | None) -> bool:
    selector = selector or {}
    clicked = False
    for xpath in (selector.get("xpath"), selector.get("fallback")):
        for el in _find(root, xpath):
            try:
                if hasattr(root, "execute_script"):
                    root.execute_script("arguments[0].click();", el)
                else:
                    el.click()
                clicked = True
                break
            except (StaleElementReferenceException, WebDriverException):
                continue
        if clicked:
            break
    if clicked:
        time.sleep(0.5)
    return clicked


def scroll_to_bottom(root, *, pause: float = 0.7, max_scrolls: int = 5) -> None:
    try:
        last_h = int(root.execute_script("return document.body.scrollHeight") or 0)
        for _ in range(max_scrolls):
            root.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(pause)
            new_h = int(root.execute_script("return document.body.scrollHeight") or 0)
            if new_h == last_h:
                break
            last_h = new_h
    except WebDriverException:
        return

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
    if field in {"final_sku_price", "original_sku_price"}:
        return siel_logging.parse_amzn_apex_price(value)
    if field in {"count_of_star_ratings", "bsr_rank"}:
        return clean(value)
    return translate_field(field, clean(value))


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
    for field in EXPAND_FIELDS:
        if field in selectors:
            click_expand(driver, selectors.get(field))
    for field, selector in selectors.items():
        if field == "base_container" or field in EXPAND_FIELDS:
            continue
        if field in MULTI_FIELDS:
            values = extract_multi(driver, selector, limit=20)
            if field == "detailed_review_content":
                if not values:
                    try:
                        driver.execute_script(
                            "var e = document.getElementById('reviewsMedley');"
                            " if (e) e.scrollIntoView({block: 'center', behavior: 'instant'});"
                        )
                    except WebDriverException:
                        pass
                    time.sleep(3)
                    scroll_to_bottom(driver, pause=0.7, max_scrolls=5)
                    values = extract_multi(driver, selector, limit=20)
                    if not values:
                        time.sleep(3)
                        values = extract_multi(driver, selector, limit=20)
                data[field] = " ||| ".join(f"review{i} - {v}" for i, v in enumerate(values, start=1)) if values else None
                data["count_of_reviews"] = len(values) if values else None
            elif field == "retailer_sku_name_similar":
                if not values:
                    try:
                        driver.execute_script(
                            "var c = document.querySelector('div[aria-labelledby=\"Customers who viewed this item also viewed\"]');"
                            " if (c) c.scrollIntoView({block: 'center', behavior: 'instant'});"
                        )
                    except WebDriverException:
                        pass
                    time.sleep(1.5)
                    values = extract_multi(driver, selector, limit=20)
                    if not values:
                        scroll_to_bottom(driver, pause=0.7, max_scrolls=5)
                        values = extract_multi(driver, selector, limit=20)
                data[field] = " ||| ".join(values) if values else None
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
    selector_fields = set(selectors)
    for key, value in fallback.items():
        if key == "facts_json" or key not in selector_fields:
            continue
        if data.get(key) in (None, "") and value not in (None, ""):
            data[key] = value
    if not data.get("detailed_review_content") and html:
        review = parsers.parse_review_html(html)
        for key, value in review.items():
            if key not in selector_fields and not (
                key == "count_of_reviews" and "detailed_review_content" in selector_fields
            ):
                continue
            if data.get(key) in (None, "") and value not in (None, ""):
                data[key] = value
    return data
