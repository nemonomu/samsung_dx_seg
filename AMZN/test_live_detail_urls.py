"""Live test for one or two Amazon detail URLs.

This script reads DB selectors and opens only the explicitly supplied URLs. It
does not run listing/BSR collection, write to the retail DB, upload to S3, or
send email.
"""
from __future__ import annotations

import argparse
import importlib
import json
from typing import Any

from common import parsers, selectors as selector_api


PRODUCT_CONFIGS = {
    "tv": "TV.config",
    "ref": "REF.config",
}
SMOKE_FIELDS = ("retailer_sku_name", "sku", "final_sku_price")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live-test 1-2 Amazon detail URLs without external writes.")
    parser.add_argument("--product", required=True, choices=sorted(PRODUCT_CONFIGS))
    parser.add_argument("--url", action="append", required=True, help="Amazon detail URL; repeat once for a second URL")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required acknowledgement that this command sends live requests to Amazon.de",
    )
    args = parser.parse_args()
    if not 1 <= len(args.url) <= 2:
        parser.error("provide one or two --url values")
    if not args.confirm_live:
        parser.error("--confirm-live is required for live Amazon requests")
    return args


def _selector_subset(selector_map: dict[str, dict[str, str | None]]) -> dict[str, dict[str, str | None]]:
    return {field: selector_map[field] for field in SMOKE_FIELDS if field in selector_map}


def _result(
    *,
    product: str,
    input_url: str,
    response: dict[str, Any],
    driver: Any,
    selectors: dict[str, dict[str, str | None]],
) -> dict[str, Any]:
    raw_sku = selector_api.extract_single(driver, selectors.get("sku"))
    parsed = parsers.parse_product_detail_html(response.get("text") or "", product=product)
    extracted = selector_api.extract_detail(driver, selectors, product=product)
    loaded_url = response.get("url") or input_url
    return {
        "product": product,
        "input_url": input_url,
        "loaded_url": loaded_url,
        "status": response.get("status"),
        "input_asin": parsers.asin_from_url(input_url),
        "loaded_asin": parsers.asin_from_url(loaded_url),
        "db_xpath_sku_raw": raw_sku,
        "structured_sku": parsed.get("sku"),
        "final_sku": extracted.get("sku"),
        "final_sku_price": extracted.get("final_sku_price"),
        "retailer_sku_name": extracted.get("retailer_sku_name"),
        "error": response.get("error"),
    }


def main() -> int:
    args = parse_args()
    cfg = importlib.import_module(PRODUCT_CONFIGS[args.product])
    selector_map = selector_api.load_selectors("detail", domain=args.product)
    selectors = _selector_subset(selector_map)
    if "sku" not in selectors:
        raise RuntimeError(f"active detail sku selector not found for domain={args.product}")

    from common.browser import AmazonBrowserSession

    session = AmazonBrowserSession(
        postal_code=getattr(cfg, "POSTAL_CODE", "10117"),
        sleep=args.sleep,
        headless=args.headless,
    )
    results: list[dict[str, Any]] = []
    try:
        for url in args.url:
            response = session.fetch(
                url,
                scroll_ratio=1.0,
                scroll_max_scrolls=15,
                post_load_sleep=max(args.sleep, 3.0),
            )
            if session.driver is None:
                raise RuntimeError("browser driver was not created")
            results.append(
                _result(
                    product=args.product.upper(),
                    input_url=url,
                    response=response,
                    driver=session.driver,
                    selectors=selectors,
                )
            )
    finally:
        session.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("final_sku") for item in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
