"""Step08: collect Amazon PDP/review detail fields for JSONL merge."""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

from common import parsers, selectors as selector_api, siel_logging as siel_log
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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


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
        headless: bool | None = None, session: Any | None = None,
        save_html: bool | None = None, review_page_fallback: bool | None = None) -> dict[str, Any]:
    del timeout
    out = category_output_root(cfg.PRODUCT)
    ref = category_reference_root(cfg.PRODUCT) / "detail" / datetime.now().strftime("%Y%m%d_%H%M%S")
    targets = read_csv(out / "amzn_final_targets.csv")
    start_i = max(start, 1) - 1
    selected = targets[start_i:] if limit <= 0 else targets[start_i:start_i + limit]
    selector_map = selector_api.load_selectors("detail", domain=cfg.PRODUCT.lower())
    logger, _html_path = siel_log.setup(getattr(cfg, "ACCOUNT_NAME", "Amazon.de"), cfg.PRODUCT.lower(), "detail")
    siel_log.log_selectors(logger, selector_map)
    if batch_id:
        logger.info("batch_id=%s", batch_id)
    progress = siel_log.DetailProgress(len(selected))
    rows: list[dict[str, Any]] = []
    attempts = []
    save_html = _truthy(os.getenv("AMZN_SAVE_HTML")) if save_html is None else save_html
    review_page_fallback = (
        _truthy(os.getenv("AMZN_REVIEW_PAGE_FALLBACK"))
        if review_page_fallback is None else review_page_fallback
    )
    inter_detail_sleep = _env_float("AMZN_INTER_DETAIL_SLEEP", 0.0)
    own_session = False
    logger.info(
        "detail targets=%d start=%d limit=%d save_html=%s review_page_fallback=%s shared_session=%s inter_detail_sleep=%s",
        len(selected), start, limit, save_html, review_page_fallback, session is not None, inter_detail_sleep,
    )
    try:
        if session is None:
            from common.browser import AmazonBrowserSession
            session = AmazonBrowserSession(
                postal_code=getattr(cfg, "POSTAL_CODE", "10117"),
                sleep=sleep,
                headless=headless,
            )
            own_session = True
        for idx, target in enumerate(selected, start=start_i + 1):
            asin = (target.get("asin") or target.get("item") or "").strip()
            logger.info("rank=%d asin=%s url=%s", idx, asin, target.get("product_url"))
            product_url = target.get("product_url")
            detail = _base_detail_record(cfg, target, asin=asin, product_url=product_url, batch_id=batch_id)
            review = {"status": None, "text": "", "error": "review_not_requested", "bytes": 0}
            pdp_review: dict[str, Any] = {}
            if product_url:
                pdp = session.fetch(product_url, scroll_ratio=1.0)
            else:
                pdp = {"status": None, "text": "", "error": "missing_url", "bytes": 0, "url": product_url}
            if save_html:
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
                if not detail.get("detailed_review_content") and r_url and review_page_fallback:
                    review = session.fetch(
                        r_url,
                        scroll_ratio=1.0,
                        scroll_max_scrolls=8,
                        post_load_sleep=max(sleep, 3.0),
                    )
                    detail.update({k: v for k, v in parsers.parse_review_html(review["text"]).items() if v not in (None, "")})
            if save_html:
                save_text(ref / f"{idx:04d}_{asin}_reviews.html", review["text"])
            detail["loaded_url"] = landing_url
            detail["redirect_decision"] = redirect_decision
            review_text = bool(detail.get("detailed_review_content"))
            review_count = detail.get("count_of_reviews") or pdp_review.get("count_of_reviews")
            review_page_status = review.get("status")
            rows.append(detail)
            siel_log.warn_price_logic(logger, detail)
            siel_log.log_record_summary(logger, detail)
            progress.update(logger, detail)
            if emit:
                emit(detail)
            attempts.append({
                "rank": idx,
                "asin": asin,
                "loaded_asin": landing_asin,
                "pdp_status": pdp.get("status"),
                "review_page_status": review_page_status,
                "review_text": review_text,
                "review_count": review_count,
                "redirect": detail.get("redirect"),
                "redirect_decision": redirect_decision,
                "detail_skip": detail.get("_detail_skip"),
                "pdp_error": pdp.get("error"),
                "review_error": review.get("error"),
                "pdp_review_count": pdp_review.get("count_of_reviews"),
            })
            logger.info("rank=%d asin=%s pdp=%s review_text=%s review_count=%s review_page=%s redirect=%s detail_skip=%s", idx, asin, pdp.get("status"), review_text, review_count, review_page_status, redirect_decision or detail.get("redirect"), detail.get("_detail_skip"))
            print(f"[detail/{cfg.PRODUCT}] rank={idx} asin={asin} pdp={pdp.get('status')} review_text={review_text} review_count={review_count or '-'} review_page={review_page_status or '-'} redirect={redirect_decision or detail.get('redirect')}", flush=True)
            if inter_detail_sleep > 0:
                time.sleep(inter_detail_sleep)
    finally:
        if own_session and session is not None:
            session.close()

    path = out / "amzn_detail.csv"
    write_csv(path, rows)
    manifest = {"run_type": "detail", "product": cfg.PRODUCT, "rows": len(rows), "output": str(path), "raw_dir": str(ref) if save_html else "", "raw_saved": save_html, "review_page_fallback": review_page_fallback, "attempts": attempts, "selector_source": "db_xpath"}
    write_json(out / "step08_detail_review_compare_manifest.json", manifest)
    logger.info("=== done: records=%d batch_id=%s ===", len(rows), batch_id)
    manifest["rows_data"] = rows
    return manifest
