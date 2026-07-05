"""SIEL-style quality report and email sender for SEG Amazon JSONL runs."""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from common import merge_insert, siel_logging as siel_log
from common.jsonl import read_jsonl

_METADATA_KEYS = {
    "account_name", "product", "stage", "company", "division", "source_url", "asin", "item",
    "batch_id", "crawl_datetime", "crawl_strdatetime", "redirect", "landing_url", "landing_asin",
    "main_rank", "bsr_rank", "page_no", "loaded_url", "redirect_decision", "calendar_week",
}
_VALID_NULL_BY_PRODUCT = {
    "HHP": {"fastest_delivery", "sku_assurance"},
    "TV": {"fastest_delivery"},
    "REF": set(),
    "LDY": set(),
}
_NOTICE_NULL_BY_PRODUCT = {
    "TV": {"fastest_delivery"},
}
_EXPECTED_DETAIL_FIELDS_BY_PRODUCT = {
    "TV": {
        "sku",
        "screen_size",
        "model_year",
        "estimated_annual_electricity_use",
        "available_quantity_for_purchase",
    },
    "REF": {"sku", "ref_refrigerator_type", "ref_capacity"},
}
_EURO = "\u20ac"
_FIELD_PATTERNS = {
    "star_rating": re.compile(r"^\d+(?:\.\d+)?$"),
    "count_of_star_ratings": re.compile(r"^\d[\d,]*$"),
    "count_of_reviews": re.compile(r"^\d[\d,]*$"),
    "final_sku_price": re.compile(
        rf"^(?:\d[\d.]*(?:,\d{{2}})?{re.escape(_EURO)}|{re.escape(_EURO)}\d[\d.]*(?:,\d{{2}})?)$"
    ),
    "original_sku_price": re.compile(
        rf"^(?:\d[\d.]*(?:,\d{{2}})?{re.escape(_EURO)}|{re.escape(_EURO)}\d[\d.]*(?:,\d{{2}})?)$"
    ),
}
_PRICE_SENTINELS = (
    "Currently unavailable",
    "No featured offers",
    "See price in cart",
    "Temporarily out of stock",
    "Price higher than typical",
    "Derzeit nicht verf\u00fcgbar",
    "Derzeit nicht verfuegbar",
    "Keine hervorgehobenen Angebote verf\u00fcgbar",
    "Keine hervorgehobenen Angebote verfuegbar",
)
_STAR_SENTINELS = ("No customer reviews",)


class _ProductCfg:
    def __init__(self, product: str):
        self.PRODUCT = product


def _product_name(cfg_or_product: Any) -> str:
    return str(getattr(cfg_or_product, "PRODUCT", cfg_or_product) or "").upper()


def _cfg_for_merge(cfg_or_product: Any) -> Any:
    if hasattr(cfg_or_product, "PRODUCT"):
        return cfg_or_product
    return _ProductCfg(_product_name(cfg_or_product))


def parse_price(value: Any) -> float | None:
    return siel_log.parse_price(value)


def parse_int(value: Any) -> int | None:
    return siel_log.parse_int_field(value)


def _key(rec: dict[str, Any] | None) -> str:
    return str((rec or {}).get("asin") or (rec or {}).get("item") or "").strip()


def _check_field_pattern(field: str, value: Any) -> bool:
    pattern = _FIELD_PATTERNS.get(field)
    if pattern is None or value in (None, ""):
        return True
    text = str(value).strip()
    if field in {"final_sku_price", "original_sku_price"} and any(token.casefold() in text.casefold() for token in _PRICE_SENTINELS):
        return True
    if field == "star_rating" and any(token.casefold() in text.casefold() for token in _STAR_SENTINELS):
        return True
    return bool(pattern.match(text))


