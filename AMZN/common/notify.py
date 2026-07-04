"""Step15: build/send SIEL-style Amazon crawl report."""
from __future__ import annotations

import os
import sys
from typing import Any

from common import email_report, siel_logging as siel_log
from common.io_util import category_output_root, read_json, write_json


def _email_error(lines: list[str], sent: bool) -> str | None:
    if sent:
        return None
    for line in lines:
        low = line.lower()
        if "failed" in low or "skipped" in low:
            return line
    return None


def run(cfg) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT)
    run_manifest_path = out / "step00_run_manifest.json"
    run_manifest = read_json(run_manifest_path) if run_manifest_path.exists() else {}
    jsonl_path = run_manifest.get("jsonl_path")
    if not jsonl_path:
        line = "[email] skipped: missing run manifest/jsonl path"
        print(line, file=sys.stderr)
        siel_log.run_log(line, "ERROR")
        manifest = {
            "run_type": "email_notify",
            "product": cfg.PRODUCT,
            "severity": "sos",
            "sent": False,
            "error": line,
            "jsonl_path": jsonl_path,
        }
        write_json(out / "step15_email_notify_manifest.json", manifest)
        return manifest

    try:
        body, severity = email_report.build_email_report_with_severity(cfg, jsonl_path)
    except TypeError:
        body, severity = email_report.build_email_report_with_severity(cfg.PRODUCT, jsonl_path)

    report_path = os.path.splitext(str(jsonl_path))[0] + ".email.txt"
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        print(f"[run.py] email_report write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        siel_log.run_log(f"email_report write failed: {type(exc).__name__}: {exc}", "ERROR")

    subject = f"[SEG] AMZN {str(cfg.PRODUCT).upper()} crawling report"
    if severity == "sos":
        subject = "SOS " + subject
    elif severity == "warning":
        subject = "WARNING " + subject
    siel_log.run_log(f"email_report severity={severity} subject={subject}")

    sent, email_lines = email_report.send_email_report(subject, body)
    for line in email_lines:
        print(line, file=sys.stderr)
        siel_log.run_log(line, "ERROR" if "failed" in line.lower() else "INFO")

    error = _email_error(email_lines, sent)
    manifest = {
        "run_type": "email_notify",
        "product": cfg.PRODUCT,
        "severity": severity,
        "sent": sent,
        "error": error,
        "subject": subject,
        "email_lines": email_lines,
        "report": report_path,
        "jsonl_path": jsonl_path,
    }
    write_json(out / "step15_email_notify_manifest.json", manifest)
    print(f"[notify/{cfg.PRODUCT}] severity={severity} sent={sent} error={error}", file=sys.stderr)
    return manifest
