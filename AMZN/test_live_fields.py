"""Interactive live test for selected Amazon collection fields.

The test reads active DB selectors and opens only explicitly supplied URLs. It
does not write to the retail DB, upload to S3, send email, or save raw HTML.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from typing import Any

from common import parsers, selectors as selector_api


PRODUCT_CONFIGS = {
    "tv": "TV.config",
    "ref": "REF.config",
}
STAGES = ("detail", "main", "bsr")
STRUCTURAL_FIELDS = {"base_container", "expand_additional_details", "expand_item_details"}
META_FIELDS = {
    "detail": ("asin", "item", "product_url"),
    "main": ("asin", "item", "product_url", "main_rank"),
    "bsr": ("asin", "item", "product_url", "bsr_rank"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live-test selected Amazon fields without external writes.")
    parser.add_argument("--product", choices=sorted(PRODUCT_CONFIGS))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--fields", help="Comma-separated field numbers or exact field names")
    parser.add_argument("--url", action="append", help="Repeat once for a second URL")
    parser.add_argument("--max-records", type=int, default=5, help="Maximum listing records per URL")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--confirm-live", action="store_true")
    return parser.parse_args()


def _choose(label: str, choices: tuple[str, ...]) -> str:
    print(f"\n{label}")
    for index, value in enumerate(choices, start=1):
        print(f"  {index}. {value}")
    while True:
        raw = input("번호 입력: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print("올바른 번호를 입력하세요.")


def _available_fields(stage: str, selector_map: dict[str, dict[str, str | None]]) -> tuple[str, ...]:
    fields = list(META_FIELDS[stage])
    fields.extend(sorted(field for field in selector_map if field not in STRUCTURAL_FIELDS))
    return tuple(dict.fromkeys(fields))


def _parse_fields(raw: str, available: tuple[str, ...]) -> tuple[str, ...]:
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    selected: list[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(available):
            field = available[int(token) - 1]
        elif token in available:
            field = token
        else:
            raise ValueError(f"unknown field selection: {token}")
        if field not in selected:
            selected.append(field)
    if not selected:
        raise ValueError("select at least one field")
    return tuple(selected)


def _choose_fields(available: tuple[str, ...], requested: str | None) -> tuple[str, ...]:
    print("\n수집 가능 컬럼")
    for index, field in enumerate(available, start=1):
        print(f"  {index}. {field}")
    if requested:
        return _parse_fields(requested, available)
    while True:
        raw = input("수집할 컬럼 번호 입력 (예: 1,2,3): ").strip()
        try:
            return _parse_fields(raw, available)
        except ValueError as exc:
            print(exc)


def _urls(requested: list[str] | None) -> list[str]:
    if requested:
        urls = [url.strip() for url in requested if url.strip()]
    else:
        first = input("\nURL 1 입력: ").strip()
        second = input("URL 2 입력 (없으면 Enter): ").strip()
        urls = [url for url in (first, second) if url]
    if not 1 <= len(urls) <= 2:
        raise ValueError("provide one or two URLs")
    return urls


def _confirm(already_confirmed: bool, *, product: str, stage: str, fields: tuple[str, ...], urls: list[str]) -> None:
    print("\n실행 내용")
    print(f"  product: {product}")
    print(f"  stage: {stage}")
    print(f"  fields: {', '.join(fields)}")
    print(f"  urls: {len(urls)}")
    print("  external writes: none")
    if already_confirmed:
        return
    answer = input("Amazon.de 실사이트 요청을 실행하려면 YES 입력: ").strip()
    if answer != "YES":
        raise SystemExit("live test cancelled")


def _selector_subset(
    stage: str,
    selector_map: dict[str, dict[str, str | None]],
    fields: tuple[str, ...],
) -> dict[str, dict[str, str | None]]:
    required = set(fields)
    if stage == "detail":
        required.update(selector_api.EXPAND_FIELDS)
    else:
        required.update({"base_container", "product_url"})
    return {field: selector for field, selector in selector_map.items() if field in required}


def _filter_record(record: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: record.get(field) for field in fields}


def _detail_records(
    *,
    product: str,
    input_url: str,
    response: dict[str, Any],
    driver: Any,
    selectors: dict[str, dict[str, str | None]],
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    extracted = selector_api.extract_detail(driver, selectors, product=product)
    loaded_url = response.get("url") or input_url
    asin = parsers.asin_from_url(loaded_url) or parsers.asin_from_url(input_url)
    extracted.update({
        "asin": asin,
        "item": asin,
        "product_url": input_url,
    })
    return [_filter_record(extracted, fields)]


def _listing_records(
    *,
    stage: str,
    driver: Any,
    selectors: dict[str, dict[str, str | None]],
    fields: tuple[str, ...],
    max_records: int,
) -> list[dict[str, Any]]:
    # The production BSR discovery pass intentionally reduces each row to its
    # ASIN, URL, and rank. This tester uses the common DB-XPath card extractor
    # so any explicitly selected BSR field can be inspected as well.
    records = selector_api.extract_cards(driver, selectors, sort=stage, start_rank=1)
    return [_filter_record(record, fields) for record in records[:max_records]]


def main() -> int:
    args = parse_args()
    product = args.product or _choose("상품군 선택", tuple(sorted(PRODUCT_CONFIGS)))
    stage = args.stage or _choose("수집 단계 선택", STAGES)
    cfg = importlib.import_module(PRODUCT_CONFIGS[product])
    selector_map = selector_api.load_selectors(stage, domain=product)
    available = _available_fields(stage, selector_map)
    fields = _choose_fields(available, args.fields)
    urls = _urls(args.url)
    _confirm(args.confirm_live, product=product, stage=stage, fields=fields, urls=urls)

    from common.browser import AmazonBrowserSession

    session = AmazonBrowserSession(
        postal_code=getattr(cfg, "POSTAL_CODE", "10117"),
        sleep=args.sleep,
        headless=args.headless,
    )
    selectors = _selector_subset(stage, selector_map, fields)
    output: list[dict[str, Any]] = []
    try:
        for url in urls:
            response = session.fetch(
                url,
                scroll_ratio=1.0,
                scroll_max_scrolls=15 if stage == "detail" else 10,
                post_load_sleep=max(args.sleep, 3.0),
            )
            if session.driver is None:
                raise RuntimeError("browser driver was not created")
            if stage == "detail":
                records = _detail_records(
                    product=product.upper(),
                    input_url=url,
                    response=response,
                    driver=session.driver,
                    selectors=selectors,
                    fields=fields,
                )
            else:
                records = _listing_records(
                    stage=stage,
                    driver=session.driver,
                    selectors=selectors,
                    fields=fields,
                    max_records=max(1, args.max_records),
                )
            output.append({
                "input_url": url,
                "loaded_url": response.get("url") or url,
                "status": response.get("status"),
                "error": response.get("error"),
                "records": records,
            })
    finally:
        session.close()

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(item.get("records") for item in output) else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
