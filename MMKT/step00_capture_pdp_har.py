"""Capture the client-side GraphQL/XHR calls a MediaMarkt PDP makes.

Three SEG fields are NOT in the PDP's server HTML (see memory mmkt-bot-defense):
top-20 reviews (reviewPage 2+), summarized_review_content (KI summary), and
retailer_sku_name_similar (Alternativen im Vergleich). All three are lazy
client calls to https://www.mediamarkt.de/api/v1/graphql. This script drives a
real browser through the ZenRows scraping browser (CDP, DE proxy — the local IP
is never exposed), triggers those lazy calls (scroll + click review/KI/compare),
and records each matching request's operationName / persisted-query sha256 /
headers / variables / response so the calls can later be replayed via ZenRows.

  python MMKT/step00_capture_pdp_har.py
  python MMKT/step00_capture_pdp_har.py --url <pdp-url> --headless

Output: references/har/pdp_<id>_<stamp>/ with capture.json (+ pdp.html).
Nothing secret is written: request headers are filtered to a safe allowlist and
the ZenRows apikey lives only in the CDP URL, which is never persisted.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from common.config import REFERENCES_ROOT
from common.zenrows import DEFAULT_PROXY_COUNTRY, build_scraping_browser_url

MMKT_HOME = "https://www.mediamarkt.de/"
# Default probe target: SAMSUNG GQ43Q7F (id 2988688) — has reviews + Alternativen.
DEFAULT_PDP = (
    "https://www.mediamarkt.de/de/product/"
    "_samsung-gq43q7f-qled-tv-vision-ai-smart-tv-43-zoll-108-cm-uhd-4k-smart-tv-tizen-2988688.html"
)

# URL substrings that mark a call we care about.
CAPTURE_URL_HINTS = ("graphql", "bazaarvoice", "/reviews", "recommend", "/api/")
# GraphQL operations that carry the 3 SEG fields missing from PDP SSR; their full
# responses are saved (not truncated) and their persisted-query hashes registered.
TARGET_OPERATIONS = {
    "GetReviewsSummary",                  # summarized_review_content (KI summary)
    "GetProductReviews",                  # detailed_review_content (top-20, reviewPage)
    "GetComparisonTableRecommendations",  # retailer_sku_name_similar (Alternativen)
    "GetOneRecommendationsGroupedByType",  # similar/accessories recommendations
}
# Request headers worth keeping for replay (everything else, incl. cookies, dropped).
HEADER_ALLOWLIST = {
    "content-type", "accept", "apollographql-client-name", "apollographql-client-version",
    "x-api-key", "x-operation-name", "x-flow", "x-pwa-mms-build", "x-mms-build",
    "graphql-client", "x-cacheable", "origin", "referer",
}
CONSENT_SELECTORS = (
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "#pwa-consent-layer-accept-all",
)
# Buttons/sections that trigger the lazy calls.
REVIEW_TRIGGERS = (
    "button:has-text('weitere Bewertungen')",
    "button:has-text('Weitere Bewertungen')",
    "button:has-text('Mehr Bewertungen')",
    "a:has-text('Bewertungen')",
)
KI_TRIGGERS = (
    "button:has-text('Zusammenfassung der KI')",
    "button:has-text('KI-Zusammenfassung')",
    "button:has-text('Zusammenfassung')",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture MediaMarkt PDP lazy GraphQL calls.")
    p.add_argument("--url", default=DEFAULT_PDP)
    p.add_argument("--proxy-country", default=DEFAULT_PROXY_COUNTRY)
    p.add_argument("--headless", action="store_true", default=False)
    p.add_argument("--nav-timeout", type=int, default=90000)
    p.add_argument("--settle", type=int, default=6000)
    return p.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sku_from_url(url: str) -> str:
    m = re.search(r"-(\d+)\.html", url)
    return m.group(1) if m else "unknown"


def filter_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in HEADER_ALLOWLIST}


def op_from_url(url: str) -> tuple[str | None, str | None, dict | None]:
    """Extract (operationName, persisted sha256, variables) from a GraphQL GET URL."""
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(url).query)
    op = (qs.get("operationName") or [None])[0]
    try:
        variables = json.loads((qs.get("variables") or ["null"])[0])
    except Exception:
        variables = None
    try:
        ext = json.loads((qs.get("extensions") or ["{}"])[0])
        sha = (ext.get("persistedQuery") or {}).get("sha256Hash")
    except Exception:
        sha = None
    return op, sha, variables


def summarize_post(post_data: str | None) -> dict[str, Any]:
    """Pull operationName / persisted sha256 / variable keys from a GraphQL body."""
    if not post_data:
        return {}
    try:
        data = json.loads(post_data)
    except Exception:
        return {"raw_preview": post_data[:300]}
    items = data if isinstance(data, list) else [data]
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ext = (it.get("extensions") or {}).get("persistedQuery") or {}
        out.append(
            {
                "operationName": it.get("operationName"),
                "persisted_sha256": ext.get("sha256Hash"),
                "has_query_text": bool(it.get("query")),
                "variables": it.get("variables"),
            }
        )
    return {"operations": out}


def main() -> int:
    args = parse_args()
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout

    sku_id = sku_from_url(args.url)
    out_dir = REFERENCES_ROOT / "har" / f"pdp_{sku_id}_{now_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    captured: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}  # request id-ish keyed by url+method+ts

    def want(url: str) -> bool:
        return any(h in url for h in CAPTURE_URL_HINTS)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(
            build_scraping_browser_url(proxy_country=args.proxy_country)
        )
        context = browser.new_context(
            locale="de-DE", timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 1600},
        )
        page = context.new_page()

        def on_request(req):
            if not want(req.url):
                return
            try:
                post = req.post_data
            except Exception:
                post = None
            pending[req.url + "#" + str(id(req))] = {
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type,
                "request_headers": filter_headers(req.headers),
                "post_summary": summarize_post(post),
            }

        def on_response(resp):
            req = resp.request
            if not want(req.url):
                return
            key = req.url + "#" + str(id(req))
            entry = pending.pop(key, None) or {
                "url": req.url, "method": req.method,
                "request_headers": filter_headers(req.headers),
                "post_summary": summarize_post(getattr(req, "post_data", None)),
            }
            entry["status"] = resp.status
            op, sha, variables = op_from_url(req.url)
            entry["operationName"] = op
            entry["persisted_sha256"] = sha
            if variables is not None:
                entry["variables"] = variables
            is_target = op in TARGET_OPERATIONS
            body_preview = None
            body_json_keys = None
            full = None
            try:
                if "json" in (resp.headers.get("content-type") or ""):
                    data = resp.json()
                    body_json_keys = list(data.keys()) if isinstance(data, dict) else None
                    full = data
                    body_preview = json.dumps(data, ensure_ascii=False)[:4000]
            except Exception:
                pass
            entry["response_status"] = resp.status
            entry["response_json_keys"] = body_json_keys
            entry["response_preview"] = body_preview
            if is_target and full is not None:
                # Save the full response for the 3 SEG-field operations.
                (out_dir / f"resp_{op}.json").write_text(
                    json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                entry["full_response_file"] = f"resp_{op}.json"
            captured.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)

        steps: list[dict[str, Any]] = []

        def log_step(name: str, **kw):
            steps.append({"step": name, **kw})
            print(f"[har] {name} {kw}")

        # 1) warm up on home + consent
        try:
            r = page.goto(MMKT_HOME, wait_until="domcontentloaded", timeout=args.nav_timeout)
            log_step("home", status=r.status if r else None)
        except Exception as exc:
            log_step("home_error", error=type(exc).__name__ + ": " + str(exc))
        for sel in CONSENT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(800)
                    log_step("consent", selector=sel)
                    break
            except Exception:
                continue

        # 2) navigate to the PDP (Referer = home, like a real click)
        try:
            r = page.goto(args.url, wait_until="domcontentloaded", timeout=args.nav_timeout, referer=MMKT_HOME)
            log_step("pdp", status=r.status if r else None)
        except Exception as exc:
            log_step("pdp_error", error=type(exc).__name__ + ": " + str(exc))
        page.wait_for_timeout(args.settle)

        # 3) scroll to bottom in steps to trigger lazy reco / Alternativen
        for frac in (0.3, 0.5, 0.7, 0.9, 1.0):
            try:
                page.evaluate("f => window.scrollTo(0, document.body.scrollHeight*f)", frac)
            except Exception:
                pass
            page.wait_for_timeout(1500)
        log_step("scrolled")

        # 4) try to open more reviews + KI summary
        for label, triggers in (("review", REVIEW_TRIGGERS), ("ki", KI_TRIGGERS)):
            for sel in triggers:
                try:
                    loc = page.locator(sel).first
                    if loc.count():
                        loc.scroll_into_view_if_needed(timeout=3000)
                        loc.click(timeout=4000)
                        page.wait_for_timeout(args.settle)
                        log_step(f"clicked_{label}", selector=sel)
                        break
                except Exception:
                    continue

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

        html = page.content()
        (out_dir / "pdp.html").write_text(html, encoding="utf-8")

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    # persisted-query registry: operationName -> {sha256, sample variables}.
    registry: dict[str, dict[str, Any]] = {}
    for c in captured:
        op = c.get("operationName")
        if op and c.get("persisted_sha256") and op not in registry:
            registry[op] = {
                "sha256": c["persisted_sha256"],
                "method": c.get("method"),
                "sample_variables": c.get("variables"),
                "status": c.get("response_status"),
                "is_target": op in TARGET_OPERATIONS,
            }

    manifest = {
        "run_type": "mmkt_pdp_har_capture",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": args.url,
        "sku_id": sku_id,
        "proxy_country": args.proxy_country,
        "graphql_endpoint": "https://www.mediamarkt.de/api/v1/graphql",
        "required_header": {"apollographql-client-name": "pwa-client-pqm"},
        "steps": steps,
        "captured_count": len(captured),
        "persisted_query_registry": registry,
        "captured": captured,
    }
    (out_dir / "capture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # concise console summary of the GraphQL operations seen
    print(f"\n[har] captured {len(captured)} matching calls -> {out_dir}")
    print(f"[har] persisted-query registry ({len(registry)} ops):")
    for op, info in sorted(registry.items(), key=lambda kv: (not kv[1]["is_target"], kv[0])):
        mark = "*TARGET*" if info["is_target"] else "        "
        vkeys = list((info.get("sample_variables") or {}).keys())
        print(f"  {mark} {op:38} {info['sha256'][:16]}.. status={info['status']} vars={vkeys}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
