"""Capture OTTO listing pages 1-4 as one continuous HAR plus DOM snapshots.

Run this on the RDP machine that can access OTTO with the intended geo/session.
The script does not use ZenRows. It drives a local Chrome/Chromium browser with
Playwright and stores capture artifacts under OTTO/references/har by default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


OTTO_ROOT = Path(__file__).resolve().parent
DEFAULT_START_URL = "https://www.otto.de/suche/fernseher/?sortiertnach=topseller"
DEFAULT_OUTPUT_PARENT = OTTO_ROOT / "references" / "har"
TARGET_AREA_SELECTOR = "#reptile-search-result section.reptile-tile-list"
PRODUCT_TILE_SELECTOR = f"{TARGET_AREA_SELECTOR} article[data-qa='reptile-product-tile']"

NEXT_SELECTORS = [
    "a[rel='next']",
    "a[aria-label*='Weiter']",
    "button[aria-label*='Weiter']",
    "a:has-text('Weiter')",
    "button:has-text('Weiter')",
    "a[data-qa*='next']",
    "button[data-qa*='next']",
    "a[data-testid*='next']",
    "button[data-testid*='next']",
]

CONSENT_SELECTORS = [
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "[id*='onetrust'] button:has-text('Akzeptieren')",
    "[class*='consent'] button:has-text('Akzeptieren')",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture OTTO Topseller listing pages 1-4 in one HAR with DOM snapshots."
    )
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--pages", type=int, default=4)
    parser.add_argument("--mode", choices=["click", "offset"], default="click")
    parser.add_argument("--page-param", default="o")
    parser.add_argument("--page-size", type=int, default=120)
    parser.add_argument("--no-fallback-offset", dest="fallback_offset", action="store_false")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--browser-channel", default="chrome", help="Use installed Chrome by default. Use '' for bundled Chromium.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--pause-first-page", action="store_true", help="Pause after page 1 opens so an operator can solve consent/blockers.")
    parser.add_argument("--pause-each-page", action="store_true", help="Pause before capturing every page.")
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1100)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--settle-ms", type=int, default=3000)
    parser.add_argument("--scroll-rounds", type=int, default=18)
    parser.add_argument("--wait-after-scroll-ms", type=int, default=1200)
    parser.add_argument("--save-xhr-body-max-bytes", type=int, default=2_000_000)
    parser.add_argument("--no-screenshot", action="store_true")
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def truncate_text(value: str | None, limit: int = 10000) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit] + f"...[truncated {len(value) - limit} chars]"


def build_offset_url(start_url: str, page_index: int, page_param: str, page_size: int) -> str:
    if page_index <= 1:
        return start_url
    parsed = urlparse(start_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[page_param] = [str((page_index - 1) * page_size)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print("[capture] Playwright is not installed in this Python environment.", file=sys.stderr)
        print("[capture] Install once on the RDP machine:", file=sys.stderr)
        print("  python -m pip install playwright", file=sys.stderr)
        print("  python -m playwright install chromium", file=sys.stderr)
        raise SystemExit(2) from exc
    return sync_playwright, PlaywrightTimeoutError


def launch_context(playwright: Any, args: argparse.Namespace, output_dir: Path, har_path: Path) -> Any:
    profile_dir = output_dir / "browser_profile"
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--lang=de-DE,de",
        "--no-first-run",
    ]

    def launch(channel: str | None, include_har_content: bool) -> Any:
        kwargs: dict[str, Any] = {
            "headless": args.headless,
            "viewport": {"width": args.viewport_width, "height": args.viewport_height},
            "locale": "de-DE",
            "timezone_id": "Europe/Berlin",
            "ignore_https_errors": True,
            "record_har_path": str(har_path),
            "record_har_mode": "full",
            "extra_http_headers": {
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            "args": browser_args,
        }
        if include_har_content:
            kwargs["record_har_content"] = "embed"
        if channel:
            kwargs["channel"] = channel
        return playwright.chromium.launch_persistent_context(str(profile_dir), **kwargs)

    channel = args.browser_channel.strip() or None
    try:
        return launch(channel, include_har_content=True)
    except TypeError:
        return launch(channel, include_har_content=False)
    except Exception:
        if not channel:
            raise
        print(f"[capture] Could not launch channel={channel!r}; retrying bundled Chromium.", file=sys.stderr)
        try:
            return launch(None, include_har_content=True)
        except TypeError:
            return launch(None, include_har_content=False)


def quick_metrics(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """(selectors) => {
            const target = document.querySelector(selectors.targetArea);
            const tiles = Array.from(document.querySelectorAll(selectors.productTile));
            const loadedTiles = tiles.filter((tile) => {
                const text = (tile.innerText || '').trim();
                return text.length > 0 && !tile.querySelector('.reptile-tile-placeholder');
            });
            const sponsoredTiles = tiles.filter((tile) => /Gesponsert/i.test(tile.innerText || ''));
            const hrefs = tiles.map((tile) => {
                const link = tile.querySelector('a[href*="/p/"], a[href]');
                return link ? link.href : null;
            }).filter(Boolean);
            return {
                url: window.location.href,
                title: document.title,
                target_area_present: Boolean(target),
                target_area_text_length: target ? (target.innerText || '').length : 0,
                product_tile_count: tiles.length,
                loaded_tile_count: loadedTiles.length,
                placeholder_tile_count: tiles.length - loadedTiles.length,
                sponsored_tile_count: sponsoredTiles.length,
                unique_href_count: new Set(hrefs).size,
                scroll_y: window.scrollY,
                inner_height: window.innerHeight,
                scroll_height: document.documentElement.scrollHeight,
            };
        }""",
        {"targetArea": TARGET_AREA_SELECTOR, "productTile": PRODUCT_TILE_SELECTOR},
    )


def dismiss_consent(page: Any) -> list[str]:
    clicked: list[str] = []
    for selector in CONSENT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.click(timeout=1500)
            clicked.append(selector)
            page.wait_for_timeout(800)
        except Exception:
            continue
    return clicked


def wait_for_page_ready(page: Any, timeout_ms: int, settle_ms: int, playwright_timeout: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"selector_seen": False, "networkidle_seen": False}
    try:
        page.wait_for_selector(TARGET_AREA_SELECTOR, timeout=timeout_ms)
        result["selector_seen"] = True
    except playwright_timeout:
        result["selector_error"] = f"{TARGET_AREA_SELECTOR} not seen within {timeout_ms}ms"
    try:
        page.wait_for_load_state("networkidle", timeout=settle_ms)
        result["networkidle_seen"] = True
    except playwright_timeout:
        result["networkidle_error"] = f"networkidle not reached within {settle_ms}ms"
    return result


def scroll_until_stable(page: Any, args: argparse.Namespace, playwright_timeout: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    stable_bottom_rounds = 0
    for step in range(1, args.scroll_rounds + 1):
        before = quick_metrics(page)
        page.evaluate(
            """(height) => {
                window.scrollBy({top: Math.max(400, Math.floor(height * 0.85)), behavior: 'instant'});
            }""",
            args.viewport_height,
        )
        page.wait_for_timeout(args.wait_after_scroll_ms)
        networkidle_seen = True
        try:
            page.wait_for_load_state("networkidle", timeout=min(args.settle_ms, 5000))
        except playwright_timeout:
            networkidle_seen = False
        after = quick_metrics(page)
        at_bottom = after["scroll_y"] + after["inner_height"] >= after["scroll_height"] - 8
        stable_bottom_rounds = stable_bottom_rounds + 1 if at_bottom else 0
        steps.append(
            {
                "step": step,
                "networkidle_seen": networkidle_seen,
                "at_bottom": at_bottom,
                "before": before,
                "after": after,
            }
        )
        if stable_bottom_rounds >= 2:
            break
    return steps


def extract_tiles(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """(selector) => {
            return Array.from(document.querySelectorAll(selector)).map((tile, index) => {
                const link = tile.querySelector('a[href*="/p/"], a[href]');
                const href = link ? link.href : null;
                let variationId = null;
                try {
                    variationId = href ? new URL(href).searchParams.get('variationId') : null;
                } catch (err) {}
                const text = (tile.innerText || '').replace(/\\s+/g, ' ').trim();
                return {
                    position: index + 1,
                    href,
                    variationId,
                    sponsored: /Gesponsert/i.test(text),
                    text: text.slice(0, 1000),
                };
            });
        }""",
        PRODUCT_TILE_SELECTOR,
    )


def capture_page_artifacts(
    context: Any,
    page: Any,
    page_index: int,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    page_prefix = f"page_{page_index:02d}"
    html_path = output_dir / f"{page_prefix}.html"
    domsnapshot_path = output_dir / f"{page_prefix}_domsnapshot.json"
    tiles_path = output_dir / f"{page_prefix}_tiles.json"
    metrics_path = output_dir / f"{page_prefix}_metrics.json"
    screenshot_path = output_dir / f"{page_prefix}.png"

    html = page.evaluate("() => document.documentElement.outerHTML")
    html_path.write_text(html, encoding="utf-8", errors="replace")

    domsnapshot_status: dict[str, Any] = {"saved": False}
    try:
        cdp = context.new_cdp_session(page)
        domsnapshot = cdp.send(
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": [],
                "includeDOMRects": True,
                "includePaintOrder": True,
            },
        )
        write_json(domsnapshot_path, domsnapshot)
        domsnapshot_status = {"saved": True, "file": safe_relative(domsnapshot_path, output_dir)}
    except Exception as exc:
        domsnapshot_status = {"saved": False, "error": repr(exc)}

    tiles = extract_tiles(page)
    write_json(tiles_path, tiles)

    metrics = quick_metrics(page)
    metrics.update(
        {
            "page_index": page_index,
            "captured_at": now_iso(),
            "html_file": safe_relative(html_path, output_dir),
            "html_bytes": html_path.stat().st_size,
            "domsnapshot": domsnapshot_status,
            "tiles_file": safe_relative(tiles_path, output_dir),
            "tile_rows": len(tiles),
        }
    )

    if not args.no_screenshot:
        page.screenshot(path=str(screenshot_path), full_page=True)
        metrics["screenshot_file"] = safe_relative(screenshot_path, output_dir)
        metrics["screenshot_bytes"] = screenshot_path.stat().st_size

    write_json(metrics_path, metrics)
    metrics["metrics_file"] = safe_relative(metrics_path, output_dir)
    return metrics


def click_next_page(page: Any, timeout_ms: int, playwright_timeout: Any) -> dict[str, Any]:
    previous_url = page.url
    attempts: list[dict[str, Any]] = []
    for selector in NEXT_SELECTORS:
        attempt: dict[str, Any] = {"selector": selector}
        try:
            locator = page.locator(selector).first
            count = locator.count()
            attempt["count"] = count
            if count == 0:
                attempts.append(attempt)
                continue
            attempt["href"] = locator.get_attribute("href", timeout=1000)
            locator.scroll_into_view_if_needed(timeout=3000)
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout_ms):
                    locator.click(timeout=5000)
                attempt["navigation_event"] = "document_navigation"
            except playwright_timeout:
                attempt["navigation_event"] = "timeout_or_client_side_navigation"
            page.wait_for_timeout(1500)
            return {
                "method": "click",
                "selector": selector,
                "previous_url": previous_url,
                "final_url_after_click": page.url,
                "attempts": attempts + [attempt],
            }
        except Exception as exc:
            attempt["error"] = repr(exc)
            attempts.append(attempt)
    return {"method": "click_failed", "previous_url": previous_url, "attempts": attempts}


def save_xhr_body(
    response: Any,
    output_dir: Path,
    xhr_body_dir: Path,
    page_index: int | None,
    sequence: int,
    max_bytes: int,
) -> dict[str, Any]:
    request = response.request
    content_type = (response.headers.get("content-type") or "").lower()
    allowed = ("json", "text", "javascript", "html", "xml", "graphql")
    if not any(token in content_type for token in allowed):
        return {"body_saved": False, "body_skip_reason": f"content_type={content_type or 'missing'}"}
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return {"body_saved": False, "body_skip_reason": f"content_length>{max_bytes}"}
        except ValueError:
            pass
    try:
        body = response.body()
    except Exception as exc:
        return {"body_saved": False, "body_error": repr(exc)}
    if len(body) > max_bytes:
        return {"body_saved": False, "body_bytes": len(body), "body_skip_reason": f"body>{max_bytes}"}
    suffix = ".json" if "json" in content_type or "graphql" in content_type else ".txt"
    digest = hashlib.sha1(request.url.encode("utf-8", errors="ignore")).hexdigest()[:12]
    body_path = xhr_body_dir / f"xhr_{sequence:04d}_p{page_index or 0}_{digest}{suffix}"
    body_path.write_bytes(body)
    return {
        "body_saved": True,
        "body_file": safe_relative(body_path, output_dir),
        "body_bytes": len(body),
    }


def run_capture(args: argparse.Namespace) -> int:
    sync_playwright, playwright_timeout = import_playwright()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_PARENT / f"listing_pages_1_4_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    xhr_body_dir = output_dir / "xhr_bodies"
    xhr_body_dir.mkdir(parents=True, exist_ok=True)
    har_path = output_dir / "otto_listing_pages_1_4.har"

    manifest: dict[str, Any] = {
        "run_type": "otto_listing_pages_1_4_har_dom_capture",
        "created_at": now_iso(),
        "start_url": args.start_url,
        "pages_requested": args.pages,
        "mode": args.mode,
        "page_param": args.page_param,
        "page_size": args.page_size,
        "output_dir": str(output_dir),
        "har_file": safe_relative(har_path, output_dir),
        "target_area_selector": TARGET_AREA_SELECTOR,
        "product_tile_selector": PRODUCT_TILE_SELECTOR,
        "browser_channel": args.browser_channel,
        "headless": args.headless,
        "completed": False,
        "pages": [],
    }
    network_events: list[dict[str, Any]] = []
    xhr_fetch_events: list[dict[str, Any]] = []
    console_events: list[dict[str, Any]] = []
    state: dict[str, Any] = {"page_index": None}
    body_sequence = {"value": 0}

    def on_request(request: Any) -> None:
        rec = {
            "ts": time.time(),
            "event": "request",
            "page_index": state.get("page_index"),
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "post_data": truncate_text(request.post_data, 10000),
        }
        network_events.append(rec)

    def on_response(response: Any) -> None:
        request = response.request
        rec = {
            "ts": time.time(),
            "event": "response",
            "page_index": state.get("page_index"),
            "method": request.method,
            "url": response.url,
            "resource_type": request.resource_type,
            "status": response.status,
            "content_type": response.headers.get("content-type"),
            "from_service_worker": response.from_service_worker,
            "request_post_data": truncate_text(request.post_data, 10000),
        }
        network_events.append(rec)
        if request.resource_type in {"xhr", "fetch"}:
            body_sequence["value"] += 1
            rec.update(
                save_xhr_body(
                    response=response,
                    output_dir=output_dir,
                    xhr_body_dir=xhr_body_dir,
                    page_index=state.get("page_index"),
                    sequence=body_sequence["value"],
                    max_bytes=args.save_xhr_body_max_bytes,
                )
            )
            xhr_fetch_events.append(rec)

    def on_console(message: Any) -> None:
        console_events.append(
            {
                "ts": time.time(),
                "page_index": state.get("page_index"),
                "type": message.type,
                "text": truncate_text(message.text, 2000),
            }
        )

    context = None
    try:
        with sync_playwright() as playwright:
            context = launch_context(playwright, args, output_dir, har_path)
            context.on("request", on_request)
            context.on("response", on_response)
            page = context.new_page()
            page.on("console", on_console)

            for page_index in range(1, args.pages + 1):
                state["page_index"] = page_index
                page_manifest: dict[str, Any] = {
                    "page_index": page_index,
                    "started_at": now_iso(),
                }
                print(f"[capture] page {page_index}/{args.pages}")

                if page_index == 1:
                    requested_url = args.start_url
                    page_manifest["navigation"] = {"method": "start_url", "requested_url": requested_url}
                    page.goto(requested_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                elif args.mode == "offset":
                    requested_url = build_offset_url(args.start_url, page_index, args.page_param, args.page_size)
                    page_manifest["navigation"] = {"method": "offset", "requested_url": requested_url}
                    page.goto(requested_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                else:
                    click_result = click_next_page(page, args.timeout_ms, playwright_timeout)
                    if click_result["method"] == "click_failed" and args.fallback_offset:
                        requested_url = build_offset_url(args.start_url, page_index, args.page_param, args.page_size)
                        click_result["fallback_requested_url"] = requested_url
                        page.goto(requested_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
                        click_result["fallback_final_url"] = page.url
                    page_manifest["navigation"] = click_result

                consent_clicks = dismiss_consent(page)
                ready = wait_for_page_ready(page, args.timeout_ms, args.settle_ms, playwright_timeout)

                if (page_index == 1 and args.pause_first_page) or args.pause_each_page:
                    print(
                        f"[capture] page {page_index} is open. Resolve consent/blockers in the browser, then press Enter here."
                    )
                    input()
                    consent_clicks.extend(dismiss_consent(page))
                    ready = wait_for_page_ready(page, args.timeout_ms, args.settle_ms, playwright_timeout)

                scroll_steps = scroll_until_stable(page, args, playwright_timeout)
                try:
                    page.wait_for_load_state("networkidle", timeout=args.settle_ms)
                except playwright_timeout:
                    pass

                metrics = capture_page_artifacts(context, page, page_index, output_dir, args)
                page_manifest.update(
                    {
                        "completed_at": now_iso(),
                        "final_url": page.url,
                        "consent_clicks": consent_clicks,
                        "ready": ready,
                        "scroll_steps": scroll_steps,
                        "metrics": metrics,
                    }
                )
                manifest["pages"].append(page_manifest)
                print(
                    "[capture] page {idx} final_url={url} tiles={tiles} sponsored={sponsored} html={html}".format(
                        idx=page_index,
                        url=page.url,
                        tiles=metrics.get("product_tile_count"),
                        sponsored=metrics.get("sponsored_tile_count"),
                        html=metrics.get("html_file"),
                    )
                )

            manifest["completed"] = True
            return_code = 0
    except KeyboardInterrupt:
        manifest["completed"] = False
        manifest["error"] = "KeyboardInterrupt"
        return_code = 130
    except Exception as exc:
        manifest["completed"] = False
        manifest["error"] = repr(exc)
        return_code = 1
        print(f"[capture] ERROR: {exc!r}", file=sys.stderr)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                manifest["context_close_error"] = repr(exc)
        write_jsonl(output_dir / "network_events.jsonl", network_events)
        write_jsonl(output_dir / "xhr_fetch_events.jsonl", xhr_fetch_events)
        write_jsonl(output_dir / "console_events.jsonl", console_events)
        manifest["finished_at"] = now_iso()
        manifest["network_event_count"] = len(network_events)
        manifest["xhr_fetch_event_count"] = len(xhr_fetch_events)
        manifest["console_event_count"] = len(console_events)
        manifest["har_exists"] = har_path.exists()
        manifest["har_bytes"] = har_path.stat().st_size if har_path.exists() else 0
        manifest["network_events_file"] = "network_events.jsonl"
        manifest["xhr_fetch_events_file"] = "xhr_fetch_events.jsonl"
        manifest["console_events_file"] = "console_events.jsonl"
        write_json(output_dir / "manifest.json", manifest)
        print(f"[capture] output_dir={output_dir}")
        print(f"[capture] har={har_path} exists={har_path.exists()}")
        print(f"[capture] manifest={output_dir / 'manifest.json'}")
    return return_code


def main() -> int:
    return run_capture(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
