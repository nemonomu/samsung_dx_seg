"""SIEL-style quality report for SEG Amazon JSONL runs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import merge_insert
from common.jsonl import read_jsonl

_METADATA_KEYS = {
    "account_name", "product", "stage", "source_url", "asin", "item", "batch_id",
    "crawl_datetime", "crawl_strdatetime", "redirect", "landing_url", "landing_asin",
    "main_rank", "bsr_rank", "page_no", "loaded_url", "redirect_decision",
}
_VALID_NULL_BY_PRODUCT = {
    "TV": {"fastest_delivery"},
    "REF": set(),
}


def parse_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value)
    match = re.search(r"\d[\d.,]*", text)
    if not match:
        return None
    raw = match.group(0)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d[\d.,]*", str(value))
    if not match:
        return None
    try:
        return int(re.sub(r"\D", "", match.group(0)))
    except ValueError:
        return None


def _key(rec: dict[str, Any]) -> str:
    return str(rec.get("asin") or rec.get("item") or "").strip()


def collect_issues(cfg, jsonl_path: str | Path) -> tuple[dict[str, Any], int]:
    records = read_jsonl(jsonl_path)
    main, bsr, details = merge_insert.split_records(records)
    issues: dict[str, Any] = {
        "redirect": [],
        "sku_null": [],
        "price_inversion": [],
        "rating_count_no_rating": [],
        "review_count_no_review_text": [],
        "all_null_fields": [],
        "notice_null_fields": [],
        "type_mismatch": [],
        "run_error": [],
        "listing_page_failure": [],
        "detail_zero": [],
        "db_insert_zero": [],
        "stage_counts": {},
        "db_insert_summary": None,
    }
    for rec in records:
        stage = rec.get("stage")
        if stage:
            issues["stage_counts"][stage] = issues["stage_counts"].get(stage, 0) + 1
        if stage == "db_insert_summary" or rec.get("run_type") == "jsonl_db_save":
            issues["db_insert_summary"] = rec
        if rec.get("_error"):
            item = {
                "stage": stage or rec.get("run_type") or "unknown",
                "message": rec.get("message") or rec.get("_error"),
                "url": rec.get("source_url") or rec.get("product_url") or "",
                "page_no": rec.get("page_no"),
            }
            if item["stage"] in {"main", "bsr"} and item.get("page_no"):
                issues["listing_page_failure"].append(item)
            else:
                issues["run_error"].append(item)

    valid_details: list[dict[str, Any]] = []
    for rec in details:
        url = rec.get("source_url") or rec.get("product_url") or ""
        key = _key(rec)
        if rec.get("redirect") is True:
            label = rec.get("_redirect_decision") or rec.get("redirect_decision")
            issues["redirect"].append(f"{url} ({label})" if label else url)
            if rec.get("_redirect_use_landing") is not True:
                continue
        if rec.get("_detail_skip"):
            continue
        valid_details.append(rec)
        if rec.get("sku") in (None, ""):
            issues["sku_null"].append(url)
        merged = merge_insert.make_row(cfg, main.get(key), bsr.get(key), rec) if key else None
        final_price = (merged or rec).get("final_sku_price")
        original_price = (merged or rec).get("original_sku_price")
        fpv = parse_price(final_price)
        opv = parse_price(original_price)
        if fpv is not None and opv is not None and fpv >= opv:
            issues["price_inversion"].append({"url": url, "final": final_price, "original": original_price})
        rating_count = parse_int(rec.get("count_of_star_ratings"))
        if rating_count is not None and rating_count >= 1 and rec.get("star_rating") in (None, ""):
            issues["rating_count_no_rating"].append({"url": url, "count_of_star_ratings": rec.get("count_of_star_ratings")})
        review_count = parse_int(rec.get("count_of_reviews"))
        if review_count is not None and review_count >= 1 and rec.get("detailed_review_content") in (None, ""):
            issues["review_count_no_review_text"].append({"url": url, "count_of_reviews": rec.get("count_of_reviews")})

    if len(valid_details) >= 2:
        counts: dict[str, list[int]] = {}
        for rec in valid_details:
            for field, value in rec.items():
                if field in _METADATA_KEYS or field.startswith("_"):
                    continue
                slot = counts.setdefault(field, [0, 0])
                slot[0] += int(value not in (None, ""))
                slot[1] += 1
        valid_null = _VALID_NULL_BY_PRODUCT.get(str(getattr(cfg, "PRODUCT", "")).upper(), set())
        for field, (non_null, total) in sorted(counts.items()):
            if total >= 2 and non_null == 0:
                target = "notice_null_fields" if field in valid_null else "all_null_fields"
                issues[target].append({"field": field, "total": total})

    if not details:
        issues["detail_zero"].append({
            "main_records": issues["stage_counts"].get("main", 0),
            "bsr_records": issues["stage_counts"].get("bsr", 0),
        })
    db_summary = issues.get("db_insert_summary") or {}
    if db_summary:
        inserted = db_summary.get("inserted_total")
        errors = db_summary.get("errors") or []
        try:
            inserted_int = int(inserted)
        except Exception:
            inserted_int = None
        if inserted_int == 0 or errors:
            issues["db_insert_zero"].append({
                "inserted_total": inserted,
                "rows_full": db_summary.get("rows_full"),
                "message": db_summary.get("message") or "; ".join(map(str, errors[:3])),
            })
    return issues, len(details)


def build_email_report_with_severity(cfg, jsonl_path: str | Path) -> tuple[str, str]:
    issues, detail_count = collect_issues(cfg, jsonl_path)
    stage_counts = issues["stage_counts"]
    db_summary = issues.get("db_insert_summary") or {}
    has_sos = bool(issues["db_insert_zero"])
    has_warning = any(issues[k] for k in (
        "redirect", "sku_null", "price_inversion", "rating_count_no_rating",
        "review_count_no_review_text", "all_null_fields", "run_error",
        "listing_page_failure", "detail_zero",
    ))
    severity = "sos" if has_sos else ("warning" if has_warning else "ok")
    lines = [
        f"product: {getattr(cfg, 'PRODUCT', '').upper()}",
        f"jsonl: {jsonl_path}",
        f"main records: {stage_counts.get('main', 0)}",
        f"bsr records: {stage_counts.get('bsr', 0)}",
        f"detail records: {detail_count}",
    ]
    if db_summary:
        lines.append(f"db insert rows: {db_summary.get('inserted_total')}")
    lines.append("")
    if severity == "ok":
        lines.append("issues: none")
    else:
        lines.append("SOS" if severity == "sos" else "WARNING")
    if issues["db_insert_zero"]:
        item = issues["db_insert_zero"][0]
        lines.append(f"- db insert problem: inserted={item.get('inserted_total')} rows_full={item.get('rows_full')} reason={item.get('message')}")
    if issues["detail_zero"]:
        item = issues["detail_zero"][0]
        lines.append(f"- detail records = 0 (main={item.get('main_records')}, bsr={item.get('bsr_records')})")
    if issues["run_error"]:
        lines.append(f"- run errors: {len(issues['run_error'])}")
        for item in issues["run_error"][:20]:
            lines.append(f"  - stage={item.get('stage')} message={item.get('message')}")
    if issues["listing_page_failure"]:
        lines.append(f"- listing page failures: {len(issues['listing_page_failure'])}")
        for item in issues["listing_page_failure"][:20]:
            lines.append(f"  - {item.get('stage')} page={item.get('page_no')} url={item.get('url')} reason={item.get('message')}")
    if issues["redirect"]:
        lines.append(f"- redirect=true: {len(issues['redirect'])}")
        for url in issues["redirect"][:50]:
            lines.append(f"  - {url}")
    if issues["sku_null"]:
        lines.append(f"- sku null: {len(issues['sku_null'])}")
        for url in issues["sku_null"][:50]:
            lines.append(f"  - {url}")
    if issues["price_inversion"]:
        lines.append(f"- price inversion final>=original: {len(issues['price_inversion'])}")
        for item in issues["price_inversion"][:50]:
            lines.append(f"  - {item['url']} (final={item['final']}, original={item['original']})")
    if issues["rating_count_no_rating"]:
        lines.append(f"- rating count exists but star_rating null: {len(issues['rating_count_no_rating'])}")
    if issues["review_count_no_review_text"]:
        lines.append(f"- review count exists but detailed_review_content null: {len(issues['review_count_no_review_text'])}")
    if issues["all_null_fields"]:
        lines.append(f"- all-null detail fields: {len(issues['all_null_fields'])}")
        for item in issues["all_null_fields"]:
            lines.append(f"  - {item['field']} (total={item['total']})")
    if issues["notice_null_fields"]:
        lines.append("")
        lines.append("NOTICE")
        for item in issues["notice_null_fields"]:
            lines.append(f"- valid-null field: {item['field']} (total={item['total']})")
    return "\n".join(lines) + "\n", severity
