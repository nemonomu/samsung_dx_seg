"""Step07: scrape OTTO detail page structure through ZenRows Scraping Browser.

This is a diagnostic script for PDP-only access. It does not use ZenRows for
listing/review. It records HTML, screenshot, HAR, response summaries, and a DOM
structure summary under OTTO/references/har by default.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from step00_config import DETAIL_SAMPLE_URL, HAR_ROOT, TOPSELLER_URL
from step00_zenrows import build_scraping_browser_url, redacted_scraping_browser_options

DETAIL_SIGNALS = (
    "Modellbezeichnung",
    "Bildschirmdiagonale in Zoll",
    "Leistungsaufnahme im Ein-Zustand",
    "application/ld+json",
)
CONSENT_SELECTORS = (
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "button:has-text('OK')",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape one OTTO PDP through ZenRows Scraping Browser and summarize page structure.")
    parser.add_argument("--url", default=DETAIL_SAMPLE_URL, help="OTTO PDP URL to scan.")
    parser.add_argument("--label", default="sample", help="Short label used in output folder name.")
    parser.add_argument("--proxy-country", default="de")
    parser.add_argument("--warmup", choices=["none", "home", "listing"], default="listing")
    parser.add_argument("--reloads", type=int, default=4, help="Number of PDP attempts including reloads after the first goto.")
    parser.add_argument("--wait-ms", type=int, default=15000, help="Wait after each PDP navigation/reload.")
    parser.add_argument("--warmup-wait-ms", type=int, default=7000)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-screenshot", action="store_true")
    return parser.parse_args()


def safe_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:80] or "detail"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def text_clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def truncate(value: str | None, limit: int = 1200) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit] + f"...[truncated {len(value) - limit} chars]"


def html_signals(html: str) -> dict[str, Any]:
    encoded = html.encode("utf-8", errors="replace")
    lower = html.lower()
    return {
        "bytes": len(encoded),
        "sha1": hashlib.sha1(encoded).hexdigest(),
        "kpsdk": lower.count("kpsdk"),
        "captcha": lower.count("captcha"),
        "modellbezeichnung": html.count("Modellbezeichnung"),
        "screen_label": html.count("Bildschirmdiagonale in Zoll"),
        "electricity_label": html.count("Leistungsaufnahme im Ein-Zustand"),
        "ldjson": html.count("application/ld+json"),
        "detail_present": any(token in html for token in DETAIL_SIGNALS),
    }


async def click_cookie_buttons(page: Any) -> list[str]:
    clicked: list[str] = []
    for selector in CONSENT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.click(timeout=3000)
                clicked.append(selector)
                await page.wait_for_timeout(1000)
        except Exception:
            continue
    return clicked


async def safe_response_body(response: Any, limit: int = 1200) -> dict[str, Any]:
    try:
        body = await response.text()
    except Exception as exc:
        return {"body_error": type(exc).__name__}
    return {"body_length": len(body), "body_sample": truncate(body, limit)}


async def extract_structure(page: Any) -> dict[str, Any]:
    return await page.evaluate(
        r"""() => {
            const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const bodyText = clean(document.body ? document.body.innerText : '');
            const headings = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]')).slice(0, 100).map((el) => ({
                tag: el.tagName.toLowerCase(),
                text: clean(el.innerText || el.textContent),
                id: el.id || null,
                classes: String(el.className || '').slice(0, 180) || null,
                dataQa: el.getAttribute('data-qa'),
                dataTestId: el.getAttribute('data-testid')
            })).filter((item) => item.text);
            const sections = Array.from(document.querySelectorAll('main,section,article,[data-qa],[data-testid]')).slice(0, 260).map((el) => ({
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                classes: String(el.className || '').slice(0, 180) || null,
                dataQa: el.getAttribute('data-qa'),
                dataTestId: el.getAttribute('data-testid'),
                ariaLabel: el.getAttribute('aria-label'),
                textSample: clean(el.innerText || el.textContent).slice(0, 300)
            })).filter((item) => item.textSample || item.dataQa || item.dataTestId || item.id);
            const detailLabelTexts = Array.from(document.querySelectorAll('dt,th,td,span,div,p')).map((el) => clean(el.innerText || el.textContent)).filter(Boolean).filter((text) => /Modellbezeichnung|Bildschirmdiagonale|Leistungsaufnahme|Energie|Details|Technische/i.test(text)).slice(0, 160);
            const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 160).map((a) => ({ text: clean(a.innerText || a.textContent).slice(0, 140), href: a.href }));
            const scripts = Array.from(document.querySelectorAll('script')).map((script) => ({
                type: script.type || null,
                id: script.id || null,
                length: (script.textContent || '').length,
                sample: clean((script.textContent || '').slice(0, 260))
            })).slice(0, 100);
            const jsonLd = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map((script) => script.textContent || '').slice(0, 30);
            return { url: location.href, title: document.title, bodyTextLength: bodyText.length, bodyTextSample: bodyText.slice(0, 1400), headings, sections, detailLabelTexts, links, scripts, jsonLd };
        }"""
    )


async def wait_for_settle(page: Any, wait_ms: int) -> None:
    await page.wait_for_timeout(wait_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass


async def main_async() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else HAR_ROOT / f"detail_zenrows_structure_{timestamp}_{safe_label(args.label)}"
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / "detail_final.html"
    png_path = output_dir / "detail_final.png"
    har_path = output_dir / "detail_zenrows_structure.har"
    responses_path = output_dir / "responses.json"
    structure_path = output_dir / "page_structure.json"
    summary_path = output_dir / "summary.json"

    response_entries: list[dict[str, Any]] = []
    response_tasks: set[asyncio.Task] = set()
    checkpoints: list[dict[str, Any]] = []
    clicked_buttons: list[str] = []
    context_error = None
    final_html = ""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(build_scraping_browser_url(proxy_country=args.proxy_country))
        try:
            try:
                context = await browser.new_context(
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    viewport={"width": 1440, "height": 1400},
                    ignore_https_errors=True,
                    record_har_path=str(har_path),
                    record_har_mode="full",
                    record_har_content="embed",
                    extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "Referer": "https://www.otto.de/"},
                )
            except TypeError:
                context = await browser.new_context(
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    viewport={"width": 1440, "height": 1400},
                    ignore_https_errors=True,
                    record_har_path=str(har_path),
                    record_har_mode="full",
                    extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7", "Referer": "https://www.otto.de/"},
                )
            except Exception as exc:
                context_error = type(exc).__name__ + ": " + str(exc)
                context = await browser.new_context(locale="de-DE", timezone_id="Europe/Berlin", viewport={"width": 1440, "height": 1400}, ignore_https_errors=True)

            page = await context.new_page()

            async def capture_response(response: Any) -> None:
                request = response.request
                headers = await response.all_headers()
                content_type = headers.get("content-type") or headers.get("Content-Type") or ""
                entry: dict[str, Any] = {
                    "url": response.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "status": response.status,
                    "content_type": content_type,
                }
                if request.resource_type in {"document", "xhr", "fetch"} or "json" in content_type or "text" in content_type:
                    entry.update(await safe_response_body(response))
                response_entries.append(entry)

            def schedule_response(response: Any) -> None:
                task = asyncio.create_task(capture_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", schedule_response)

            warmups: list[tuple[str, str]] = []
            if args.warmup in {"home", "listing"}:
                warmups.append(("home", "https://www.otto.de/"))
            if args.warmup == "listing":
                warmups.append(("listing", TOPSELLER_URL))

            for label, url in warmups:
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                    clicked_buttons.extend(await click_cookie_buttons(page))
                    await wait_for_settle(page, args.warmup_wait_ms)
                    html = await page.content()
                    checkpoints.append({"label": label, "status": response.status if response else None, "url": page.url, "title": await page.title(), "signals": html_signals(html)})
                except Exception as exc:
                    checkpoints.append({"label": label, "url": page.url, "error": type(exc).__name__ + ": " + str(exc)})

            attempts = max(args.reloads, 1)
            for idx in range(1, attempts + 1):
                try:
                    response = await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms) if idx == 1 else await page.reload(wait_until="domcontentloaded", timeout=args.timeout_ms)
                    await wait_for_settle(page, args.wait_ms)
                    final_html = await page.content()
                    signals = html_signals(final_html)
                    checkpoints.append({"label": f"detail_attempt_{idx}", "status": response.status if response else None, "url": page.url, "title": await page.title(), "signals": signals})
                    if signals["detail_present"]:
                        break
                except Exception as exc:
                    checkpoints.append({"label": f"detail_attempt_{idx}", "url": page.url, "error": type(exc).__name__ + ": " + str(exc)})

            if response_tasks:
                await asyncio.wait(response_tasks, timeout=10)
            final_html = final_html or await page.content()
            html_path.write_text(final_html, encoding="utf-8")
            if not args.no_screenshot:
                try:
                    await page.screenshot(path=str(png_path), full_page=True)
                except Exception:
                    pass
            structure = await extract_structure(page)
            write_json(structure_path, structure)
            await context.close()
        finally:
            await browser.close()

    write_json(responses_path, response_entries)
    summary = {
        "run_type": "step07_scrape_detail_zenrows_structure",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_url": args.url,
        "zenrows_options": redacted_scraping_browser_options(proxy_country=args.proxy_country),
        "warmup": args.warmup,
        "reloads": args.reloads,
        "context_error": context_error,
        "clicked_buttons": clicked_buttons,
        "checkpoints": checkpoints,
        "final_signals": html_signals(final_html),
        "response_count": len(response_entries),
        "response_counts": {
            "document": sum(1 for item in response_entries if item.get("resource_type") == "document"),
            "xhr": sum(1 for item in response_entries if item.get("resource_type") == "xhr"),
            "fetch": sum(1 for item in response_entries if item.get("resource_type") == "fetch"),
            "status_200": sum(1 for item in response_entries if item.get("status") == 200),
            "status_429": sum(1 for item in response_entries if item.get("status") == 429),
        },
        "interesting_responses": [item for item in response_entries if item.get("resource_type") in {"document", "xhr", "fetch"}][:120],
        "outputs": {
            "html": str(html_path),
            "screenshot": str(png_path) if png_path.exists() else None,
            "har": str(har_path) if har_path.exists() else None,
            "responses": str(responses_path),
            "structure": str(structure_path),
        },
    }
    write_json(summary_path, summary)
    console = {"summary": str(summary_path), "final_signals": summary["final_signals"], "response_counts": summary["response_counts"]}
    print(json.dumps(console, ensure_ascii=True, indent=2))
    return 0 if summary["final_signals"].get("detail_present") else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
