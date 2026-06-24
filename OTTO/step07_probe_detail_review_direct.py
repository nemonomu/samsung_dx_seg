"""Step07: probe OTTO detail/review pages without ZenRows.

This script intentionally does not load .env and does not use ZenRows. It checks
whether direct HTTP GET is enough for PDP detail and customer review pages.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from step00_config import DETAIL_SAMPLE_URL, OUTPUT_ROOT, REFERENCES_ROOT, REVIEW_SAMPLE_URL
from step00_parsers import parse_detail_html, parse_review_html, text_clean

OUTPUT_BASE = REFERENCES_ROOT / "xhr"
FINAL_TARGETS = OUTPUT_ROOT / "otto_final_targets.csv"

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://www.otto.de/suche/fernseher/?sortiertnach=topseller",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe OTTO detail/review direct HTTP access without ZenRows.")
    parser.add_argument("--limit", type=int, default=5, help="Number of final targets to probe after the sample.")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--skip-sample", action="store_true")
    return parser.parse_args()


def read_final_targets(limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not FINAL_TARGETS.exists():
        return []
    with FINAL_TARGETS.open("r", newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    return rows[:limit]


def product_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"-((?:C)?[A-Z0-9]+)/(?:\?|$)", url, re.I)
    return match.group(1) if match else None


def review_url_for(row: dict[str, Any]) -> str | None:
    product_id = text_clean(row.get("product_id")) or product_id_from_url(row.get("product_url"))
    if not product_id:
        return None
    return f"https://www.otto.de/kundenbewertungen/{product_id}/"


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return cleaned[:120] or "item"


def fetch_direct(url: str, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    request = Request(url, headers=DEFAULT_HEADERS, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            return {
                "url": url,
                "final_url": response.url,
                "status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "body": body,
                "error": None,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
    except HTTPError as exc:
        body = exc.read()
        return {
            "url": url,
            "final_url": exc.url,
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type") if exc.headers else None,
            "body": body,
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except URLError as exc:
        return {
            "url": url,
            "final_url": url,
            "status": None,
            "content_type": None,
            "body": b"",
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def body_signals(body: bytes) -> dict[str, Any]:
    html = body.decode("utf-8", errors="replace")
    lowered = html.lower()
    return {
        "html_bytes": len(body),
        "body_sha1": hashlib.sha1(body).hexdigest(),
        "kpsdk": "kpsdk" in lowered,
        "captcha": "captcha" in lowered,
        "application_ld_json": html.count("application/ld+json"),
        "modellbezeichnung": html.count("Modellbezeichnung"),
        "screen_size_label": html.count("Bildschirmdiagonale in Zoll"),
        "electricity_label": html.count("Leistungsaufnahme im Ein-Zustand"),
        "review_id": html.count("data-review-id"),
        "review_text_term": lowered.count("reviewtext"),
        "bewertung_term": lowered.count("bewertung"),
    }


def write_body(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def parse_saved_detail(path: Path) -> dict[str, Any]:
    try:
        parsed = parse_detail_html(path)
        return {
            "parse_error": None,
            "sku": parsed.get("sku"),
            "screen_size": parsed.get("screen_size"),
            "estimated_annual_electricity_use": parsed.get("estimated_annual_electricity_use"),
            "delivery_availability": parsed.get("delivery_availability"),
            "retailer_sku_name_similar_present": bool(parsed.get("retailer_sku_name_similar")),
            "star_rating": parsed.get("star_rating"),
            "count_of_reviews": parsed.get("count_of_reviews"),
            "top_review_rows": parsed.get("top_review_rows"),
            "summarized_review_present": bool(parsed.get("summarized_review_content")),
        }
    except Exception as exc:
        return {"parse_error": repr(exc)}


def parse_saved_review(path: Path) -> dict[str, Any]:
    try:
        parsed = parse_review_html(path)
        return {
            "parse_error": None,
            "title": parsed.get("title"),
            "review_rows": parsed.get("review_rows"),
            "review_text_rows": parsed.get("review_text_rows"),
            "detailed_review_count": parsed.get("detailed_review_count"),
            "detailed_review_present": bool(parsed.get("detailed_review_content")),
            "summarized_review_present": bool(parsed.get("summarized_review_content")),
            "recommendation_intent": parsed.get("recommendation_intent"),
        }
    except Exception as exc:
        return {"parse_error": repr(exc)}


def probe_one(label: str, detail_url: str | None, review_url: str | None, output_dir: Path, timeout: int, sleep: float) -> dict[str, Any]:
    result: dict[str, Any] = {"label": label, "detail_url": detail_url, "review_url": review_url}

    if detail_url:
        detail_response = fetch_direct(detail_url, timeout)
        detail_path = output_dir / f"{safe_name(label)}_detail.html"
        write_body(detail_path, detail_response["body"])
        detail_signals = body_signals(detail_response["body"])
        detail_parsed = parse_saved_detail(detail_path)
        detail_ok = (
            detail_response["status"] == 200
            and not detail_signals["kpsdk"]
            and any(detail_parsed.get(key) for key in ("sku", "screen_size", "estimated_annual_electricity_use"))
        )
        result["detail"] = {
            "status": detail_response["status"],
            "content_type": detail_response["content_type"],
            "final_url": detail_response["final_url"],
            "error": detail_response["error"],
            "elapsed_seconds": detail_response["elapsed_seconds"],
            "html_file": str(detail_path),
            "signals": detail_signals,
            "parsed": detail_parsed,
            "direct_detail_ok": detail_ok,
            "blocked_or_challenge": detail_response["status"] in (403, 429) or detail_signals["kpsdk"],
        }
        if sleep > 0:
            time.sleep(sleep)

    if review_url:
        review_response = fetch_direct(review_url, timeout)
        review_path = output_dir / f"{safe_name(label)}_review.html"
        write_body(review_path, review_response["body"])
        review_signals = body_signals(review_response["body"])
        review_parsed = parse_saved_review(review_path)
        review_page_ok = review_response["status"] == 200 and not review_signals["captcha"] and bool(review_parsed.get("title"))
        result["review"] = {
            "status": review_response["status"],
            "content_type": review_response["content_type"],
            "final_url": review_response["final_url"],
            "error": review_response["error"],
            "elapsed_seconds": review_response["elapsed_seconds"],
            "html_file": str(review_path),
            "signals": review_signals,
            "parsed": review_parsed,
            "direct_review_page_ok": review_page_ok,
            "direct_detailed_review_ok": bool(review_parsed.get("detailed_review_present")),
            "blocked_or_challenge": review_response["status"] in (403, 429) or review_signals["captcha"],
        }
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    detail_results = [item.get("detail") for item in results if item.get("detail")]
    review_results = [item.get("review") for item in results if item.get("review")]
    return {
        "detail_attempts": len(detail_results),
        "detail_direct_ok": sum(1 for item in detail_results if item.get("direct_detail_ok")),
        "detail_blocked_or_challenge": sum(1 for item in detail_results if item.get("blocked_or_challenge")),
        "review_attempts": len(review_results),
        "review_page_direct_ok": sum(1 for item in review_results if item.get("direct_review_page_ok")),
        "review_detailed_review_ok": sum(1 for item in review_results if item.get("direct_detailed_review_ok")),
        "review_blocked_or_challenge": sum(1 for item in review_results if item.get("blocked_or_challenge")),
    }


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_BASE / f"detail_review_direct_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)

    targets: list[dict[str, Any]] = []
    if not args.skip_sample:
        targets.append({
            "label": "sample_philips",
            "detail_url": DETAIL_SAMPLE_URL,
            "review_url": REVIEW_SAMPLE_URL,
        })
    for idx, row in enumerate(read_final_targets(args.limit), start=1):
        targets.append({
            "label": f"target_{idx:03d}_{row.get('variation_id') or row.get('product_id') or idx}",
            "detail_url": row.get("product_url"),
            "review_url": review_url_for(row),
            "target_rank": row.get("final_target_rank"),
            "retailer_sku_name": row.get("retailer_sku_name"),
        })

    results: list[dict[str, Any]] = []
    for target in targets:
        result = probe_one(target["label"], target.get("detail_url"), target.get("review_url"), output_dir, args.timeout, args.sleep)
        for key in ("target_rank", "retailer_sku_name"):
            if key in target:
                result[key] = target[key]
        results.append(result)
        detail = result.get("detail") or {}
        review = result.get("review") or {}
        print(
            "[direct-probe] {label} detail={d_status}/{d_ok} review={r_status}/{r_ok} detailed_reviews={rv_count}".format(
                label=target["label"],
                d_status=detail.get("status"),
                d_ok=detail.get("direct_detail_ok"),
                r_status=review.get("status"),
                r_ok=review.get("direct_review_page_ok"),
                rv_count=(review.get("parsed") or {}).get("detailed_review_count"),
            )
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    manifest = {
        "run_type": "step07_probe_detail_review_direct",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "uses_zenrows": False,
        "loads_env": False,
        "final_targets_input": str(FINAL_TARGETS),
        "output_dir": str(output_dir),
        "summary": summarize(results),
        "results": results,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[direct-probe] summary={summary_path}")
    return 0 if manifest["summary"]["review_page_direct_ok"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())