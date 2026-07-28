"""Step01/BSR: collect Amazon listing pages into per-source CSVs/JSONL."""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from typing import Any

from selenium.common.exceptions import WebDriverException

from common import parsers, selectors as selector_api, siel_logging as siel_log
from common.config import BSR_TARGET, DEFAULT_SLEEP, DEFAULT_TIMEOUT, LISTING_TARGET
from common.http import add_query, save_text
from common.io_util import category_output_root, category_reference_root, ensure_dirs, write_csv, write_json


class MainListingUnavailableError(RuntimeError):
    """Raised when the first main listing page cannot be recovered safely."""


def page_url(cfg, sort: str, page: int) -> str:
    if sort == "bsr":
        return cfg.BSR_URL if page <= 1 else add_query(cfg.BSR_URL, pg=page)
    return cfg.MAIN_URL if page <= 1 else add_query(cfg.MAIN_URL, page=page)


def _crawl_datetime() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


_LISTING_NORMALIZE_FIELDS = {
    "final_sku_price",
    "original_sku_price",
    "discount_type",
    "sku_popularity",
    "sku_status",
    "star_rating",
    "count_of_star_ratings",
    "number_of_units_purchased_past_month",
    "inventory_status",
}


def _main_page_failure_reason(resp: dict[str, Any], parsed: list[dict[str, Any]]) -> str | None:
    """Return why the first main listing page is unsafe to accept."""
    error = str(resp.get("error") or "").strip()
    if error:
        return error
    if resp.get("status") != 200:
        return f"status_{resp.get('status')}"
    if not parsed:
        return "listing_cards_empty"
    return None


def _normalize_listing_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in list(row.keys()):
        if key in _LISTING_NORMALIZE_FIELDS:
            row[key] = selector_api.normalize_field(key, row.get(key))
    return row


def _apply_record_meta(cfg, row: dict[str, Any], *, sort: str, page: int, source_url: str,
                       batch_id: str | None) -> dict[str, Any]:
    row.update({
        "account_name": getattr(cfg, "ACCOUNT_NAME", "Amazon.de"),
        "product": getattr(cfg, "PRODUCT", "").upper(),
        "stage": sort,
        "page_no": page,
        "source_url": source_url,
        "batch_id": batch_id,
        "crawl_datetime": _crawl_datetime(),
    })
    return row


