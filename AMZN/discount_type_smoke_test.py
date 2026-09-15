"""Amazon.de discount_type live smoke test with no database writes.

This script reads the active SEG XPath selectors with a read-only transaction,
opens Amazon.de in a visible Chrome window, and compares the current selector
with a proposed German/English deal-only selector.  It never imports or calls
the crawler's DB-save, S3-upload, or email paths.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By

from common import parsers
from common.io_util import db_config
from common.translations import translate_field


PRODUCT_CONFIGS = {
    "tv": "TV.config",
    "ref": "REF.config",
}

FALLBACK_MAIN_SELECTORS = {
    "base_container": {
        "xpath": '//div[@data-component-type="s-search-result" and @data-asin]',
        "fallback": None,
    },
    "product_url": {
        "xpath": './/a[contains(@href,"/dp/") or contains(@href,"/gp/product/") or contains(@href,"/sspa/click")][1]',
        "fallback": None,
    },
}

_LOWER = 'translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'
_ALLOWED_RAW_PREDICATE = f"""
    {_LOWER} = "limited time offer"
    or {_LOWER} = "hot deal"
    or {_LOWER} = "limited time deal"
    or {_LOWER} = "ends in"
    or starts-with({_LOWER}, "ends in ")
    or {_LOWER} = "befristetes angebot"
    or {_LOWER} = "zeitlich begrenztes angebot"
    or {_LOWER} = "endet in"
    or starts-with({_LOWER}, "endet in ")
    or {_LOWER} = "angebot endet in"
    or starts-with({_LOWER}, "angebot endet in ")
"""

# Search/listing deal badges have an ASIN-specific DEAL_* label.  Restricting
# the search to that subtree excludes Amazon's Choice/Tipp and coupon blocks.
PROPOSED_MAIN_XPATH = f"""
.//*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[
{_ALLOWED_RAW_PREDICATE}
]
""".strip()

# PDPs primarily expose deal text in dealBadgeSupportingText.  The additional
# branches cover page variants without scanning unrelated coupon/price blocks.
PROPOSED_DETAIL_XPATH = f"""
//*[@id="dealBadgeSupportingText"][
{_ALLOWED_RAW_PREDICATE}
]
|
//*[@id="dealBadge_feature_div"]//span[
{_ALLOWED_RAW_PREDICATE}
]
|
//*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[
{_ALLOWED_RAW_PREDICATE}
]
""".strip()

CSV_FIELDS = (
    "product",
    "stage",
    "page",
    "rank",
    "asin",
    "product_url",
    "http_status",
    "db_raw",
    "db_current_value",
    "db_policy_value",
    "proposed_raw",
    "proposed_value",
    "badge_texts",
    "coupon_texts",
    "verdict",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SEG Amazon.de discount_type를 DB 저장 없이 실페이지에서 점검합니다."
    )
    parser.add_argument(
        "--products",
        nargs="+",
        choices=sorted(PRODUCT_CONFIGS),
        default=sorted(PRODUCT_CONFIGS),
        help="점검 상품군(기본: ref tv)",
    )
    parser.add_argument(
        "--main-limit",
        type=int,
        default=30,
        help="상품군별 main 최대 수집 건수. 페이지 수가 아닙니다(기본: 30건).",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        default=5,
        help="상품군별 상세 확인 상품 수(기본: 5건).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="상품군별 main 최대 탐색 페이지 수(기본: 5페이지).",
    )
    parser.add_argument("--sleep", type=float, default=2.0, help="페이지 로드 후 대기 초")
    parser.add_argument("--headless", action="store_true", help="Chrome 창을 표시하지 않음")
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="점검 후 Enter를 누를 때까지 Chrome을 열어 둠",
    )
    parser.add_argument(
        "--output",
        help="CSV 저장 경로. 생략하면 Windows 임시 폴더에 저장합니다.",
    )
    return parser.parse_args()


def _clean(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def normalize_allowed_discount(value: Any) -> str | None:
    """Return the agreed canonical value, or None for every other promotion."""
    text = _clean(value)
    if not text:
        return None
    folded = text.casefold()
    fixed = {
        "limited time offer": "Limited Time Offer",
        "hot deal": "Hot deal",
        "limited time deal": "Limited time deal",
        "befristetes angebot": "Limited Time Offer",
        "zeitlich begrenztes angebot": "Limited Time Offer",
    }
    if folded in fixed:
        return fixed[folded]
    match = re.fullmatch(
        r"(?:angebot\s+)?(?:endet\s+in|ends\s+in)(?:\s+(.*))?",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        suffix = _clean(match.group(1))
        return "Ends in" if not suffix else f"Ends in {suffix}"
    return None


def _read_active_selectors(product: str, stage: str) -> dict[str, dict[str, str | None]]:
    """Mirror production's selector lookup inside an enforced read-only transaction."""
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
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT data_field, xpath_primary, fallback_xpath "
                "FROM dx_seg.dx_seg_xpath_selectors "
                "WHERE site_account = %s AND page_type = %s "
                "AND domain = %s AND is_active = TRUE",
                ("Amazon", stage, product),
            )
            rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    return {
        str(row["data_field"]): {
            "xpath": str(row["xpath_primary"]),
            "fallback": row["fallback_xpath"],
        }
        for row in rows
        if row["data_field"] and row["xpath_primary"]
    }


