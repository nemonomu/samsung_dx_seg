"""Capture MediaMarkt's native PDP GraphQL traffic with local UC Chrome.

This diagnostic uses the same local browser/session as the production crawler.
It does not use ZenRows and does not touch crawler CSVs or the database.

  python step00_capture_uc_graphql.py
  python step00_capture_uc_graphql.py --url <MediaMarkt PDP URL>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from common.config import REFERENCES_ROOT
from common.pdp_browser import _comparison_vars, _reviews_vars, _summary_vars
from common.uc import UcSession


DEFAULT_PDP = (
    "https://www.mediamarkt.de/de/product/"
    "_samsung-gq55q7f-qled-tv-vision-ai-smart-tv-55-zoll-138-cm-"
    "uhd-4k-smart-tv-tizen-2988691.html"
)
TARGET_OPERATIONS = {
    "GetComparisonTableRecommendations",
    "GetReviewsSummary",
    "GetProductReviews",
}
SAFE_REQUEST_HEADERS = {
    "accept",
    "apollographql-client-name",
    "apollographql-client-version",
    "content-type",
    "origin",
    "referer",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "x-cacheable",
    "x-flow",
    "x-flow-id",
    "x-mms-country",
    "x-mms-language",
    "x-mms-salesline",
    "x-operation",
    "x-operation-name",
    "x-pwa-mms-build",
}
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "cf-cache-status",
    "cf-ray",
    "content-type",
    "server",
    "x-request-id",
}
TRIGGER_TEXTS = (
    "Bewertungen",
    "weitere Bewertungen",
    "Mehr Bewertungen",
    "Zusammenfassung der KI",
    "KI-Zusammenfassung",
    "Alternativen",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture native MediaMarkt PDP GraphQL traffic with local UC."
    )
    parser.add_argument("--url", default=DEFAULT_PDP)
    parser.add_argument("--settle", type=float, default=6.0)
    parser.add_argument(
        "--manual-timeout",
        type=int,
        default=180,
        help="seconds to wait for a manually completed Cloudflare challenge",
    )
    return parser.parse_args()


def sku_from_url(url: str) -> str:
    match = re.search(r"-(\d+)\.html", url)
    return match.group(1) if match else "unknown"


def safe_headers(headers: dict[str, Any], allowed: set[str]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() in allowed
    }


def parse_graphql_url(url: str) -> dict[str, Any]:
    query = parse_qs(urlparse(url).query)
    operation = (query.get("operationName") or [None])[0]
    variables = None
    extensions = None
    try:
        variables = json.loads((query.get("variables") or ["null"])[0])
    except Exception:
        pass
    try:
        extensions = json.loads((query.get("extensions") or ["null"])[0])
    except Exception:
        pass
    persisted = (extensions or {}).get("persistedQuery") or {}
    return {
        "operation_name": operation,
        "persisted_sha256": persisted.get("sha256Hash"),
        "variables": variables,
        "extensions": extensions,
    }


def parse_performance_logs(logs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for entry in logs:
        try:
            event = json.loads(entry["message"])["message"]
            method = event["method"]
            params = event["params"]
        except Exception:
            continue
        request_id = str(params.get("requestId") or "")
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = request.get("url") or ""
            if "/api/v1/graphql" not in url:
                continue
            record = records.setdefault(request_id, {"request_id": request_id})
            record.update({
                "url": url,
                "method": request.get("method"),
                "request_headers": safe_headers(
                    request.get("headers") or {}, SAFE_REQUEST_HEADERS
                ),
                **parse_graphql_url(url),
            })
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            url = response.get("url") or ""
            if "/api/v1/graphql" not in url:
                continue
            record = records.setdefault(request_id, {
                "request_id": request_id,
                "url": url,
                **parse_graphql_url(url),
            })
            record.update({
                "status": response.get("status"),
                "mime_type": response.get("mimeType"),
                "response_headers": safe_headers(
                    response.get("headers") or {}, SAFE_RESPONSE_HEADERS
                ),
                "from_disk_cache": response.get("fromDiskCache"),
                "from_service_worker": response.get("fromServiceWorker"),
            })
    return records


def wait_for_manual_clearance(
    session: UcSession, label: str, timeout_s: int
) -> bool:
    """Wait while the visible browser is on a Cloudflare challenge page."""
    if not session._blocked(session.driver.page_source or ""):
        return True
    print(
        f"[uc-capture] {label} is blocked. Complete the CAPTCHA in Chrome "
        f"(waiting up to {timeout_s}s) ...",
        flush=True,
    )
    deadline = time.monotonic() + timeout_s
    next_progress = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            blocked = session._blocked(session.driver.page_source or "")
            current_url = session.driver.current_url
        except Exception:
            return False
        if not blocked:
            print(
                f"[uc-capture] {label} CAPTCHA cleared -> {current_url}",
                flush=True,
            )
            return True
        if time.monotonic() >= next_progress:
            remaining = max(0, int(deadline - time.monotonic()))
            print(
                f"[uc-capture] still waiting for CAPTCHA ({remaining}s left) ...",
                flush=True,
            )
            next_progress = time.monotonic() + 15
    print(f"[uc-capture] {label} CAPTCHA wait timed out", flush=True)
    return False


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sku_id = sku_from_url(args.url)
    out_dir = REFERENCES_ROOT / "uc_graphql" / f"pdp_{sku_id}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = UcSession(
        headless=False,
        settle_s=args.settle,
        warmup_s=3.0,
        review_pages=1,
        performance_logging=True,
        block_images=False,
    )
    navigation: dict[str, Any] = {}
    scripts: list[str] = []
    logs: list[dict[str, Any]] = []
    try:
        session.open()
        if not wait_for_manual_clearance(session, "home", args.manual_timeout):
            return 2
        # A consent layer can appear only after the challenge is cleared.
        session._click_consent()
        # Remove home warmup events so the capture contains only the target PDP.
        session.driver.get_log("performance")
        navigation = session.navigate(args.url, settle_s=args.settle)
        if navigation.get("blocked"):
            if not wait_for_manual_clearance(session, "PDP", args.manual_timeout):
                return 2
            time.sleep(args.settle)
            navigation = {
                "html": session.driver.page_source or "",
                "blocked": session._blocked(session.driver.page_source or ""),
                "error": None,
                "url": session.driver.current_url,
            }
        scripts = session.driver.execute_script(
            "return Array.from(document.scripts).map(s=>s.src).filter(Boolean)"
        ) or []

        # Trigger lazy recommendation/review traffic using normal page behavior.
        for fraction in (0.25, 0.5, 0.75, 1.0):
            session.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight*arguments[0])",
                fraction,
            )
            time.sleep(1.0)
        from selenium.webdriver.common.by import By

        for text_value in TRIGGER_TEXTS:
            xpath = (
                "//*[self::button or self::a]"
                f"[contains(normalize-space(.), {json.dumps(text_value)})]"
            )
            try:
                elements = session.driver.find_elements(By.XPATH, xpath)
                if elements:
                    session.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'})", elements[0]
                    )
                    elements[0].click()
                    time.sleep(1.0)
            except Exception:
                continue
        time.sleep(args.settle)
        logs = session.driver.get_log("performance")
        records = parse_performance_logs(logs)

        # Replay the crawler's own request recipe in the same cleared PDP
        # session. Native=200 plus replay=200 proves the captured header/hash
        # update is sufficient without relying on a separate browser run.
        replay_specs = [
            ("GetComparisonTableRecommendations", _comparison_vars(sku_id)),
            ("GetReviewsSummary", _summary_vars(sku_id)),
            ("GetProductReviews", _reviews_vars(sku_id, 1)),
        ]
        replay_results = session._gql_many(replay_specs)
        crawler_replay = [
            {
                "operation_name": operation,
                "status": result.get("status"),
                "content_type": result.get("content_type"),
                "body_preview": result.get("body_preview"),
                "transport_error": result.get("transport_error"),
            }
            for (operation, _), result in zip(replay_specs, replay_results)
        ]

        for index, record in enumerate(records.values(), start=1):
            if record.get("operation_name") not in TARGET_OPERATIONS:
                continue
            try:
                body_result = session.driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": record["request_id"]}
                )
                body = body_result.get("body") or ""
                suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", record["operation_name"])
                body_path = out_dir / f"response_{index:03d}_{suffix}.txt"
                body_path.write_text(body, encoding="utf-8")
                record["response_body_file"] = body_path.name
                record["response_body_preview"] = " ".join(body.split())[:500]
            except Exception as exc:
                record["response_body_error"] = type(exc).__name__ + ": " + str(exc)

        build_ids = sorted({
            match.group(1)
            for script in scripts
            if (match := re.search(r"/assets/webmobile-pwa/([^/]+)/", script))
        })
        manifest = {
            "run_type": "mmkt_uc_native_graphql_capture",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url": args.url,
            "sku_id": sku_id,
            "final_url": getattr(session.driver, "current_url", None),
            "navigation_blocked": navigation.get("blocked"),
            "navigation_error": navigation.get("error"),
            "chrome_major": session.version_main,
            "pwa_build_ids": build_ids,
            "script_urls": scripts,
            "graphql_requests": list(records.values()),
            "crawler_replay": crawler_replay,
        }
        capture_path = out_dir / "capture.json"
        capture_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"[uc-capture] build={build_ids or ['unknown']} blocked={navigation.get('blocked')}")
        print(f"[uc-capture] graphql_requests={len(records)} -> {capture_path}")
        for record in records.values():
            operation = record.get("operation_name") or "unknown"
            sha = record.get("persisted_sha256") or ""
            client = record.get("request_headers", {}).get(
                "apollographql-client-version"
            )
            print(
                f"[uc-capture] {operation} status={record.get('status')} "
                f"sha={sha[:16] or '-'} client={client or '-'}"
            )
        for result in crawler_replay:
            print(
                f"[uc-capture] crawler-replay {result['operation_name']} "
                f"status={result['status']}"
            )
        return 0 if records else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