def run(cfg, *, sort: str = "main", target: int | None = None, max_pages: int = 30,
        timeout: int = DEFAULT_TIMEOUT, sleep: float = DEFAULT_SLEEP, input_html: str = "",
        batch_id: str | None = None, emit=None, headless: bool | None = None,
        page_load_strategy: str | None = None, session: Any | None = None,
        save_html: bool | None = None) -> dict[str, Any]:
    del timeout  # Selenium session owns page timeouts.
    ensure_dirs(cfg.PRODUCT)
    out = category_output_root(cfg.PRODUCT)
    ref = category_reference_root(cfg.PRODUCT) / "listing" / f"{sort}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    target = target or (BSR_TARGET if sort == "bsr" else LISTING_TARGET)
    selector_map = selector_api.load_selectors(sort, domain=cfg.PRODUCT.lower())
    logger, _html_path = siel_log.setup(getattr(cfg, "ACCOUNT_NAME", "Amazon.de"), cfg.PRODUCT.lower(), sort)
    siel_log.log_selectors(logger, selector_map)
    if batch_id:
        logger.info("batch_id=%s", batch_id)
    rows: list[dict[str, Any]] = []
    pages = []
    fatal_main_reason: str | None = None
    warmup_summary: dict[str, Any] | None = None
    save_html = _truthy(os.getenv("AMZN_SAVE_HTML")) if save_html is None else save_html
    inter_page_sleep = _env_float("AMZN_INTER_PAGE_SLEEP", 0.0)
    own_session = False
    logger.info(
        "save_html=%s shared_session=%s inter_page_sleep=%s",
        save_html, session is not None, inter_page_sleep,
    )

    try:
        if input_html:
            html = open(input_html, encoding="utf-8", errors="replace").read()
            rows = parsers.parse_bsr_html(html) if sort == "bsr" else parsers.parse_listing_html(html, page=1, sort=sort)
            rows = [_apply_record_meta(cfg, _normalize_listing_row(r), sort=sort, page=1, source_url=input_html, batch_id=batch_id) for r in rows]
            pages.append({"page": 1, "url": input_html, "status": "file", "parsed_rows": len(rows)})
            logger.info("page=%d file=%s records=%d", 1, input_html, len(rows))
        else:
            if session is None:
                from common.browser import AmazonBrowserSession
                session = AmazonBrowserSession(
                    postal_code=getattr(cfg, "POSTAL_CODE", "10117"),
                    sleep=sleep,
                    headless=headless,
                    page_load_strategy=page_load_strategy,
                )
                own_session = True
            if sort == "main" and str(cfg.PRODUCT).lower() == "ref":
                logger.info("main warmup start url=https://www.amazon.de/")
                warmup_result = session.warm_up("https://www.amazon.de/")
                warmup_summary = {
                    "url": warmup_result.get("url"),
                    "status": warmup_result.get("status"),
                    "bytes": warmup_result.get("bytes"),
                    "error": warmup_result.get("error"),
                }
                logger.info(
                    "main warmup done status=%s bytes=%s error=%s",
                    warmup_summary["status"], warmup_summary["bytes"], warmup_summary["error"],
                )
            page_limit = min(max_pages, max(1, (target + 49) // 50)) if sort == "bsr" else max_pages
            for page in range(1, page_limit + 1):
                url = page_url(cfg, sort, page)
                logger.info("page=%d url=%s", page, url)
                resp = session.fetch(
                    url,
                    scroll_ratio=0.85 if sort == "bsr" else 1.0,
                    scroll_max_scrolls=10 if sort == "bsr" else 8,
                    post_load_sleep=_env_float("AMZN_BSR_POST_GET_WAIT", 8.0) if sort == "bsr" else max(sleep, 3.0),
                )
                if save_html:
                    save_text(ref / f"page_{page:02d}.html", resp["text"])
                start_rank = len(rows) + 1
                parsed = []
                if session.driver is not None:
                    if sort == "bsr":
                        expected_count = min(50, max(target - start_rank + 1, 1))
                        parsed = selector_api.extract_bsr_cards_siel(
                            session.driver,
                            selector_map,
                            start_rank=start_rank,
                            expected_count=expected_count,
                            logger=logger,
                        )
                    else:
                        parsed = selector_api.extract_cards(session.driver, selector_map, sort=sort, start_rank=start_rank)
                    if sort == "bsr":
                        if not parsed:
                            logger.info("page=%d records=0 -> refresh", page)
                            try:
                                session.driver.refresh()
                                time.sleep(3)
                                parsed = selector_api.extract_bsr_cards_siel(
                                    session.driver,
                                    selector_map,
                                    start_rank=start_rank,
                                    expected_count=expected_count,
                                    logger=logger,
                                )
                                logger.info("page=%d records=%d (after refresh primary-grid pass)", page, len(parsed))
                            except WebDriverException as exc:
                                logger.warning("page=%d refresh failed: %s", page, exc)
                        elif len(parsed) < expected_count:
                            logger.info(
                                "page=%d records=%d<%d -> refresh/retry primary-grid pass",
                                page, len(parsed), expected_count,
                            )
                            try:
                                session.driver.refresh()
                                time.sleep(3)
                                retry_parsed = selector_api.extract_bsr_cards_siel(
                                    session.driver,
                                    selector_map,
                                    start_rank=start_rank,
                                    expected_count=expected_count,
                                    logger=logger,
                                )
                                if len(retry_parsed) > len(parsed):
                                    parsed = retry_parsed
                                logger.info("page=%d records=%d (after refresh/retry primary-grid pass)", page, len(parsed))
                            except WebDriverException as exc:
                                logger.warning("page=%d refresh retry failed: %s", page, exc)
                retry_attempts: list[dict[str, Any]] = []
                if sort == "main" and page == 1 and not rows:
                    failure_reason = _main_page_failure_reason(resp, parsed)
                    if failure_reason:
                        first_diagnostic = ref / "page_01_attempt_1_error.html"
                        save_text(first_diagnostic, resp.get("text") or "")
                        retry_attempts.append({
                            "attempt": 1,
                            "mode": "initial",
                            "status": resp.get("status"),
                            "bytes": resp.get("bytes"),
                            "rows": len(parsed),
                            "error": resp.get("error"),
                            "reason": failure_reason,
                            "diagnostic_html": str(first_diagnostic),
                        })
                        logger.warning(
                            "page=1 main retry requested attempt=1/3 mode=initial reason=%s "
                            "status=%s records=%d bytes=%s",
                            failure_reason, resp.get("status"), len(parsed), resp.get("bytes"),
                        )

                        resp = session.refetch(
                            url,
                            wait_range=(5.0, 10.0),
                            scroll_ratio=1.0,
                            scroll_max_scrolls=8,
                            post_load_sleep=max(sleep, 3.0),
                        )
                        parsed = (
                            selector_api.extract_cards(
                                session.driver, selector_map, sort=sort, start_rank=start_rank,
                            )
                            if session.driver is not None else []
                        )
                        failure_reason = _main_page_failure_reason(resp, parsed)
                        retry_attempts.append({
                            "attempt": 2,
                            "mode": "same_session",
                            "status": resp.get("status"),
                            "bytes": resp.get("bytes"),
                            "rows": len(parsed),
                            "error": resp.get("error"),
                            "reason": failure_reason,
                        })

                        if failure_reason:
                            second_diagnostic = ref / "page_01_attempt_2_error.html"
                            save_text(second_diagnostic, resp.get("text") or "")
                            retry_attempts[-1]["diagnostic_html"] = str(second_diagnostic)
                            logger.warning(
                                "page=1 main retry requested attempt=2/3 mode=same_session reason=%s "
                                "status=%s records=%d bytes=%s",
                                failure_reason, resp.get("status"), len(parsed), resp.get("bytes"),
                            )
                            session.restart("main_listing_recovery")
                            restart_warmup = session.warm_up("https://www.amazon.de/")
                            resp = session.fetch(
                                url,
                                scroll_ratio=1.0,
                                scroll_max_scrolls=8,
                                post_load_sleep=max(sleep, 3.0),
                            )
                            parsed = (
                                selector_api.extract_cards(
                                    session.driver, selector_map, sort=sort, start_rank=start_rank,
                                )
                                if session.driver is not None else []
                            )
                            failure_reason = _main_page_failure_reason(resp, parsed)
                            retry_attempts.append({
                                "attempt": 3,
                                "mode": "new_session",
                                "status": resp.get("status"),
                                "bytes": resp.get("bytes"),
                                "rows": len(parsed),
                                "error": resp.get("error"),
                                "reason": failure_reason,
                                "warmup": {
                                    "url": restart_warmup.get("url"),
                                    "status": restart_warmup.get("status"),
                                    "bytes": restart_warmup.get("bytes"),
                                    "error": restart_warmup.get("error"),
                                },
                            })
                            if failure_reason:
                                final_diagnostic = ref / "page_01_attempt_3_error.html"
                                save_text(final_diagnostic, resp.get("text") or "")
                                retry_attempts[-1]["diagnostic_html"] = str(final_diagnostic)
                                logger.error(
                                    "page=1 main recovery exhausted reason=%s status=%s records=%d bytes=%s",
                                    failure_reason, resp.get("status"), len(parsed), resp.get("bytes"),
                                )
                                fatal_main_reason = failure_reason
                            else:
                                logger.info(
                                    "page=1 main recovered attempt=3/3 mode=new_session records=%d bytes=%s",
                                    len(parsed), resp.get("bytes"),
                                )
                        else:
                            logger.info(
                                "page=1 main recovered attempt=2/3 mode=same_session records=%d bytes=%s",
                                len(parsed), resp.get("bytes"),
                            )
                parsed = [
                    _apply_record_meta(cfg, _normalize_listing_row(r), sort=sort, page=page, source_url=resp["url"], batch_id=batch_id)
                    for r in parsed
                ]
                rows.extend(parsed)
                pages.append({
                    "page": page,
                    "url": resp["url"],
                    "status": resp["status"],
                    "bytes": resp["bytes"],
                    "parsed_rows": len(parsed),
                    "error": resp["error"],
                    "retry_attempts": retry_attempts,
                })
                logger.info("page=%d status=%s records=%d total=%d bytes=%s error=%s", page, resp["status"], len(parsed), len(rows), resp["bytes"], resp["error"])
                print(f"[listing/{cfg.PRODUCT}/{sort}] page={page} status={resp['status']} parsed={len(parsed)} total={len(rows)}", flush=True)
                if fatal_main_reason:
                    break
                if len(rows) >= target or (not parsed and sort != "bsr"):
                    break
                if inter_page_sleep > 0:
                    time.sleep(inter_page_sleep)
    finally:
        if own_session and session is not None:
            session.close()

    rows = rows[:target]
    for row in rows:
        siel_log.warn_price_logic(logger, row)
        siel_log.log_record_summary(logger, row)
        if emit:
            emit(row)
    path = out / f"amzn_listing_{sort}.csv"
    write_csv(path, rows)
    manifest = {
        "run_type": f"listing_{sort}",
        "product": cfg.PRODUCT,
        "target": target,
        "rows": len(rows),
        "output": str(path),
        "raw_dir": str(ref) if save_html else "",
        "raw_saved": save_html,
        "pages": pages,
        "success": fatal_main_reason is None,
        "failure_reason": fatal_main_reason,
        "warmup": warmup_summary,
        "selector_source": "db_xpath",
    }
    write_json(out / f"step01_listing_{sort}_manifest.json", manifest)
    logger.info("=== done: records=%d batch_id=%s ===", len(rows), batch_id)
    manifest["rows_data"] = rows
    if fatal_main_reason:
        raise MainListingUnavailableError(
            f"main listing unavailable after 3 attempts: {fatal_main_reason}"
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", choices=["TV"], default="TV")
    parser.add_argument("--sort", choices=["main", "bsr"], default="main")
    parser.add_argument("--target", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--input-html", default="")
    args = parser.parse_args()
    from TV import config
    run(config, sort=args.sort, target=args.target or None, max_pages=args.max_pages, input_html=args.input_html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