def _elements(root: Any, xpath: str | None) -> list[Any]:
    if not xpath:
        return []
    try:
        return root.find_elements(By.XPATH, xpath)
    except (StaleElementReferenceException, WebDriverException):
        return []


def _element_text(element: Any) -> str | None:
    try:
        for value in (
            element.text,
            element.get_attribute("textContent"),
            element.get_attribute("innerText"),
            element.get_attribute("aria-label"),
        ):
            cleaned = _clean(value)
            if cleaned:
                return cleaned
    except (StaleElementReferenceException, WebDriverException):
        return None
    return None


def _first_xpath(root: Any, selector: dict[str, str | None] | None) -> str | None:
    selector = selector or {}
    for xpath in (selector.get("xpath"), selector.get("fallback")):
        for element in _elements(root, xpath):
            value = _element_text(element)
            if value:
                return value
    return None


def _first_attr(root: Any, selector: dict[str, str | None] | None, attr: str) -> str | None:
    selector = selector or {}
    for xpath in (selector.get("xpath"), selector.get("fallback")):
        for element in _elements(root, xpath):
            try:
                value = _clean(element.get_attribute(attr))
            except (StaleElementReferenceException, WebDriverException):
                value = None
            if value:
                return value
    return None


def _unique_css_texts(root: Any, css: str, limit: int = 8) -> list[str]:
    values: list[str] = []
    try:
        elements = root.find_elements(By.CSS_SELECTOR, css)
    except (StaleElementReferenceException, WebDriverException):
        return values
    for element in elements:
        value = _element_text(element)
        if value and value not in values:
            values.append(value)
        if len(values) >= limit:
            break
    return values


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _translated_current(value: str | None) -> str | None:
    return translate_field("discount_type", value) if value else None


def _verdict(db_raw: str | None, proposed_value: str | None, badge_texts: list[str], coupon_texts: list[str]) -> str:
    if proposed_value:
        return "허용 딜"
    if coupon_texts:
        return "쿠폰 제외"
    if db_raw:
        return "현재 오수집 위험"
    if badge_texts:
        return "기타 배지 제외"
    return "대상 없음"


def _make_row(
    *,
    product: str,
    stage: str,
    page: int | None,
    rank: int | None,
    asin: str | None,
    product_url: str | None,
    http_status: Any,
    root: Any,
    db_selector: dict[str, str | None] | None,
    proposed_xpath: str,
) -> dict[str, Any]:
    db_raw = _first_xpath(root, db_selector)
    proposed_raw = _first_xpath(root, {"xpath": proposed_xpath, "fallback": None})
    badge_texts = _unique_css_texts(root, ".a-badge-text")
    coupon_texts = _unique_css_texts(
        root,
        ".s-coupon-clipped, .s-coupon-unclipped, [class*='coupon'], [id*='coupon']",
    )
    proposed_value = normalize_allowed_discount(proposed_raw)
    return {
        "product": product.upper(),
        "stage": stage,
        "page": page,
        "rank": rank,
        "asin": asin,
        "product_url": product_url,
        "http_status": http_status,
        "db_raw": db_raw,
        "db_current_value": _translated_current(db_raw),
        "db_policy_value": normalize_allowed_discount(db_raw),
        "proposed_raw": proposed_raw,
        "proposed_value": proposed_value,
        "badge_texts": " | ".join(badge_texts),
        "coupon_texts": " | ".join(coupon_texts),
        "verdict": _verdict(db_raw, proposed_value, badge_texts, coupon_texts),
    }


