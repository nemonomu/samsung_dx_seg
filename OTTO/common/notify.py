"""Step15 (shared): build the concise crawl report per category and optionally email it."""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from common.io_util import category_output_root, env_value, read_csv, read_json, write_json

NULL_BASE = [
    "item", "product_url", "retailer_sku_name", "final_sku_price", "original_sku_price",
    "savings", "sku_popularity", "sku_status", "discount_type", "delivery_availability", "sku",
]
NULL_TAIL = [
    "retailer_sku_name_similar", "star_rating", "count_of_star_ratings", "count_of_reviews",
    "recommendation_intent", "summarized_review_content", "detailed_review_content",
]
SUMMARY_UI_TEXT = (
    "Das sagen unsere Kunden",
    "Bewertungen ansehen",
    "Ist diese Zusammenfassung hilfreich?",
    "Nicht hilfreich",
)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def build_report(cfg, rows: list[dict]) -> tuple[str, str]:
    out = category_output_root(cfg.PRODUCT.lower())
    targets_mf = out / "step02_final_targets_manifest.json"
    full_mf = out / "step09_full_output_manifest.json"
    db_mf = out / "step14_db_save_manifest.json"
    targets = read_json(targets_mf) if targets_mf.exists() else {}
    full = read_json(full_mf) if full_mf.exists() else {}
    db = read_json(db_mf) if db_mf.exists() else {}
    total = len(rows)
    main_expected = targets.get("main_target_unique", 300)
    bsr_expected = targets.get("bsr_rank_limit", 100)
    main_present = sum(1 for r in rows if (r.get("main_rank") or "").strip())
    bsr_present = sum(1 for r in rows if (r.get("bsr_rank") or "").strip())

    null_fields_check = NULL_BASE + list(cfg.SPEC_FIELDS) + NULL_TAIL
    null_fields = [f for f in null_fields_check if not any((r.get(f) or "").strip() for r in rows)]

    issues = []
    if targets.get("main_target_shortfall"):
        issues.append(f"메인 수집 대상 부족 {targets.get('main_target_shortfall')}")
    if main_present != main_expected:
        issues.append(f"main_rank 수집 {main_present}/{main_expected}")
    if bsr_present != bsr_expected:
        issues.append(f"bsr_rank 수집 {bsr_present}/{bsr_expected}")
    spec_missing_counts = full.get("missing_spec_counts") or {
        f: sum(1 for r in rows if not (r.get(f) or "").strip()) for f in cfg.SPEC_FIELDS
    }
    for field, count in spec_missing_counts.items():
        if count:
            issues.append(f"{field} NULL {count}/{total}")
    summary_qa = full.get("summary_qa") or {}
    summary_checked = int(summary_qa.get("checked") or 0)
    summary_eligible = int(summary_qa.get("eligible") or 0)
    summary_rendered = int(summary_qa.get("rendered") or 0)
    summary_eligible_missing = int(summary_qa.get("eligible_missing") or 0)
    summary_no_source = int(summary_qa.get("no_source") or 0)
    summary_selector_mismatch = int(summary_qa.get("selector_mismatch") or 0)
    summary_http_failed = int(summary_qa.get("http_failed") or 0)
    summary_ui_polluted = max(int(summary_qa.get("ui_text_contamination") or 0), sum(
        1 for row in rows
        if any(text in str(row.get("summarized_review_content") or "") for text in SUMMARY_UI_TEXT)
    ))
    if summary_eligible_missing:
        issues.append(
            f"summarized_review_content 요약 대상 중 미수집 "
            f"{summary_eligible_missing}/{summary_eligible}"
        )
    if summary_ui_polluted:
        issues.append(f"summarized_review_content UI 문구 혼입 {summary_ui_polluted}/{total}")
    if summary_selector_mismatch:
        issues.append(f"summarized_review_content 선택자 불일치 {summary_selector_mismatch}")
    if summary_http_failed:
        issues.append(f"summarized_review_content HTTP 실패 {summary_http_failed}")
    if db.get("success") is False:
        issues.append(f"DB 오류: {db.get('reason') or db.get('blocked_reason') or '원인 불명'}")
    if db.get("dry_run") is False and db.get("inserted", 0) != total:
        issues.append(f"DB 저장 {db.get('inserted', 0)}/{total}")
    elif db.get("dry_run"):
        issues.append("DB 시험 실행으로 저장 생략")

    base_subject = f"[SEG] OTTO {cfg.PRODUCT} 수집 완료"
    subject = base_subject if not issues else f"[확인필요] {base_subject}"
    lines = [
        subject, "",
        f"총 수집: {total}개 SKU", "",
        "순위 수집 현황",
        f"  main_rank - {main_present}/{main_expected}",
        f"  bsr_rank - {bsr_present}/{bsr_expected}", "",
        "전체 NULL 필드",
        *([f"  {f}" for f in null_fields] if null_fields else ["  없음"]), "",
        "리뷰 요약 수집 현황 (메인 TOP 20)",
        f"  확인 대상 - {summary_checked}",
        f"  요약 대상 - {summary_eligible}",
        f"  수집 완료 - {summary_rendered}",
        f"  요약 대상 중 미수집 - {summary_eligible_missing}",
        f"  요약 없음 - {summary_no_source}",
        f"  선택자 불일치 - {summary_selector_mismatch}",
        f"  HTTP 실패 - {summary_http_failed}",
        f"  UI 문구 혼입 - {summary_ui_polluted}", "",
        ("이상 없음" if not issues else "확인 필요\n" + "\n".join(f"  - {i}" for i in issues)),
    ]
    return subject, "\n".join(lines) + "\n"


def _send(subject: str, body: str) -> tuple[bool, int, str | None]:
    server = env_value("SEG_SMTP_SERVER")
    port = int(env_value("SEG_SMTP_PORT", "587") or "587")
    sender = env_value("SEG_EMAIL_FROM")
    password = env_value("SEG_EMAIL_PASSWORD")
    recipients = [a.strip() for a in re.split(r"[,;]", env_value("SEG_EMAIL_TO", "") or "") if a.strip()]
    if not (server and sender and password and recipients):
        return False, len(recipients), "missing SMTP settings"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(server, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(sender, password)
            s.send_message(msg)
        return True, len(recipients), None
    except Exception as exc:  # noqa: BLE001
        return False, len(recipients), type(exc).__name__ + ": " + str(exc)


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT.lower())
    full = out / "otto_full_output.csv"
    rows = read_csv(full) if full.exists() else []
    subject, report = build_report(cfg, rows)
    (out / "otto_email_report.txt").write_text(report, encoding="utf-8")

    notify = _truthy(env_value("SEG_EMAIL_NOTIFY", "0"))
    dry = _truthy(env_value("SEG_EMAIL_DRY_RUN", "0"))
    sent, n_to, error = (False, 0, None)
    if notify and not dry:
        sent, n_to, error = _send(subject, report)
    manifest = {"run_type": "email_notify", "product": cfg.PRODUCT, "notify": notify, "dry_run": dry,
                "sent": sent, "recipients_count": n_to, "error": error, "report": str(out / "otto_email_report.txt")}
    write_json(out / "step15_email_notify_manifest.json", manifest)
    print(f"[notify/{cfg.PRODUCT}] sent={sent} dry_run={dry} error={error}")
    return manifest