def collect_issues(cfg_or_product: Any, jsonl_path: str | Path) -> tuple[dict[str, Any], int]:
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
        "detail_zero": [],
        "stage_counts": {},
        "stage_summaries": {},
        "listing_page_failure": [],
        "db_insert_summary": None,
        "db_insert_zero": [],
    }
    if not jsonl_path or not Path(jsonl_path).exists():
        return issues, 0

    records = read_jsonl(jsonl_path)
    main, bsr, detail_records = merge_insert.split_records(records)
    for rec in records:
        stage = rec.get("stage")
        if stage:
            issues["stage_counts"][stage] = issues["stage_counts"].get(stage, 0) + 1
        if rec.get("_error"):
            error_stage = rec.get("error_stage") or stage or "unknown"
            is_listing_page_failure = (
                error_stage in {"main", "bsr"}
                and rec.get("page_no") not in (None, "")
                and rec.get("_error") in {"listing page load failed", "listing page has no cards"}
            )
            item = {
                "stage": error_stage,
                "page_no": rec.get("page_no"),
                "url": rec.get("source_url") or rec.get("product_url") or "",
                "message": rec.get("message") or rec.get("_error"),
            }
            if is_listing_page_failure:
                issues["listing_page_failure"].append(item)
            else:
                issues["run_error"].append(item)
        if rec.get("_summary") and rec.get("summary_stage"):
            issues["stage_summaries"][rec.get("summary_stage")] = rec
        if stage == "db_insert_summary" or rec.get("run_type") == "jsonl_db_save":
            issues["db_insert_summary"] = rec

    product_key = _product_name(cfg_or_product)
    cfg_for_merge = _cfg_for_merge(cfg_or_product)
    detail_count = 0
    valid_details: list[dict[str, Any]] = []
    for rec in detail_records:
        detail_count += 1
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

        merged = merge_insert.make_row(cfg_for_merge, main.get(key), bsr.get(key), rec) if key else None
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

        for field in _FIELD_PATTERNS:
            value = rec.get(field)
            if value in (None, ""):
                continue
            if not _check_field_pattern(field, value):
                issues["type_mismatch"].append({"url": url, "field": field, "value": value})

    if len(valid_details) >= 2:
        expected_fields = _EXPECTED_DETAIL_FIELDS_BY_PRODUCT.get(product_key, set())
        counts: dict[str, list[int]] = {field: [0, len(valid_details)] for field in expected_fields}
        for rec in valid_details:
            for field, value in rec.items():
                if field in _METADATA_KEYS or field.startswith("_"):
                    continue
                slot = counts.setdefault(field, [0, 0])
                slot[0] += int(value not in (None, ""))
                if field not in expected_fields:
                    slot[1] += 1
        valid_null = _VALID_NULL_BY_PRODUCT.get(product_key, set())
        notice_null = _NOTICE_NULL_BY_PRODUCT.get(product_key, set())
        for field, (non_null, total) in sorted(counts.items()):
            if field == "sku":
                continue
            if total >= 2 and non_null == 0:
                if field in valid_null:
                    if field in notice_null:
                        issues["notice_null_fields"].append({"field": field, "total": total})
                    continue
                issues["all_null_fields"].append({"field": field, "total": total})

    if detail_count == 0:
        issues["detail_zero"].append({
            "main_records": issues["stage_counts"].get("main", 0),
            "bsr_records": issues["stage_counts"].get("bsr", 0),
        })
    db_summary = issues.get("db_insert_summary") or {}
    if db_summary:
        inserted = db_summary.get("inserted_total")
        returncode = db_summary.get("returncode")
        try:
            inserted_int = int(inserted)
        except Exception:
            inserted_int = None
        if inserted_int == 0 or (returncode not in (None, 0) and not inserted_int):
            issues["db_insert_zero"].append({
                "inserted_total": inserted,
                "returncode": returncode,
                "rows_full": db_summary.get("rows_full"),
                "rows_listing": db_summary.get("rows_listing"),
                "message": db_summary.get("message") or "; ".join(map(str, (db_summary.get("errors") or [])[:3])),
            })
    elif detail_count == 0:
        issues["db_insert_zero"].append({
            "inserted_total": None,
            "returncode": None,
            "rows_full": None,
            "rows_listing": None,
            "message": "detail records = 0 and no db_insert_summary found",
        })
    return issues, detail_count


