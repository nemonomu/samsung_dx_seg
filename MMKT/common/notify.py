"""Step15 (shared): build the per-product crawl report and optionally email it.

Mirrors OTTO common/notify.py. Reads mmkt_full_output.csv + the step14 manifest,
summarizes rank coverage + null fields + issues, writes mmkt_email_report.txt, and
emails it via the SEG_* SMTP settings when SEG_EMAIL_NOTIFY is truthy (and not
SEG_EMAIL_DRY_RUN). Product-aware via cfg.SPEC_FIELDS / cfg.OUTPUT_ROOT.

  python -m common.notify --product tv
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from common.config import ACCOUNT_NAME, env_value, read_csv, write_json

# Fields whose all-null state is worth flagging (MMKT set — excludes OTTO-only
# sku_popularity / recommendation_intent which MMKT never collects).
NULL_BASE = [
    "item", "product_url", "retailer_sku_name", "final_sku_price", "original_sku_price",
    "savings", "sku_status", "discount_type",
    "delivery_availability", "pick_up_availability", "sku",
]
NULL_TAIL = [
    "retailer_sku_name_similar", "star_rating", "count_of_star_ratings",
    "count_of_reviews", "summarized_review_content", "detailed_review_content",
]


def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_report(cfg, rows: list[dict]) -> tuple[str, str]:
    out = cfg.OUTPUT_ROOT
    db = _read_json(out / "step14_db_save_manifest.json")
    total = len(rows)
    main_expected = cfg.MAIN_TARGET_UNIQUE
    bsr_expected = cfg.BSR_TARGET_RANK
    main_present = sum(1 for r in rows if (r.get("main_rank") or "").strip())
    bsr_present = sum(1 for r in rows if (r.get("bsr_rank") or "").strip())

    null_fields_check = NULL_BASE + list(cfg.SPEC_FIELDS) + NULL_TAIL
    null_fields = [f for f in null_fields_check if not any((r.get(f) or "").strip() for r in rows)]

    issues = []
    if main_present != main_expected:
        issues.append(f"main_rank {main_present}/{main_expected}")
    if bsr_present != bsr_expected:
        issues.append(f"bsr_rank {bsr_present}/{bsr_expected}")
    if db.get("dry_run") is False and db.get("inserted", 0) != total:
        issues.append(f"DB 적재 {db.get('inserted', 0)}/{total}")
    elif db.get("dry_run"):
        issues.append("DB 미적재(dry-run/테이블 없음)")

    subject = f"[SEG] MediaMarkt {cfg.PRODUCT} crawled"
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
    except Exception as exc:
        return False, len(recipients), type(exc).__name__ + ": " + str(exc)


def run(cfg) -> dict[str, Any]:
    out = cfg.OUTPUT_ROOT
    rows = read_csv(out / "mmkt_full_output.csv")
    subject, report = build_report(cfg, rows)
    (out / "mmkt_email_report.txt").write_text(report, encoding="utf-8")

    notify = _truthy(env_value("SEG_EMAIL_NOTIFY", "0"))
    dry = _truthy(env_value("SEG_EMAIL_DRY_RUN", "0"))
    sent, n_to, error = (False, 0, None)
    if notify and not dry:
        sent, n_to, error = _send(subject, report)
    manifest = {"run_type": "email_notify", "product": cfg.PRODUCT, "account_name": ACCOUNT_NAME,
                "notify": notify, "dry_run": dry, "sent": sent, "recipients_count": n_to,
                "error": error, "report": str(out / "mmkt_email_report.txt")}
    write_json(out / "step15_email_notify_manifest.json", manifest)
    print(f"[notify/{cfg.PRODUCT}] sent={sent} dry_run={dry} recipients={n_to} error={error}")
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description="Email the MMKT crawl report for a product.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    args = p.parse_args()
    run(load_cfg(args.product))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