def _listing_rows(
    *,
    session: Any,
    product: str,
    cfg: Any,
    selectors: dict[str, dict[str, str | None]],
    main_limit: int,
    max_pages: int,
    sleep: float,
) -> list[dict[str, Any]]:
    base = selectors.get("base_container") or FALLBACK_MAIN_SELECTORS["base_container"]
    product_link = selectors.get("product_url") or FALLBACK_MAIN_SELECTORS["product_url"]
    current_discount = selectors.get("discount_type")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    rank = 1
    for page in range(1, max_pages + 1):
        if len(rows) >= main_limit:
            break
        url = _page_url(cfg.MAIN_URL, page)
        response = session.fetch(
            url,
            scroll_ratio=1.0,
            scroll_max_scrolls=8,
            post_load_sleep=max(sleep, 3.0),
        )
        driver = session.driver
        if driver is None:
            raise RuntimeError("Chrome driver가 생성되지 않았습니다.")
        cards = _elements(driver, base.get("xpath"))
        print(f"  main {page}페이지: HTTP={response.get('status')} 카드={len(cards)}")
        for card in cards:
            asin = _clean(card.get_attribute("data-asin"))
            href = _first_attr(card, product_link, "href")
            asin = asin or parsers.asin_from_url(href)
            if not asin or asin in seen:
                continue
            seen.add(asin)
            product_url = parsers.product_url_for_asin(href, asin)
            rows.append(
                _make_row(
                    product=product,
                    stage="main",
                    page=page,
                    rank=rank,
                    asin=asin,
                    product_url=product_url,
                    http_status=response.get("status"),
                    root=card,
                    db_selector=current_discount,
                    proposed_xpath=PROPOSED_MAIN_XPATH,
                )
            )
            rank += 1
            if len(rows) >= main_limit:
                break
    return rows


def _detail_targets(rows: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row.get("product_url")]
    candidates.sort(
        key=lambda row: (
            0 if row.get("proposed_value") else 1,
            0 if row.get("db_raw") and not row.get("db_policy_value") else 1,
            0 if row.get("coupon_texts") else 1,
            int(row.get("rank") or 999999),
        )
    )
    return candidates[: max(limit, 0)]


