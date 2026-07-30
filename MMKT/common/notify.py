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


# A row "got its PDP detail" if a comparison/PDP-only field came back. Ratings
# are deliberately excluded because step09 may recover them from listing JSON-LD;
# counting those would hide a Cloudflare-blocked detail run.
def _detail_present(r: dict, spec_fields: list[str]) -> bool:
    keys = ["sku", "delivery_availability", *spec_fields]
    return any((r.get(k) or "").strip() for k in keys)


def build_report(cfg, rows: list[dict]) -> tuple[str, str]:
    out = cfg.OUTPUT_ROOT
    step02 = _read_json(out / "mmkt_step02_pdp_detail_manifest.json")
    full = _read_json(out / "step09_full_output_manifest.json")
    db = _read_json(out / "step14_db_save_manifest.json")
    total = len(rows)
    main_expected = cfg.MAIN_TARGET_UNIQUE
    bsr_expected = cfg.BSR_TARGET_RANK
    main_present = sum(1 for r in rows if (r.get("main_rank") or "").strip())
    bsr_present = sum(1 for r in rows if (r.get("bsr_rank") or "").strip())

    spec_fields = list(cfg.SPEC_FIELDS)
    detail_present = sum(1 for r in rows if _detail_present(r, spec_fields))
    detail_ratio = (detail_present / total) if total else 0.0
    min_ratio = float(env_value("SEG_DETAIL_MIN_RATIO", "0.90") or "0.90")

    null_fields_check = NULL_BASE + spec_fields + NULL_TAIL
    null_fields = [f for f in null_fields_check if not any((r.get(f) or "").strip() for r in rows)]

    issues = []
    if main_present != main_expected:
        issues.append(f"main_rank {main_present}/{main_expected}")
    if bsr_present != bsr_expected:
        issues.append(f"bsr_rank {bsr_present}/{bsr_expected}")
    if total and detail_ratio < min_ratio:
        issues.append(f"detail collection low {detail_present}/{total} ({detail_ratio:.0%})")
    for source, mf in (("step02", step02), ("full", full)):
        missing_primary = int(mf.get("rows_missing_primary_spec") or 0)
        if missing_primary:
            issues.append(f"{source} primary spec NULL {missing_primary}/{total}")
        fetch_errors = int(mf.get("rows_with_fetch_error") or 0)
        if fetch_errors:
            issues.append(f"{source} fetch_error {fetch_errors}/{total}")
    for field, count in (full.get("spec_missing_counts") or {}).items():
        if count:
            issues.append(f"{field} NULL {count}/{total}")
    if db.get("success") is False:
        issues.append(f"DB issue: {db.get('reason') or 'unknown'}")
    if db.get("dry_run") is False and db.get("inserted", 0) != total:
        issues.append(f"DB inserted {db.get('inserted', 0)}/{total}")
    elif db.get("dry_run"):
        issues.append("DB dry-run/skipped")

    base_subject = f"[SEG] MediaMarkt {cfg.PRODUCT} crawled"
    subject = base_subject if not issues else f"[CHECK] {base_subject}"
    lines = [
        subject, "",
        f"Total collected: {total} sku", "",
        "Rank coverage",
        f"  main_rank - {main_present}/{main_expected}",
        f"  bsr_rank - {bsr_present}/{bsr_expected}",
        f"  detail(PDP) - {detail_present}/{total} ({detail_ratio:.0%})", "",
        "All-null fields",
        *([f"  {f}" for f in null_fields] if null_fields else ["  none"]), "",
        ("Issues: none" if not issues else "Issues\n" + "\n".join(f"  - {i}" for i in issues)),
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
