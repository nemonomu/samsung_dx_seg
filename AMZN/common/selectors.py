"""DB-backed XPath selectors and Selenium extractors for Amazon.de."""
from __future__ import annotations

import re
import time
from typing import Any

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from common import parsers, siel_logging
from common.translations import translate_field
from common.io_util import db_config, split_table


ATTR_FIELDS = {"product_url": "href"}
MULTI_FIELDS = {"retailer_sku_name_similar", "detailed_review_content"}
EXPAND_FIELDS = {"expand_additional_details", "expand_item_details"}
DISABLED_FIELDS = {"count_of_reviews"}


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
        if row["data_field"] and row["xpath_primary"] and str(row["data_field"]) not in DISABLED_FIELDS
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
        for attr in ("textContent", "innerText", "aria-label", "alt", "title", "value", "content"):
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
        return siel_logging.parse_star_rating(value)
    if field == "count_of_star_ratings":
        return siel_logging.parse_count_of_ratings(value)
    if field in {"final_sku_price", "original_sku_price"}:
        return siel_logging.parse_amzn_apex_price(value)
    if field == "model_year":
        return siel_logging.parse_model_year(value)
    if field == "delivery_availability":
        return translate_field(field, siel_logging.parse_delivery_availability(value))
    if field == "fastest_delivery":
        return translate_field(field, siel_logging.parse_fastest_delivery(value))
    if field == "sku_assurance":
        return siel_logging.parse_sku_assurance(value)
    if field == "bsr_rank":
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
        if field in {"base_container", "product_url"} or field in DISABLED_FIELDS:
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


def _dispatch_wheel(driver, delta_y: int) -> None:
    try:
        driver.execute_script(
            """
            const dy = arguments[0];
            const ev = new WheelEvent('wheel', {
              deltaY: dy,
              deltaMode: 0,
              bubbles: true,
              cancelable: true,
              view: window
            });
            (document.scrollingElement || document.documentElement).dispatchEvent(ev);
            window.dispatchEvent(ev);
            window.scrollBy(0, dy);
            """,
            delta_y,
        )
    except WebDriverException:
        pass


def _selenium_wheel(driver, amount: int) -> None:
    try:
        ActionChains(driver).scroll_by_amount(0, amount).perform()
    except Exception:
        _dispatch_wheel(driver, amount)


def _key_scroll(driver, key: str, times: int = 1, pause: float = 0.25) -> None:
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        for _ in range(times):
            body.send_keys(key)
            time.sleep(pause)
    except Exception:
        pass


def _js_bsr_records(driver) -> list[dict[str, Any]]:
    try:
        rows = driver.execute_script(
            r"""
            const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const text = (root, selectors) => {
              for (const sel of selectors) {
                const el = root.querySelector(sel);
                if (!el) continue;
                const val = clean(el.textContent || el.getAttribute('aria-label'));
                if (val) return val;
              }
              return null;
            };
            const attr = (root, selectors, name) => {
              for (const sel of selectors) {
                const el = root.querySelector(sel);
                if (!el) continue;
                const val = clean(el.getAttribute(name));
                if (val) return val;
              }
              return null;
            };
            const gridRoot = document.querySelector('#zg, #zg-right-col, #zg-center-div, .p13n-gridRow') || document;
            const cards = Array.from(gridRoot.querySelectorAll('#gridItemRoot, .zg-grid-general-faceout, [data-asin]:not([data-asin=""])'));
            const asinFromHref = (href) => {
              const match = (href || '').match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i);
              return match ? match[1].toUpperCase() : null;
            };
            const seen = new Set();
            const rows = [];
            for (const card of cards) {
              const href = attr(card, ['a[href*="/dp/"]', 'a[href*="/gp/product/"]'], 'href');
              const dataAsin = clean(card.getAttribute('data-asin') || attr(card, ['[data-asin]:not([data-asin=""])'], 'data-asin') || '');
              const asin = asinFromHref(href) || (dataAsin.match(/[A-Z0-9]{10}/) || [null])[0];
              if (!href && !asin) continue;
              const key = asin || href.split('?')[0];
              if (!key || seen.has(key)) continue;
              seen.add(key);
              const imgAlt = attr(card, ['img[alt]'], 'alt');
              rows.push({
                asin,
                product_url: href || (asin ? `https://www.amazon.de/dp/${asin}` : null),
                retailer_sku_name: text(card, [
                  '.p13n-sc-css-line-clamp',
                  '.p13n-sc-truncate',
                  'a[href*="/dp/"] span',
                  'a[href*="/gp/product/"] span',
                  'a[href*="/dp/"] div',
                  'a[href*="/gp/product/"] div'
                ]) || imgAlt,
                final_sku_price: text(card, ['.p13n-sc-price', '.a-price .a-offscreen', '.a-color-price']),
                star_rating: attr(card, [
                  '[aria-label*="von 5"]',
                  '[aria-label*="out of 5"]',
                  'i.a-icon-star span'
                ], 'aria-label') || text(card, ['i.a-icon-star span', '[aria-label*="von 5"]', '[aria-label*="out of 5"]']),
                count_of_star_ratings: text(card, [
                  'a[href*="customerReviews"] span',
                  'a[href*="product-reviews"] span',
                  '.a-size-small'
                ])
              });
            }
            return rows;
            """
        )
        return rows if isinstance(rows, list) else []
    except WebDriverException:
        return []


