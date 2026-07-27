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


REF_CORE_FIELDS = ("sku", "ref_capacity", "ref_refrigerator_type")
NORMAL_PDP_CONTAINERS = (
    "#dp",
    "#ppd",
    "#centerCol",
    "#productOverview_feature_div",
    "#detailBullets_feature_div",
    "#productDetails_feature_div",
)


def review_url(product_url: str | None, asin: str | None) -> str | None:
    if not asin and product_url:
        asin = parsers.asin_from_url(product_url)
    return f"https://www.amazon.de/product-reviews/{asin}/?sortBy=helpful" if asin else None


def _norm_name(value: str | None) -> str:
    import re
    return re.sub(r"\s+", " ", value or "").strip()


def _norm_url(value: str | None) -> str:
    return (parsers.canonical_url(value) or "").rstrip("/")


def _urls_differ(listing_url: str | None, landing_url: str | None) -> bool:
    listing = _norm_url(listing_url)
    landing = _norm_url(landing_url)
    return bool(listing and landing and listing != landing)


def _extract_landing_name(driver, selector_map: dict[str, Any]) -> str | None:
    if driver is None:
        return None
    value = selector_api.extract_single(driver, selector_map.get("retailer_sku_name"))
    return selector_api.normalize_field("retailer_sku_name", value) if value else None


def _has_nonempty_product_title(driver: Any | None) -> bool:
    if driver is None:
        return False
    try:
        return any((element.text or "").strip() for element in driver.find_elements("css selector", "#productTitle"))
    except Exception:  # Browser failures are treated as an incomplete PDP and retried once.
        return False


def _has_normal_pdp_container(driver: Any | None) -> bool:
    if driver is None:
        return False
    try:
        return any(driver.find_elements("css selector", selector) for selector in NORMAL_PDP_CONTAINERS)
    except Exception:  # Browser failures are treated as an incomplete PDP and retried once.
        return False


def _ref_retry_reason(driver: Any | None, parsed_detail: dict[str, Any]) -> str | None:
    """Return the single retry reason for an incomplete REF PDP, if applicable."""
    reasons = []
    if not _has_nonempty_product_title(driver):
        reasons.append("missing_title")
    if not _has_normal_pdp_container(driver):
        reasons.append("missing_container")
    if all(parsed_detail.get(field) in (None, "") for field in REF_CORE_FIELDS):
        reasons.append("core_fields_empty")
    return ",".join(reasons) or None


def _browser_retry_reason(pdp: dict[str, Any]) -> str | None:
    """Return a retry reason for transport failures, excluding Amazon interstitials."""
    error = str(pdp.get("error") or "")
    if pdp.get("status") == 429 or error == "amazon_interstitial":
        return None
    if "timeoutexception" in error.lower() or "timed out receiving message" in error.lower():
        return "timeout"
    if pdp.get("status") is None:
        return "browser_failure"
    if not pdp.get("text"):
        return "empty_html"
    return None