def _detail_rows(
    *,
    session: Any,
    product: str,
    selectors: dict[str, dict[str, str | None]],
    targets: list[dict[str, Any]],
    sleep: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_discount = selectors.get("discount_type")
    for index, target in enumerate(targets, start=1):
        url = str(target["product_url"])
        response = session.fetch(
            url,
            scroll_ratio=1.0,
            scroll_max_scrolls=15,
            post_load_sleep=max(sleep, 3.0),
        )
        driver = session.driver
        if driver is None:
            raise RuntimeError("Chrome driver가 생성되지 않았습니다.")
        loaded_url = response.get("url") or url
        asin = parsers.asin_from_url(loaded_url) or target.get("asin")
        row = _make_row(
            product=product,
            stage="detail",
            page=None,
            rank=index,
            asin=asin,
            product_url=url,
            http_status=response.get("status"),
            root=driver,
            db_selector=current_discount,
            proposed_xpath=PROPOSED_DETAIL_XPATH,
        )
        rows.append(row)
        print(
            f"  detail {index}/{len(targets)}: {asin} "
            f"DB={row.get('db_current_value') or '-'} 후보={row.get('proposed_value') or '-'}"
        )
    return rows


def _show_selectors(product: str, main: dict[str, Any], detail: dict[str, Any]) -> None:
    print(f"\n[{product.upper()} 현재 DB SELECT 결과]")
    print(f"  main discount_type : {(main.get('discount_type') or {}).get('xpath') or '(없음)'}")
    print(f"  detail discount_type: {(detail.get('discount_type') or {}).get('xpath') or '(없음)'}")


def _show_relevant(rows: list[dict[str, Any]]) -> None:
    relevant = [
        row for row in rows
        if row.get("db_raw") or row.get("proposed_raw") or row.get("badge_texts") or row.get("coupon_texts")
    ]
    if not relevant:
        print("  할인/배지/쿠폰 관련 DOM이 발견되지 않았습니다.")
        return
    for row in relevant:
        print(
            f"  [{row['verdict']}] {row['product']} {row['stage']} "
            f"ASIN={row.get('asin') or '-'}\n"
            f"    현재 DB: raw={row.get('db_raw') or '-'} -> value={row.get('db_current_value') or '-'}\n"
            f"    후보안  : raw={row.get('proposed_raw') or '-'} -> value={row.get('proposed_value') or '-'}"
        )
        if row.get("badge_texts"):
            print(f"    배지 DOM: {row['badge_texts']}")
        if row.get("coupon_texts"):
            print(f"    쿠폰 DOM: {row['coupon_texts']}")


def _write_csv(rows: list[dict[str, Any]], output: str | None) -> Path:
    if output:
        path = Path(output).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path(tempfile.gettempdir()) / f"seg_amzn_discount_type_{stamp}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _print_summary(rows: list[dict[str, Any]], output_path: Path) -> None:
    main_rows = [row for row in rows if row["stage"] == "main"]
    detail_rows = [row for row in rows if row["stage"] == "detail"]
    allowed = [row for row in rows if row.get("proposed_value")]
    current_invalid = [row for row in rows if row.get("db_raw") and not row.get("db_policy_value")]
    coupons = [row for row in rows if row.get("coupon_texts")]
    candidate_coupon_leaks = [row for row in coupons if row.get("proposed_value")]
    print("\n" + "=" * 78)
    print("점검 요약")
    print(f"  main 확인: {len(main_rows)}건")
    print(f"  detail 확인: {len(detail_rows)}건")
    print(f"  후보안 허용 딜: {len(allowed)}건")
    print(f"  현재 DB 선택자의 비허용값: {len(current_invalid)}건")
    print(f"  쿠폰 DOM 발견/후보안 유입: {len(coupons)}건 / {len(candidate_coupon_leaks)}건")
    print(f"  결과 CSV: {output_path}")
    print("  DB INSERT/UPDATE/DELETE: 0건 (selector SELECT만 실행)")
    print("  SQL 적용: 하지 않음")


def main() -> int:
    args = parse_args()
    if args.main_limit < 1 or args.detail_limit < 0 or args.max_pages < 1:
        raise SystemExit("main-limit은 1 이상, detail-limit은 0 이상, max-pages는 1 이상이어야 합니다.")
    if args.headless and args.keep_browser_open:
        print("[안내] headless 모드에서는 --keep-browser-open을 무시합니다.")

    print("=" * 78)
    print("SEG Amazon.de discount_type 실페이지 스모크 테스트")
    print("  브라우저: " + ("headless" if args.headless else "화면에 표시"))
    print(f"  main-limit: 상품군별 {args.main_limit}건 (페이지 수 아님)")
    print(f"  max-pages: 상품군별 최대 {args.max_pages}페이지")
    print(f"  detail-limit: 상품군별 {args.detail_limit}건")
    print("  DB 권한: read-only transaction / selector SELECT만 실행")
    print("  운영 테이블 저장·S3 업로드·메일 발송·SQL 변경: 없음")
    print("\n독일/인도 DOM 비교 기준")
    print("  독일 딜: Befristetes Angebot -> Limited Time Offer")
    print("  독일 쿠폰: Du zahlst ... Coupon ... angewendet -> 제외")
    print("  인도 딜: Limited time deal / Limited Time Offer / Hot deal / Ends in ...")
    print("  인도 쿠폰: You pay ... coupon applied -> 제외")

    selector_maps: dict[str, dict[str, dict[str, dict[str, str | None]]]] = {}
    for product in args.products:
        main_selectors = _read_active_selectors(product, "main")
        detail_selectors = _read_active_selectors(product, "detail")
        selector_maps[product] = {"main": main_selectors, "detail": detail_selectors}
        _show_selectors(product, main_selectors, detail_selectors)

    from common.browser import AmazonBrowserSession

    session = AmazonBrowserSession(
        postal_code="10117",
        sleep=args.sleep,
        headless=args.headless,
        set_postal=True,
    )
    all_rows: list[dict[str, Any]] = []
    try:
        for product in args.products:
            cfg = importlib.import_module(PRODUCT_CONFIGS[product])
            print(f"\n[{product.upper()} main 점검]")
            main_rows = _listing_rows(
                session=session,
                product=product,
                cfg=cfg,
                selectors=selector_maps[product]["main"],
                main_limit=args.main_limit,
                max_pages=args.max_pages,
                sleep=args.sleep,
            )
            all_rows.extend(main_rows)
            targets = _detail_targets(main_rows, args.detail_limit)
            print(f"[{product.upper()} detail 점검: {len(targets)}건]")
            all_rows.extend(
                _detail_rows(
                    session=session,
                    product=product,
                    selectors=selector_maps[product]["detail"],
                    targets=targets,
                    sleep=args.sleep,
                )
            )

        print("\n관련 값 상세")
        _show_relevant(all_rows)
        output_path = _write_csv(all_rows, args.output)
        _print_summary(all_rows, output_path)
        if args.keep_browser_open and not args.headless:
            input("\n직접 확인이 끝나면 Enter를 눌러 Chrome을 닫으세요: ")
    finally:
        session.close()
    return 0 if all_rows else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