def build_email_report_with_severity(cfg_or_product: Any, jsonl_path: str | Path) -> tuple[str, str]:
    issues, detail_count = collect_issues(cfg_or_product, jsonl_path)
    redirects = issues["redirect"]
    sku_nulls = issues["sku_null"]
    price_inv = issues["price_inversion"]
    rating_mis = issues["rating_count_no_rating"]
    review_mis = issues["review_count_no_review_text"]
    all_null = issues["all_null_fields"]
    notice_null = issues.get("notice_null_fields", [])
    type_mis = issues["type_mismatch"]
    run_errors = issues.get("run_error", [])
    detail_zero = issues.get("detail_zero", [])
    stage_counts = issues.get("stage_counts", {})
    listing_page_failures = issues.get("listing_page_failure", [])
    db_insert_zero = issues.get("db_insert_zero", [])
    db_summary = issues.get("db_insert_summary") or {}
    has_warning = bool(
        redirects or sku_nulls or price_inv or rating_mis or review_mis or all_null or type_mis
        or run_errors or detail_zero or listing_page_failures
    )
    has_sos = bool(db_insert_zero)
    severity = "sos" if has_sos else ("warning" if has_warning else "ok")
    product = _product_name(cfg_or_product)

    lines = [
        f"product: {product}",
        f"main records: {stage_counts.get('main', 0)}",
        f"bsr records: {stage_counts.get('bsr', 0)}",
        f"detail records: {detail_count}",
        "",
    ]
    run_log_path = db_summary.get("run_log_path") or os.path.splitext(str(jsonl_path))[0] + ".log"
    if run_log_path:
        lines.insert(-1, f"run log: {run_log_path}")
    if db_summary:
        lines.insert(-1, f"db insert rows: {db_summary.get('inserted_total')}")

    if severity == "ok":
        if notice_null:
            lines.append("NOTICE")
            lines.append(f"- valid-null fields: {len(notice_null)}")
            for item in notice_null:
                lines.append(f"  - {item['field']} (total={item['total']})")
        else:
            lines.append("issues: none")
        return "\n".join(lines) + "\n", severity

    lines.append("SOS" if severity == "sos" else "WARNING")
    if db_insert_zero:
        item = db_insert_zero[0]
        lines.append(
            "- db insert rows = 0 "
            f"(returncode={item.get('returncode')}, rows_full={item.get('rows_full')}, rows_listing={item.get('rows_listing')})"
        )
        if item.get("message"):
            lines.append(f"  - reason={item.get('message')}")
    if detail_zero:
        item = detail_zero[0]
        lines.append(f"- detail records = 0 (main={item.get('main_records', 0)}, bsr={item.get('bsr_records', 0)})")
    if run_errors:
        lines.append(f"- run errors: {len(run_errors)}")
        for item in run_errors[:20]:
            lines.append(f"  - stage={item.get('stage')} message={item.get('message')}")
    if listing_page_failures:
        lines.append(f"- listing page failures: {len(listing_page_failures)}")
        for item in listing_page_failures[:20]:
            lines.append(f"  - {item.get('stage')} page={item.get('page_no')} url={item.get('url')} reason={item.get('message')}")
    if redirects:
        lines.append(f"- redirect=true: {len(redirects)}")
        for url in redirects:
            lines.append(f"  - {url}")
    if sku_nulls:
        lines.append(f"- sku null: {len(sku_nulls)}")
        for url in sku_nulls:
            lines.append(f"  - {url}")
    if price_inv:
        lines.append(f"- price inversion (final >= original): {len(price_inv)}")
        for item in price_inv:
            lines.append(f"  - {item['url']} (final={item['final']}, original={item['original']})")
    if rating_mis:
        lines.append(f"- count_of_star_ratings>=1 but star_rating null: {len(rating_mis)}")
        for item in rating_mis:
            lines.append(f"  - {item['url']} (count_of_star_ratings={item['count_of_star_ratings']})")
    if review_mis:
        lines.append(f"- count_of_reviews>=1 but detailed_review_content null: {len(review_mis)}")
        for item in review_mis:
            lines.append(f"  - {item['url']} (count_of_reviews={item['count_of_reviews']})")
    if all_null:
        lines.append(f"- detail record all-null field (XPath/layout drift suspected): {len(all_null)}")
        for item in all_null:
            lines.append(f"  - {item['field']} (total={item['total']})")
    if type_mis:
        lines.append(f"- field shape mismatch (XPath drift suspected): {len(type_mis)}")
        for item in type_mis:
            value = item["value"]
            value_display = str(value) if len(str(value)) <= 80 else str(value)[:80] + "..."
            lines.append(f"  - {item['url']} field={item['field']} value={value_display!r}")
    if notice_null:
        lines.append("")
        lines.append("NOTICE")
        lines.append(f"- valid-null fields: {len(notice_null)}")
        for item in notice_null:
            lines.append(f"  - {item['field']} (total={item['total']})")
    return "\n".join(lines) + "\n", severity


def build_email_report(cfg_or_product: Any, jsonl_path: str | Path) -> tuple[str, bool]:
    body, severity = build_email_report_with_severity(cfg_or_product, jsonl_path)
    return body, severity != "ok"


def email_config_value(cfg: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = cfg.get(key)
        if value not in (None, ""):
            return value
    return default


def email_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def send_email_report(subject: str, body: str) -> tuple[bool, list[str]]:
    try:
        import config  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return False, [f"[email] skipped: config import failed: {repr(exc)}"]

    cfg = dict(getattr(config, "EMAIL_CONFIG", {}) or {})
    server = email_config_value(cfg, "smtp_server", "smtp_host", "host")
    port = int(email_config_value(cfg, "smtp_port", "port", default=587))
    sender = email_config_value(cfg, "sender_email", "from_email", "username", "user")
    password = email_config_value(cfg, "sender_password", "password")
    recipients = email_recipients(email_config_value(cfg, "receiver_email", "receiver_emails", "to_email", "to"))
    use_ssl = bool(email_config_value(cfg, "use_ssl", "smtp_ssl", default=(port == 465)))
    use_tls = bool(email_config_value(cfg, "use_tls", "starttls", default=(not use_ssl)))
    username = email_config_value(cfg, "smtp_username", "username", "user", default=sender)

    missing = [
        name
        for name, value in (
            ("smtp_server", server),
            ("sender_email", sender),
            ("receiver_email", recipients),
        )
        if not value
    ]
    if missing:
        return False, [f"[email] skipped: missing EMAIL_CONFIG keys: {', '.join(missing)}"]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = str(sender)
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(str(server), port, timeout=60) as smtp:
                if password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
        else:
            with smtplib.SMTP(str(server), port, timeout=60) as smtp:
                if use_tls:
                    smtp.starttls()
                if password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001
        return False, [f"[email] failed: {repr(exc)}"]

    return True, [f"[email] sent: {subject} -> {', '.join(recipients)}"]