def _detail_quality(driver: Any | None, parsed_detail: dict[str, Any]) -> tuple[int, int]:
    """Prefer a structurally normal PDP, then the result with more REF core fields."""
    structure_ok = int(_has_nonempty_product_title(driver) and _has_normal_pdp_container(driver))
    core_count = sum(parsed_detail.get(field) not in (None, "") for field in REF_CORE_FIELDS)
    return structure_ok, core_count


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
            if product_url:
                first_pdp = session.fetch(
                    product_url,
                    scroll_ratio=1.0,
                    scroll_max_scrolls=15,
                    post_load_sleep=max(sleep, 3.0),
                )
            else:
                first_pdp = {"status": None, "text": "", "error": "missing_url", "bytes": 0, "url": product_url}
            pdp = first_pdp
            selected_pdp = first_pdp
            retry_reason = _browser_retry_reason(first_pdp) if product_url else None
            retry_pdp = None
            retry_final_reason = None
            if retry_reason:
                logger.warning("asin=%s browser retry requested reason=%s", asin, retry_reason)
                retry_pdp = session.refetch(
                    product_url,
                    wait_range=(5.0, 10.0),
                    scroll_ratio=1.0,
                    scroll_max_scrolls=15,
                    post_load_sleep=max(sleep, 3.0),
                )
                detail["retry_attempted"] = True
                detail["retry_reason"] = retry_reason
                detail["retry_mode"] = retry_pdp.get("retry_mode", "normal_cache")
                detail["retry_wait_seconds"] = retry_pdp.get("retry_wait_seconds")
                if retry_pdp.get("status") == 200 and retry_pdp.get("text"):
                    pdp = retry_pdp
                    selected_pdp = retry_pdp
                    detail["retry_selected_attempt"] = "retry"
                else:
                    detail["retry_selected_attempt"] = "first"
                    retry_final_reason = retry_reason
                    detail["retry_final_reason"] = retry_final_reason
                    logger.warning("asin=%s browser retry failed reason=%s", asin, retry_final_reason)
            if save_html:
                save_text(ref / f"{idx:04d}_{asin}_pdp.html", pdp["text"])

            landing_url = pdp.get("url") or product_url
            landing_asin = parsers.asin_from_url(landing_url)
            parsed_detail = {}
            use_detail = True
            redirect_decision = None
            if _urls_differ(product_url, landing_url):
                listing_name = target.get("retailer_sku_name")
                landing_name = _extract_landing_name(session.driver, selector_map) if pdp.get("text") else None
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
                    detail["item"] = landing_asin or asin
                else:
                    redirect_decision = "name_mismatch_listing_only"
                    detail["_detail_skip"] = "url_mismatch_name_mismatch"
                    use_detail = False
                detail["_redirect_decision"] = redirect_decision

            if use_detail:
                parsed_detail = selector_api.extract_detail(session.driver, selector_map, product=cfg.PRODUCT) if session.driver is not None and pdp.get("text") else {}
                if (
                    str(cfg.PRODUCT).lower() == "ref"
                    and product_url
                    and not detail.get("retry_attempted")
                    and pdp.get("status") != 429
                    and pdp.get("error") != "amazon_interstitial"
                ):
                    retry_reason = _ref_retry_reason(session.driver, parsed_detail)
                    if retry_reason:
                        first_quality = _detail_quality(session.driver, parsed_detail)
                        logger.warning("asin=%s pdp retry requested reason=%s", asin, retry_reason)
                        retry_pdp = session.refetch_without_cache(
                            product_url,
                            wait_range=(2.0, 4.0),
                            scroll_ratio=1.0,
                            scroll_max_scrolls=15,
                            post_load_sleep=max(sleep, 3.0),
                        )
                        retry_detail = (
                            selector_api.extract_detail(session.driver, selector_map, product=cfg.PRODUCT)
                            if session.driver is not None and retry_pdp.get("text") else {}
                        )
                        retry_landing_url = retry_pdp.get("url") or product_url
                        retry_landing_asin = parsers.asin_from_url(retry_landing_url)
                        retry_redirect_allowed = True
                        retry_redirect_decision = None
                        if _urls_differ(product_url, retry_landing_url):
                            listing_name = target.get("retailer_sku_name")
                            retry_landing_name = (
                                _extract_landing_name(session.driver, selector_map)
                                if retry_pdp.get("text") else None
                            )
                            detail.update({
                                "redirect": True,
                                "landing_url": retry_landing_url,
                                "landing_asin": retry_landing_asin,
                                "_original_asin": asin,
                                "_listing_retailer_sku_name": listing_name or None,
                                "_landing_retailer_sku_name": retry_landing_name,
                            })
                            if (
                                listing_name and retry_landing_name
                                and _norm_name(listing_name) == _norm_name(retry_landing_name)
                            ):
                                retry_redirect_decision = "same_name_collect_landing"
                            else:
                                retry_redirect_decision = "name_mismatch_listing_only"
                                retry_redirect_allowed = False
                                detail["_detail_skip"] = "url_mismatch_name_mismatch"
                            redirect_decision = retry_redirect_decision
                            detail["_redirect_decision"] = redirect_decision

                        retry_incomplete_reason = _ref_retry_reason(session.driver, retry_detail)
                        if retry_redirect_allowed and _detail_quality(session.driver, retry_detail) > first_quality:
                            parsed_detail = retry_detail
                            selected_pdp = retry_pdp
                            landing_url = retry_landing_url
                            landing_asin = retry_landing_asin
                            detail["retry_selected_attempt"] = "retry"
                            if retry_redirect_decision == "same_name_collect_landing":
                                detail["_redirect_use_landing"] = True
                            elif not _urls_differ(product_url, retry_landing_url):
                                detail.pop("_redirect_use_landing", None)
                        else:
                            detail["retry_selected_attempt"] = "first"
                            if not retry_redirect_allowed:
                                parsed_detail = {}
                                landing_url = retry_landing_url
                                landing_asin = retry_landing_asin
                                detail.pop("_redirect_use_landing", None)
                        detail["retry_attempted"] = True
                        detail["retry_reason"] = retry_reason
                        if not retry_redirect_allowed:
                            retry_final_reason = "redirect_name_mismatch"
                        elif detail["retry_selected_attempt"] == "retry":
                            retry_final_reason = retry_incomplete_reason
                        else:
                            retry_final_reason = retry_reason
                        detail["retry_final_reason"] = retry_final_reason
                        if retry_final_reason:
                            logger.warning(
                                "asin=%s pdp still incomplete after retry reason=%s",
                                asin,
                                retry_final_reason,
                            )
                detail.update({k: v for k, v in parsed_detail.items() if v not in (None, "")})
                detail["item"] = asin if detail.get("_detail_skip") else landing_asin or asin
                detail["product_url"] = product_url
                r_url = review_url(landing_url if use_detail else product_url, landing_asin if use_detail else asin)
                if (
                    not detail.get("_detail_skip")
                    and not detail.get("detailed_review_content")
                    and r_url
                    and review_page_fallback
                ):
                    review = session.fetch(
                        r_url,
                        scroll_ratio=1.0,
                        scroll_max_scrolls=8,
                        post_load_sleep=max(sleep, 3.0),
                    )
                    if session.driver is not None and review.get("text"):
                        review_detail = selector_api.extract_detail(session.driver, selector_map, product=cfg.PRODUCT)
                        detail.update({k: v for k, v in review_detail.items() if v not in (None, "") and detail.get(k) in (None, "")})
            if save_html:
                save_text(ref / f"{idx:04d}_{asin}_reviews.html", review["text"])
            detail["loaded_url"] = landing_url
            detail["redirect_decision"] = redirect_decision
            review_text = bool(detail.get("detailed_review_content"))
            review_page_status = review.get("status")
            rows.append(detail)
            siel_log.warn_price_logic(logger, detail)
            siel_log.log_record_summary(logger, detail)
            siel_log.log_detail_result(logger, detail, cfg.PRODUCT)
            progress.update(logger, detail)
            if emit:
                emit(detail)
            attempts.append({
                "rank": idx,
                "asin": asin,
                "loaded_asin": landing_asin,
                "pdp_status": first_pdp.get("status"),
                "first_pdp_status": first_pdp.get("status"),
                "first_pdp_error": first_pdp.get("error"),
                "retry_attempted": detail.get("retry_attempted", False),
                "retry_reason": detail.get("retry_reason"),
                "retry_mode": detail.get("retry_mode"),
                "retry_wait_seconds": detail.get("retry_wait_seconds"),
                "retry_pdp_status": retry_pdp.get("status") if retry_pdp else None,
                "retry_final_reason": retry_final_reason,
                "selected_attempt": detail.get("retry_selected_attempt", "first"),
                "selected_pdp_status": selected_pdp.get("status"),
                "final_core_field_count": (
                    sum(parsed_detail.get(field) not in (None, "") for field in REF_CORE_FIELDS)
                    if str(cfg.PRODUCT).lower() == "ref" else None
                ),
                "review_page_status": review_page_status,
                "review_text": review_text,
                "redirect": detail.get("redirect"),
                "redirect_decision": redirect_decision,
                "detail_skip": detail.get("_detail_skip"),
                "pdp_error": first_pdp.get("error"),
                "retry_pdp_error": retry_pdp.get("error") if retry_pdp else None,
                "review_error": review.get("error"),
            })
            logger.info("rank=%d asin=%s pdp=%s review_text=%s review_page=%s redirect=%s detail_skip=%s", idx, asin, pdp.get("status"), review_text, review_page_status, redirect_decision or detail.get("redirect"), detail.get("_detail_skip"))
            print(f"[detail/{cfg.PRODUCT}] rank={idx} asin={asin} pdp={pdp.get('status')} review_text={review_text} review_page={review_page_status or '-'} redirect={redirect_decision or detail.get('redirect')}", flush=True)
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
