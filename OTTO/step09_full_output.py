"""Step09: build OTTO final full output for listing + detail + review.

This run uses listing API output as targets, PDP document replay with KPSDK
cookies from a local Copy-as-cURL file for detail fields, and direct review HTML.
Cookie values are never printed or persisted to the output manifest.
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

from step00_config import OUTPUT_ROOT, REFERENCES_ROOT, RETAILER, PRODUCT, COUNTRY, read_csv, write_csv, write_json
from step00_parsers import parse_detail_html, parse_review_html, text_clean

TARGETS_CSV = OUTPUT_ROOT / "otto_final_targets.csv"
DEFAULT_CURL_PATH = REFERENCES_ROOT / "detail_curl_all_cmd_v2.txt"
ARCHIVE_ROOT = Path(__file__).resolve().parent / "archive"
OUTPUT_CSV = OUTPUT_ROOT / "otto_full_output.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step09_full_output_manifest.json"

DELIMITER = " ||| "
FINAL_FIELDS = [
    "retailer",
    "product",
    "country",
    "final_target_rank",
    "main_rank",
    "bsr_rank",
    "product_id",
    "variation_id",
    "product_url",
    "retailer_sku_name",
    "final_sku_price",
    "original_sku_price",
    "savings",
    "sku_popularity",
    "sku_status",
    "discount_type",
    "delivery_availability",
    "sku",
    "screen_size",
    "estimated_annual_electricity_use",
    "retailer_sku_name_similar",
    "star_rating",
    "count_of_star_ratings",
    "count_of_reviews",
    "recommendation_intent",
    "summarized_review_content",
    "detailed_review_content",
    "detail_http_status",
    "review_http_status",
    "detail_source",
    "review_source",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build OTTO full output for final targets.")
    parser.add_argument("--targets", default=str(TARGETS_CSV))
    parser.add_argument("--curl-file", default=str(DEFAULT_CURL_PATH))
    parser.add_argument("--output", default=str(OUTPUT_CSV))
    parser.add_argument("--manifest", default=str(MANIFEST_OUTPUT))
    parser.add_argument("--limit", type=int, default=0, help="0 means all targets.")
    parser.add_argument("--start", type=int, default=1, help="1-based target row start.")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--detail-sleep", type=float, default=1.5)
    parser.add_argument("--review-sleep", type=float, default=0.3)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-sleep", type=float, default=8.0)
    parser.add_argument("--keep-html", action="store_true")
    parser.add_argument("--append-existing", action="store_true", help="Load existing output CSV and upsert rows by final_target_rank.")
    parser.add_argument("--stop-on-detail-block", action="store_true", help="Stop before writing a row when PDP detail is blocked or unavailable.")
    return parser.parse_args()


def ascii_status(value: Any) -> str:
    return str(value).encode("ascii", errors="replace").decode("ascii")


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


def extract_document_curl_block(curl_text: str) -> str:
    blocks = re.split(r"(?=curl \^\")", curl_text)
    for block in blocks:
        url_match = re.search(r"curl \^\"([^\"]+)\^\"", block)
        if not url_match:
            continue
        url = url_match.group(1)
        if "www.otto.de/p/" in url and "sec-fetch-dest: document" in block.lower():
            return block
    for block in blocks:
        url_match = re.search(r"curl \^\"([^\"]+)\^\"", block)
        if url_match and "www.otto.de/p/" in url_match.group(1):
            return block
    raise RuntimeError("No OTTO PDP document cURL block found.")


def load_detail_headers(curl_file: Path) -> dict[str, str]:
    curl_text = curl_file.read_text(encoding="utf-8", errors="replace")
    block = extract_document_curl_block(curl_text)
    cookie_match = re.search(r"-b \^\"(.*?)\^\"", block, re.S)
    cookie = cookie_match.group(1).replace("^&", "&") if cookie_match else ""
    if not cookie or "KP_UIDz" not in cookie:
        raise RuntimeError("PDP cURL block does not contain KPSDK cookies.")

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.otto.de/suche/fernseher/",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Cookie": cookie,
    }
    for raw_name in ("user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"):
        pattern = re.compile(rf"-H \^\"{re.escape(raw_name)}: (.*?)\^\"", re.I | re.S)
        match = pattern.search(block)
        if match:
            header_name = "-".join(part.capitalize() for part in raw_name.split("-"))
            if raw_name == "user-agent":
                header_name = "User-Agent"
            headers[header_name] = match.group(1).replace("^\\^\"", "\"")
    return headers


def review_headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.otto.de/suche/fernseher/",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    }


def fetch_html(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
            body = response.read()
            return {
                "status": response.status,
                "final_url": response.url,
                "content_type": response.headers.get("Content-Type"),
                "body": body,
                "error": None,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
    except HTTPError as exc:
        body = exc.read()
        return {
            "status": exc.code,
            "final_url": exc.url,
            "content_type": exc.headers.get("Content-Type") if exc.headers else None,
            "body": body,
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except URLError as exc:
        return {
            "status": None,
            "final_url": url,
            "content_type": None,
            "body": b"",
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "status": None,
            "final_url": url,
            "content_type": None,
            "body": b"",
            "error": type(exc).__name__ + ": " + str(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def fetch_with_retries(url: str | None, headers: dict[str, str], timeout: int, max_retries: int, retry_sleep: float) -> dict[str, Any]:
    if not url:
        return {"status": None, "body": b"", "error": "missing_url", "attempts": 0}
    last: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        last = fetch_html(url, headers, timeout)
        last["attempts"] = attempt + 1
        body_text = last.get("body", b"").decode("utf-8", errors="replace")
        has_challenge = last.get("status") in (403, 429) or (body_text.lower().count("kpsdk") >= 5 and body_text.count("Modellbezeichnung") == 0)
        if not has_challenge:
            return last
        if attempt < max_retries:
            time.sleep(retry_sleep * (attempt + 1))
    return last


def write_temp_html(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def rank_key(row: dict[str, Any]) -> str:
    return str(row.get("final_target_rank") or "").strip()


def rank_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    key = rank_key(row)
    try:
        return int(key), key
    except ValueError:
        return 999999, key


def has_detail_fields(row: dict[str, Any]) -> bool:
    return bool(row.get("sku") and row.get("screen_size") and row.get("estimated_annual_electricity_use"))


def upsert_output_row(rows: list[dict[str, Any]], row: dict[str, Any]) -> None:
    key = rank_key(row)
    if key:
        for index, existing in enumerate(rows):
            if rank_key(existing) == key:
                rows[index] = row
                rows.sort(key=rank_sort_key)
                return
    rows.append(row)
    rows.sort(key=rank_sort_key)


def write_final_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in FINAL_FIELDS and key not in extra_fields:
                extra_fields.append(key)
    fields = FINAL_FIELDS + extra_fields
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_detail_response(path: Path, response: dict[str, Any]) -> dict[str, Any]:
    body_text = response.get("body", b"").decode("utf-8", errors="replace")
    result = {
        "detail_http_status": response.get("status"),
        "detail_error": response.get("error"),
        "detail_bytes": len(response.get("body", b"")),
        "detail_kpsdk_count": body_text.lower().count("kpsdk"),
        "detail_modell_count": body_text.count("Modellbezeichnung"),
    }
    if response.get("status") == 200 and body_text.count("Modellbezeichnung") > 0:
        try:
            result.update(parse_detail_html(path))
            result["detail_source"] = "pdp_html_kpsdk_cookie_replay"
        except Exception as exc:
            result["detail_parse_error"] = repr(exc)
            result["detail_source"] = "pdp_html_parse_error"
    else:
        result["detail_source"] = "pdp_html_unavailable"
    return result


def parse_review_response(path: Path, response: dict[str, Any]) -> dict[str, Any]:
    body_text = response.get("body", b"").decode("utf-8", errors="replace")
    result = {
        "review_http_status": response.get("status"),
        "review_error": response.get("error"),
        "review_bytes": len(response.get("body", b"")),
        "review_kpsdk_count": body_text.lower().count("kpsdk"),
    }
    if response.get("status") == 200:
        try:
            parsed = parse_review_html(path)
            result.update(parsed)
            result["review_source"] = "review_html_direct"
        except Exception as exc:
            result["review_parse_error"] = repr(exc)
            result["review_source"] = "review_html_parse_error"
    else:
        result["review_source"] = "review_html_unavailable"
    return result


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def build_output_row(target: dict[str, Any], detail: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    count_reviews = first_non_empty(detail.get("count_of_reviews"), review.get("review_text_rows"), target.get("count_of_reviews_listing"))
    star_rating = first_non_empty(detail.get("star_rating"), target.get("average_rating_listing"))
    return {
        "retailer": RETAILER,
        "product": PRODUCT,
        "country": COUNTRY,
        "final_target_rank": target.get("final_target_rank"),
        "main_rank": target.get("main_rank"),
        "bsr_rank": target.get("bsr_rank"),
        "product_id": target.get("product_id"),
        "variation_id": target.get("variation_id"),
        "product_url": target.get("product_url"),
        "retailer_sku_name": target.get("retailer_sku_name"),
        "final_sku_price": target.get("final_sku_price"),
        "original_sku_price": target.get("original_sku_price"),
        "savings": target.get("savings"),
        "sku_popularity": target.get("sku_popularity"),
        "sku_status": target.get("sku_status"),
        "discount_type": target.get("discount_type"),
        "delivery_availability": first_non_empty(detail.get("delivery_availability"), target.get("delivery_availability")),
        "sku": detail.get("sku"),
        "screen_size": detail.get("screen_size"),
        "estimated_annual_electricity_use": detail.get("estimated_annual_electricity_use"),
        "retailer_sku_name_similar": detail.get("retailer_sku_name_similar"),
        "star_rating": star_rating,
        "count_of_star_ratings": count_reviews,
        "count_of_reviews": count_reviews,
        "recommendation_intent": first_non_empty(review.get("recommendation_intent"), detail.get("recommendation_intent")),
        "summarized_review_content": first_non_empty(review.get("summarized_review_content"), detail.get("summarized_review_content")),
        "detailed_review_content": first_non_empty(review.get("detailed_review_content"), detail.get("detailed_review_content")),
        "detail_http_status": detail.get("detail_http_status"),
        "review_http_status": review.get("review_http_status"),
        "detail_source": detail.get("detail_source"),
        "review_source": review.get("review_source"),
    }


def main() -> int:
    args = parse_args()
    targets = read_csv(Path(args.targets))
    start_index = max(args.start, 1) - 1
    end_index = len(targets) if args.limit <= 0 else min(len(targets), start_index + args.limit)
    selected_all = targets[start_index:end_index]

    detail_headers = load_detail_headers(Path(args.curl_file))
    r_headers = review_headers()
    run_dir = ARCHIVE_ROOT / f"full_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    html_dir = run_dir / "html"
    output_path = Path(args.output)
    existing_rows = read_csv(output_path) if args.append_existing and output_path.exists() else []
    complete_existing_ranks = {rank_key(row) for row in existing_rows if has_detail_fields(row)}
    selected = [row for row in selected_all if rank_key(row) not in complete_existing_ranks]
    rows: list[dict[str, Any]] = list(existing_rows)
    attempts: list[dict[str, Any]] = []
    manifest_path = Path(args.manifest)
    blocked_info: dict[str, Any] | None = None

    def write_progress(is_final: bool = False) -> None:
        write_final_csv(output_path, rows)
        manifest = {
            "run_type": "step09_full_output",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "targets_input": str(Path(args.targets)),
            "output_csv": str(output_path),
            "selected_start": args.start,
            "selected_rows_requested": len(selected_all),
            "selected_rows_skipped_complete_existing": len(selected_all) - len(selected),
            "selected_rows": len(selected),
            "existing_rows_loaded": len(existing_rows),
            "output_rows": len(rows),
            "selected_completed": is_final and len([item for item in attempts if item.get("row_written")]) == len(selected),
            "completed": is_final and blocked_info is None,
            "blocked": blocked_info is not None,
            "blocked_info": blocked_info,
            "uses_detail_cookie_replay": True,
            "curl_file": str(Path(args.curl_file)),
            "cookie_values_redacted": True,
            "detail_success_rows": sum(1 for row in rows if row.get("sku") and row.get("screen_size") and row.get("estimated_annual_electricity_use")),
            "detail_http_200_rows": sum(1 for item in attempts if item.get("detail_status") == 200),
            "review_http_200_rows": sum(1 for item in attempts if item.get("review_status") == 200),
            "detailed_review_content_rows": sum(1 for row in rows if row.get("detailed_review_content")),
            "summarized_review_content_rows": sum(1 for row in rows if row.get("summarized_review_content")),
            "missing_required_fields": {
                field: sum(1 for row in rows if row.get(field) in (None, ""))
                for field in FINAL_FIELDS
                if field not in {"bsr_rank", "sku_popularity", "sku_status", "discount_type", "summarized_review_content", "detailed_review_content"}
            },
            "attempts": attempts,
        }
        write_json(manifest_path, manifest)

    for offset, target in enumerate(selected, start=start_index + 1):
        rank = target.get("final_target_rank") or str(offset)
        variation_id = target.get("variation_id") or f"row{offset}"
        label = f"rank_{int(rank):03d}_{variation_id}" if str(rank).isdigit() else f"row_{offset:03d}_{variation_id}"

        detail_response = fetch_with_retries(target.get("product_url"), detail_headers, args.timeout, args.max_retries, args.retry_sleep)
        detail_path = html_dir / f"{label}_detail.html"
        write_temp_html(detail_path, detail_response.get("body", b""))
        detail = parse_detail_response(detail_path, detail_response)
        if args.stop_on_detail_block and detail.get("detail_source") == "pdp_html_unavailable":
            blocked_info = {
                "rank": rank,
                "variation_id": variation_id,
                "detail_status": detail.get("detail_http_status"),
                "detail_source": detail.get("detail_source"),
                "detail_attempts": detail_response.get("attempts"),
                "detail_error": detail.get("detail_error"),
            }
            attempts.append({
                "rank": rank,
                "variation_id": variation_id,
                "detail_status": detail.get("detail_http_status"),
                "detail_source": detail.get("detail_source"),
                "detail_attempts": detail_response.get("attempts"),
                "detail_has_sku": False,
                "review_status": None,
                "review_source": None,
                "review_count": None,
                "has_detailed_review_content": False,
                "has_summary": False,
                "row_written": False,
            })
            if not args.keep_html:
                try:
                    detail_path.unlink()
                except OSError:
                    pass
            print(
                "[step09] stopped_on_detail_block rank={rank} detail={status}/{source}".format(
                    rank=rank,
                    status=detail.get("detail_http_status"),
                    source=detail.get("detail_source"),
                ),
                flush=True,
            )
            write_progress(False)
            return 3
        if args.detail_sleep > 0:
            time.sleep(args.detail_sleep)

        review_url = review_url_for(target)
        review_response = fetch_with_retries(review_url, r_headers, args.timeout, 0, args.retry_sleep)
        review_path = html_dir / f"{label}_review.html"
        write_temp_html(review_path, review_response.get("body", b""))
        review = parse_review_response(review_path, review_response)
        if not args.keep_html:
            for path in (detail_path, review_path):
                try:
                    path.unlink()
                except OSError:
                    pass
        if args.review_sleep > 0:
            time.sleep(args.review_sleep)

        row = build_output_row(target, detail, review)
        upsert_output_row(rows, row)
        attempts.append({
            "rank": rank,
            "variation_id": variation_id,
            "detail_status": detail.get("detail_http_status"),
            "detail_source": detail.get("detail_source"),
            "detail_attempts": detail_response.get("attempts"),
            "detail_has_sku": bool(row.get("sku")),
            "review_status": review.get("review_http_status"),
            "review_source": review.get("review_source"),
            "review_count": review.get("detailed_review_count"),
            "has_detailed_review_content": bool(row.get("detailed_review_content")),
            "has_summary": bool(row.get("summarized_review_content")),
            "row_written": True,
        })
        print(
            "[step09] rank={rank} detail={d_status}/{d_source} sku={sku} review={r_status}/{r_count}".format(
                rank=rank,
                d_status=detail.get("detail_http_status"),
                d_source=detail.get("detail_source"),
                sku=ascii_status(row.get("sku") or ""),
                r_status=review.get("review_http_status"),
                r_count=review.get("detailed_review_count"),
            ),
            flush=True,
        )
        write_progress(False)

    write_progress(True)
    final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"[step09] output={output_path} rows={len(rows)} detail_success={final_manifest['detail_success_rows']} review_200={final_manifest['review_http_200_rows']}")
    written_rows = sum(1 for item in attempts if item.get("row_written"))
    return 0 if blocked_info is None and written_rows == len(selected) else 2


if __name__ == "__main__":
    raise SystemExit(main())
