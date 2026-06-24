"""Step07: fetch the OTTO sample detail page through ZenRows Scraping Browser."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from step00_config import DETAIL_SAMPLE_URL, HAR_ROOT, REFERENCES_ROOT, write_json
from step00_zenrows import build_scraping_browser_url, redacted_scraping_browser_options

SAMPLE_NAME = "detail_philips_zenrows_browser"
SAMPLE_HAR_DIR = HAR_ROOT / "sample_philips"
SAMPLE_XHR_DIR = REFERENCES_ROOT / "xhr" / "sample_philips"
HTML_OUTPUT = SAMPLE_HAR_DIR / f"{SAMPLE_NAME}.html"
PNG_OUTPUT = SAMPLE_HAR_DIR / f"{SAMPLE_NAME}.png"
HAR_OUTPUT = SAMPLE_HAR_DIR / f"{SAMPLE_NAME}.har"
SUMMARY_OUTPUT = SAMPLE_HAR_DIR / f"{SAMPLE_NAME}_summary.json"
XHR_OUTPUT = SAMPLE_XHR_DIR / f"{SAMPLE_NAME}_endpoint_summary.json"
CANONICAL_DETAIL_OUTPUT = HAR_ROOT / "detail_sample.html"


def text_clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def contains_product_detail(html: str) -> bool:
    indicators = (
        "Modellbezeichnung",
        "Bildschirmdiagonale in Zoll",
        "Leistungsaufnahme im Ein-Zustand",
        "application/ld+json",
    )
    return any(indicator in html for indicator in indicators)


async def click_cookie_buttons(page) -> list[str]:
    clicked: list[str] = []
    selectors = (
        "button:has-text('Alle akzeptieren')",
        "button:has-text('Akzeptieren')",
        "button:has-text('OK')",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.click(timeout=2500)
                clicked.append(selector)
                await page.wait_for_timeout(1000)
        except Exception:
            continue
    return clicked


async def wait_for_detail_signals(page) -> dict[str, Any]:
    waits: dict[str, Any] = {}
    for selector in (".dv_characteristicsTable", "text=Modellbezeichnung", "body"):
        try:
            await page.wait_for_selector(selector, timeout=12000)
            waits[selector] = "ok"
        except PlaywrightTimeoutError:
            waits[selector] = "timeout"
        except Exception as exc:
            waits[selector] = type(exc).__name__
    return waits


async def capture_response(response, entries: list[dict[str, Any]]) -> None:
    request = response.request
    headers = await response.all_headers()
    content_type = headers.get("content-type") or headers.get("Content-Type")
    entry: dict[str, Any] = {
        "url": response.url,
        "method": request.method,
        "resource_type": request.resource_type,
        "status": response.status,
        "content_type": content_type,
    }
    is_interesting = request.resource_type in {"xhr", "fetch", "document"} or "json" in (content_type or "")
    if is_interesting:
        try:
            body = await response.text()
            entry["body_length"] = len(body)
            entry["body_sample"] = body[:1000]
        except Exception as exc:
            entry["body_error"] = type(exc).__name__
    entries.append(entry)


async def main_async() -> int:
    SAMPLE_HAR_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLE_XHR_DIR.mkdir(parents=True, exist_ok=True)

    response_entries: list[dict[str, Any]] = []
    response_tasks: set[asyncio.Task] = set()
    context_mode = "record_har"
    context_error = None
    nav_status = None
    final_url = None
    wait_state = None
    clicked_buttons: list[str] = []

    connection_url = build_scraping_browser_url(proxy_country="de")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(connection_url)
        try:
            try:
                context = await browser.new_context(
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    viewport={"width": 1440, "height": 1400},
                    record_har_path=str(HAR_OUTPUT),
                )
            except Exception as exc:
                context_error = type(exc).__name__
                context_mode = "no_har"
                context = await browser.new_context(
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    viewport={"width": 1440, "height": 1400},
                )
            page = await context.new_page()

            def schedule_response(response) -> None:
                task = asyncio.create_task(capture_response(response, response_entries))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", schedule_response)
            response = await page.goto(DETAIL_SAMPLE_URL, wait_until="domcontentloaded", timeout=90000)
            nav_status = response.status if response else None
            final_url = page.url
            clicked_buttons = await click_cookie_buttons(page)
            waits = await wait_for_detail_signals(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
                wait_state = "networkidle"
            except PlaywrightTimeoutError:
                wait_state = "networkidle_timeout"
            await page.wait_for_timeout(5000)
            if response_tasks:
                await asyncio.wait(response_tasks, timeout=8)
            html = await page.content()
            title = await page.title()
            try:
                body_text = await page.locator("body").inner_text(timeout=5000)
            except Exception:
                body_text = ""
            await page.screenshot(path=str(PNG_OUTPUT), full_page=True)
            HTML_OUTPUT.write_text(html, encoding="utf-8")
            if nav_status == 200 and contains_product_detail(html):
                CANONICAL_DETAIL_OUTPUT.write_text(html, encoding="utf-8")
                canonical_detail_written = True
            await context.close()
        finally:
            await browser.close()

    filtered_entries = [
        entry for entry in response_entries
        if entry.get("resource_type") in {"document", "xhr", "fetch"}
        or "json" in (entry.get("content_type") or "")
    ]
    write_json(XHR_OUTPUT, filtered_entries)

    summary = {
        "run_type": "zenrows_scraping_browser_detail_sample",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_url": DETAIL_SAMPLE_URL,
        "zenrows_options": redacted_scraping_browser_options(proxy_country="de"),
        "context_mode": context_mode,
        "context_error": context_error,
        "navigation_status": nav_status,
        "final_url": final_url,
        "title": text_clean(title),
        "wait_state": wait_state,
        "detail_waits": waits,
        "clicked_cookie_buttons": clicked_buttons,
        "html_bytes": len(html.encode("utf-8", errors="replace")),
        "body_text_sample": text_clean(body_text[:1000]),
        "contains_product_detail": contains_product_detail(html),
        "counts": {
            "modellbezeichnung": html.count("Modellbezeichnung"),
            "dv_characteristicsTable": html.count("dv_characteristicsTable"),
            "application_ld_json": html.count("application/ld+json"),
            "kp_sdk": html.lower().count("kpsdk"),
            "review": html.lower().count("review"),
            "bewertung": html.lower().count("bewertung"),
        },
        "outputs": {
            "html": str(HTML_OUTPUT),
            "screenshot": str(PNG_OUTPUT),
            "har": str(HAR_OUTPUT) if HAR_OUTPUT.exists() else None,
            "xhr_summary": str(XHR_OUTPUT),
            "canonical_detail_html": str(CANONICAL_DETAIL_OUTPUT) if canonical_detail_written else None,
        },
        "endpoint_entries": len(filtered_entries),
    }
    write_json(SUMMARY_OUTPUT, summary)
    print(
        "[step07] status={status} detail={detail} html_bytes={bytes} endpoints={endpoints} har={har}".format(
            status=nav_status,
            detail=summary["contains_product_detail"],
            bytes=summary["html_bytes"],
            endpoints=len(filtered_entries),
            har="yes" if HAR_OUTPUT.exists() else "no",
        )
    )
    return 0 if summary["contains_product_detail"] else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())

