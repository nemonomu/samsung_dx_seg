"""Step01/BSR: collect Amazon listing pages into per-source CSVs/JSONL."""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from typing import Any

from common import parsers, selectors as selector_api, siel_logging as siel_log
from common.config import BSR_TARGET, DEFAULT_SLEEP, DEFAULT_TIMEOUT, LISTING_TARGET
from common.http import add_query, save_text
from common.io_util import category_output_root, category_reference_root, ensure_dirs, write_csv, write_json


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
            rows = [_apply_record_meta(cfg, r, sort=sort, page=1, source_url=input_html, batch_id=batch_id) for r in rows]
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
            for page in range(1, max_pages + 1):
                url = page_url(cfg, sort, page)
                logger.info("page=%d url=%s", page, url)
                resp = session.fetch(
                    url,
                    scroll_ratio=0.85 if sort == "bsr" else 1.0,
                    scroll_max_scrolls=10 if sort == "bsr" else 8,
                    post_load_sleep=max(sleep, 3.0),
                )
                if save_html:
                    save_text(ref / f"page_{page:02d}.html", resp["text"])
                start_rank = len(rows) + 1
                parsed = []
                fallback_parsed = parsers.parse_bsr_html(resp["text"], start_rank=start_rank) if sort == "bsr" else parsers.parse_listing_html(resp["text"], page=page, sort=sort, start_rank=start_rank)
                if session.driver is not None:
                    parsed = selector_api.extract_cards(session.driver, selector_map, sort=sort, start_rank=start_rank)
                if parsed:
                    fallback_by_asin = {(r.get("asin") or r.get("item") or ""): r for r in fallback_parsed}
                    for row in parsed:
                        fallback = fallback_by_asin.get(row.get("asin") or row.get("item") or "") or {}
                        for key, value in fallback.items():
                            if row.get(key) in (None, "") and value not in (None, ""):
                                normalized = selector_api.normalize_field(key, value)
                                if normalized not in (None, ""):
                                    row[key] = normalized
                else:
                    parsed = fallback_parsed
                parsed = [
                    _apply_record_meta(cfg, r, sort=sort, page=page, source_url=resp["url"], batch_id=batch_id)
                    for r in parsed
                ]
                rows.extend(parsed)
                pages.append({"page": page, "url": resp["url"], "status": resp["status"], "bytes": resp["bytes"], "parsed_rows": len(parsed), "error": resp["error"]})
                logger.info("page=%d status=%s records=%d total=%d bytes=%s error=%s", page, resp["status"], len(parsed), len(rows), resp["bytes"], resp["error"])
                print(f"[listing/{cfg.PRODUCT}/{sort}] page={page} status={resp['status']} parsed={len(parsed)} total={len(rows)}", flush=True)
                if len(rows) >= target or not parsed:
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
        "selector_source": "db_xpath",
    }
    write_json(out / f"step01_listing_{sort}_manifest.json", manifest)
    logger.info("=== done: records=%d batch_id=%s ===", len(rows), batch_id)
    manifest["rows_data"] = rows
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