def _normalize_bsr_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    rec = dict(raw or {})
    asin = clean(rec.get("asin")) or parsers.asin_from_url(rec.get("product_url"))
    if not asin:
        return None
    product_url = parsers.product_url_for_asin(rec.get("product_url"), asin)
    row: dict[str, Any] = {
        "source": "bsr",
        "stage": "bsr",
        "asin": asin,
        "item": asin,
        "product_url": product_url,
    }
    return row


def _rank_bsr_records(records: list[dict[str, Any]], start_rank: int) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank = start_rank
    for raw in records:
        row = _normalize_bsr_record(raw)
        if not row:
            continue
        asin = str(row.get("asin") or "")
        if asin in seen:
            continue
        seen.add(asin)
        row["bsr_rank"] = rank
        ranked.append(row)
        rank += 1
    return ranked


def extract_bsr_cards_siel(driver, selectors: dict[str, dict[str, str | None]], *, start_rank: int = 1,
                           expected_count: int = 50) -> list[dict[str, Any]]:
    best_records: list[dict[str, Any]] = []

    def remember_records() -> list[dict[str, Any]]:
        nonlocal best_records
        records = _rank_bsr_records(_js_bsr_records(driver), start_rank)
        if len(records) > len(best_records):
            best_records = records
        return records

    records = remember_records()
    if len(records) >= expected_count:
        return records
    pause = 2.0
    try:
        for pct in (20, 40, 60, 80, 100):
            driver.execute_script('window.scrollTo(0, document.body.scrollHeight * arguments[0]);', pct / 100)
            time.sleep(pause)
            records = remember_records()
            if len(records) >= expected_count:
                return records
        for amount in (700, 900, 1100, 1300, 1500, 1800, 2200, 2600):
            _dispatch_wheel(driver, amount)
            time.sleep(0.45)
            records = remember_records()
            if len(records) >= expected_count:
                return records
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        records = remember_records()
        if len(records) >= expected_count:
            return records
        _key_scroll(driver, Keys.PAGE_DOWN, 8, pause=0.35)
        records = remember_records()
        if len(records) >= expected_count:
            return records
        _key_scroll(driver, Keys.END, 1, pause=0.5)
        time.sleep(1.0)
        records = remember_records()
    except WebDriverException:
        pass
    if best_records:
        return best_records
    return _rank_bsr_records(extract_cards(driver, selectors, sort="bsr", start_rank=start_rank), start_rank)


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
        if field == "base_container" or field in EXPAND_FIELDS or field in DISABLED_FIELDS:
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
                formatted = siel_logging.format_review_content(values)
                data[field] = formatted
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
                if str(product).lower() == "ref":
                    values = siel_logging.filter_similar_noise_ref(values)
                else:
                    values = siel_logging.filter_similar_noise(values)
                data[field] = siel_logging.format_similar_names(values)
            else:
                data[field] = siel_logging.SIMILAR_SEP.join(values) if values else None
            continue
        value = extract_single(driver, selector, attr=ATTR_FIELDS.get(field))
        if value:
            data[field] = normalize_field(field, value)

    html = ""
    try:
        html = driver.page_source or ""
    except WebDriverException:
        pass
    selector_fields = set(selectors)
    if html:
        parsed_fallback = parsers.parse_product_detail_html(html)
        for field in ("sku", "screen_size", "model_year", "estimated_annual_electricity_use", "retailer_sku_name_similar"):
            if field in selector_fields and data.get(field) in (None, "") and parsed_fallback.get(field) not in (None, ""):
                data[field] = normalize_field(field, parsed_fallback.get(field))
    if (
        "star_rating" in selector_fields
        and "count_of_star_ratings" in selector_fields
        and not data.get("star_rating")
        and not data.get("count_of_star_ratings")
        and html
    ):
        lower_html = html.casefold()
        no_review_hints = (
            "no customer reviews",
            "there are 0 customer reviews",
            "keine kundenrezensionen",
            "0 kundenrezensionen",
            "noch keine kundenrezensionen",
        )
        if any(hint in lower_html for hint in no_review_hints):
            data["star_rating"] = "No customer reviews"
            data["count_of_star_ratings"] = "0"
    return data
