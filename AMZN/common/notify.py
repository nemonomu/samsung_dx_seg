"""Step15: build/send SIEL-style Amazon crawl report."""
from __future__ import annotations

import re
import smtplib
import sys
import ssl
from email.message import EmailMessage
from typing import Any

from common import email_report, siel_logging as siel_log
from common.io_util import category_output_root, env_value, read_json, truthy, write_json


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
        with smtplib.SMTP(server, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(sender, password)
            smtp.send_message(msg)
        return True, len(recipients), None
    except Exception as exc:  # noqa: BLE001
        return False, len(recipients), type(exc).__name__ + ": " + str(exc)


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT)
    run_manifest_path = out / "step00_run_manifest.json"
    run_manifest = read_json(run_manifest_path) if run_manifest_path.exists() else {}
    jsonl_path = run_manifest.get("jsonl_path")
    if jsonl_path:
        body, severity = email_report.build_email_report_with_severity(cfg, jsonl_path)
    else:
        severity = "sos"
        body = f"product: {cfg.PRODUCT}\nSOS\n- missing run manifest/jsonl path\n"
    subject = f"[SEG] Amazon.de {cfg.PRODUCT} crawling report"
    if severity == "sos":
        subject = "SOS " + subject
    elif severity == "warning":
        subject = "WARNING " + subject
    report_path = out / "amzn_email_report.txt"
    report_path.write_text(body, encoding="utf-8")
    notify = truthy(env_value("SEG_EMAIL_NOTIFY", "0"))
    dry = truthy(env_value("SEG_EMAIL_DRY_RUN", "0"))
    sent, count, error = (False, 0, None)
    if notify and not dry:
        sent, count, error = _send(subject, body)
    siel_log.run_log(f"email_report severity={severity} subject={subject}")
    if sent:
        email_line = f"[email] sent: {subject} -> recipients={count}"
        email_level = "INFO"
    elif notify and not dry:
        email_line = f"[email] failed: {error}"
        email_level = "ERROR"
    else:
        reason = "dry_run" if dry else "SEG_EMAIL_NOTIFY=0"
        email_line = f"[email] skipped: {reason}"
        email_level = "INFO"
    print(email_line, file=sys.stderr)
    siel_log.run_log(email_line, email_level)
    manifest = {
        "run_type": "email_notify",
        "product": cfg.PRODUCT,
        "severity": severity,
        "notify": notify,
        "dry_run": dry,
        "sent": sent,
        "recipients_count": count,
        "error": error,
        "report": str(report_path),
        "jsonl_path": jsonl_path,
    }
    write_json(out / "step15_email_notify_manifest.json", manifest)
    print(f"[notify/{cfg.PRODUCT}] severity={severity} sent={sent} dry_run={dry} error={error}", file=sys.stderr)
    return manifest
