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


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def build_report(cfg, rows: list[dict]) -> tuple[str, str]:
    out = category_output_root(cfg.PRODUCT.lower())
    targets_mf = out / "step02_final_targets_manifest.json"
    db_mf = out / "step14_db_save_manifest.json"
    targets = read_json(targets_mf) if targets_mf.exists() else {}
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
        issues.append(f"타깃 {targets.get('main_target_shortfall')}건 부족")
    if main_present != main_expected:
        issues.append(f"main_rank {main_present}/{main_expected}")
    if bsr_present != bsr_expected:
        issues.append(f"bsr_rank {bsr_present}/{bsr_expected}")
    if db.get("dry_run") is False and db.get("inserted", 0) != total:
        issues.append(f"DB 적재 {db.get('inserted', 0)}/{total}")
    elif db.get("dry_run"):
        issues.append("DB 미적재(dry-run/테이블 없음)")

    subject = f"[SEG] OTTO {cfg.PRODUCT} crawled"
    lines = [
        subject, "",
        f"총 수집 {total} sku", "",
        "랭크 수집 현황",
        f"  main_rank - {main_present}/{main_expected}",
        f"  bsr_rank - {bsr_present}/{bsr_expected}", "",
        "전체 null 현황",
        *([f"  {f}" for f in null_fields] if null_fields else ["  없음"]), "",
        ("특이사항 없음" if not issues else "특이사항\n" + "\n".join(f"  - {i}" for i in issues)),
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
