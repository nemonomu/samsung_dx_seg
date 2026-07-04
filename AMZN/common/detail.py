"""Step08: collect Amazon PDP/review detail fields for JSONL merge."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from common import parsers, selectors as selector_api
from common.config import DEFAULT_SLEEP, DEFAULT_TIMEOUT
from common.http import save_text
from common.io_util import category_output_root, category_reference_root, read_csv, write_csv, write_json


def review_url(product_url: str | None, asin: str | None) -> str | None:
    if not asin and product_url:
        asin = parsers.asin_from_url(product_url)
    return f"https://www.amazon.de/product-reviews/{asin}/?sortBy=helpful" if asin else None


def _norm_name(value: str | None) -> str:
    import re
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _crawl_datetime() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _base_detail_record(cfg, target: dict[str, Any], *, asin: str, product_url: str | None,
                        batch_id: str | None) -> dict[str, Any]:
    return {
        "account_name": getattr(cfg, "ACCOUNT_NAME", "Amazon.de"),
        "product": getattr(cfg, "PRODUCT", "").upper(),
        "stage": "detail",
        "source_url": product_url,
        "asin": asin,
        "item": asin,
        "product_url": product_url,
        "batch_id": batch_id or target.get("batch_id"),
        "crawl_datetime": _crawl_datetime(),
        "redirect": False,
    }


def run(cfg, *, limit: int = 0, start: int = 1, timeout: int = DEFAULT_TIMEOUT,
        sleep: float = DEFAULT_SLEEP, batch_id: str | None = None, emit=None,
        headless: bool | None = None) -> dict[str, Any]:
    del timeout
    out = category_output_root(cfg.PRODUCT)
    ref = category_reference_root(cfg.PRODUCT) / "detail" / datetime.now().strftime("%Y%m%d_%H%M%S")
    targets = read_csv(out / "amzn_final_targets.csv")
    start_i = max(start, 1) - 1
    selected = targets[start_i:] if limit <= 0 else targets[start_i:start_i + limit]
    selector_map = selector_api.load_selectors("detail", domain="product")
    rows: list[dict[str, Any]] = []
    attempts = []
    session = None
    try:
        from common.browser import AmazonBrowserSession
        session = AmazonBrowserSession(
            postal_code=getattr(cfg, "POSTAL_CODE", "10117"),
            sleep=sleep,
            headless=headless,
        )
        for idx, target in enumerate(selected, start=start_i + 1):
            asin = (target.get("asin") or target.get("item") or "").strip()
            product_url = target.get("product_url")
            detail = _base_detail_record(cfg, target, asin=asin, product_url=product_url, batch_id=batch_id)
            review = {"status": None, "text": "", "error": "review_not_requested", "bytes": 0}
            pdp_review: dict[str, Any] = {}
            if product_url:
                pdp = session.fetch(product_url, scroll_ratio=1.0)
            else:
                pdp = {"status": None, "text": "", "error": "missing_url", "bytes": 0, "url": product_url}
            save_text(ref / f"{idx:04d}_{asin}_pdp.html", pdp["text"])

            parsed_detail = selector_api.extract_detail(session.driver, selector_map, product=cfg.PRODUCT) if session.driver is not None and pdp.get("text") else {}
            landing_url = pdp.get("url") or product_url
            landing_asin = parsers.asin_from_url(landing_url)
            use_detail = True
            redirect_decision = None
            if asin and landing_asin and asin != landing_asin:
                listing_name = target.get("retailer_sku_name")
                landing_name = parsed_detail.get("retailer_sku_name")
                detail.update({
                    "redirect": True,
                    "landing_url": landing_url,
                    "landing_asin": landing_asin,
                    "_original_asin": asin,
                    "_listing_retailer_sku_name": listing_name or None,
                    "_landing_retailer_sku_name": landing_name,
                })
                if listing_name and landing_name and _norm_name(listing_name) == _norm_name(landing_name):
                    redirect_decision = "same_name_collect_landing"
                    detail["_redirect_use_landing"] = True
                    detail["item"] = landing_asin
                else:
                    redirect_decision = "name_mismatch_listing_only"
                    detail["_detail_skip"] = "asin_mismatch"
                    use_detail = False
                detail["_redirect_decision"] = redirect_decision

            if use_detail:
                detail.update({k: v for k, v in parsed_detail.items() if v not in (None, "")})
                detail["item"] = landing_asin or asin
                detail["product_url"] = product_url
                pdp_review = parsers.parse_review_html(pdp["text"]) if pdp.get("text") else {}
                if not detail.get("detailed_review_content"):
                    detail.update({k: v for k, v in pdp_review.items() if v not in (None, "")})
                r_url = review_url(landing_url if use_detail else product_url, landing_asin if use_detail else asin)
                if not detail.get("detailed_review_content") and r_url:
                    review = session.fetch(r_url, scroll_ratio=1.0)
                    detail.update({k: v for k, v in parsers.parse_review_html(review["text"]).items() if v not in (None, "")})
            save_text(ref / f"{idx:04d}_{asin}_reviews.html", review["text"])
            detail["loaded_url"] = landing_url
            detail["redirect_decision"] = redirect_decision
            rows.append(detail)
            if emit:
                emit(detail)
            attempts.append({
                "rank": idx,
                "asin": asin,
                "loaded_asin": landing_asin,
                "pdp_status": pdp.get("status"),
                "review_status": review.get("status"),
                "redirect": detail.get("redirect"),
                "redirect_decision": redirect_decision,
                "detail_skip": detail.get("_detail_skip"),
                "pdp_error": pdp.get("error"),
                "review_error": review.get("error"),
                "pdp_review_count": pdp_review.get("count_of_reviews"),
            })
            print(f"[detail/{cfg.PRODUCT}] rank={idx} asin={asin} pdp={pdp.get('status')} review={review.get('status')} redirect={redirect_decision or detail.get('redirect')}", flush=True)
            time.sleep(sleep)
    finally:
        if session is not None:
            session.close()

    path = out / "amzn_detail.csv"
    write_csv(path, rows)
    manifest = {"run_type": "detail", "product": cfg.PRODUCT, "rows": len(rows), "output": str(path), "raw_dir": str(ref), "attempts": attempts, "selector_source": "db_or_default_xpath"}
    write_json(out / "step08_detail_review_compare_manifest.json", manifest)
    manifest["rows_data"] = rows
    return manifest
