"""DRY completion report using the existing SEG mail settings and SMTP sender.

Only this module owns DRY notifications; the integrated runner never sends mail.
No raw SMTP errors, addresses or authentication settings enter logs/manifests.
"""
from __future__ import annotations

from pathlib import Path

from common.notify import _send, _truthy, env_value
from dry.events import context, emit, quiet_external
from dry.output import write_manifest as write_json


def send_error_code(error) -> str:
    """Whitelist SMTP failure categories; never return any original error text."""
    if error == "missing SMTP settings":
        return "SMTP_CONFIGURATION_MISSING"
    if isinstance(error, str):
        for name, code in (("SMTPAuthenticationError", "SMTP_AUTHENTICATION_FAILED"),
                           ("SMTPRecipientsRefused", "SMTP_RECIPIENTS_REFUSED"),
                           ("SMTPServerDisconnected", "SMTP_DISCONNECTED"),
                           ("SMTPConnectError", "SMTP_CONNECTION_FAILED"),
                           ("TimeoutError", "SMTP_TIMEOUT")):
            if error.startswith(name + ":"):
                return code
    return "SMTP_SEND_FAILED"


def build_report(manifest, table, *, success, error=None, elapsed_s=None):
    subject = "[SEG] MediaMarkt DRY crawled" if success else "[CHECK] [SEG] MediaMarkt DRY failed"
    lines = [subject, "", f"Batch: {manifest.get('batch_id') or 'not completed'}",
             f"Table: {'.'.join(table)}", f"Collected unique products: {manifest.get('output_rows', 0)}"]
    for sort in ("main", "bsr"):
        listing = manifest.get(sort) or {}
        lines.append(f"{sort}: {listing.get('rows', 0)}/{(manifest.get('targets') or {}).get(sort, '?')}"
                     f" ({listing.get('stop_reason') or 'not completed'})")
    timing = manifest.get("timing") or {}
    lines.extend([f"Collection seconds: {timing.get('collection_elapsed_s', 'not available')}",
                  f"Collection completed: {timing.get('collection_completed_at', 'not completed')}",
                  f"Current run seconds through DB: {elapsed_s}"])
    database = manifest.get("database") or {}
    lines.extend([f"DB state: {database.get('state') or 'not completed'}",
                  f"DB confirmed: {database.get('db_written')}",
                  f"Inserted: {database.get('inserted', 0)}",
                  f"Already loaded: {bool(database.get('already_loaded'))}"])
    if database.get("backfilled"):
        lines.append(f"Backfilled: {database['backfilled']}")
    if error:
        lines.append(f"Failure code: {error}")  # caller supplies a static code only
    return subject, "\n".join(lines) + "\n"


def notify_run(output_dir: Path, manifest: dict, table, *, collection_success: bool,
               db_written: bool | None, error=None, elapsed_s=None) -> dict:
    """Confirmed success gets a success subject; all other outcomes get CHECK.

    A persisted sent marker avoids duplicate mail on a load-only retry of the
    same batch/table. A crash during sending requires review instead of an
    automatic resend. CSV integrity and the database result remain independent.
    """
    table_name = ".".join(table)
    context("notification", batch_id=manifest.get("batch_id"), table=table_name)
    database = manifest.get("database") or {}
    success = (collection_success and manifest.get("success") is True and db_written is True
               and database.get("state") == "success" and database.get("db_written") is True
               and database.get("table") == table_name and error is None)
    previous = (manifest.get("notifications") or {}).get(table_name) or manifest.get("notification") or {}
    same = (previous.get("batch_id") == manifest.get("batch_id")
            and previous.get("table") == table_name and previous.get("success_report") == success)
    if same and (previous.get("sent") is True or previous.get("state") == "sending"):
        state = "already_sent" if previous.get("sent") is True else "previous_send_unknown"
        result = {**previous, "state": state, "failed": state == "previous_send_unknown"}
        emit("mail_complete", **result)
        return result

    subject, report = build_report(manifest, table, success=success, error=error, elapsed_s=elapsed_s)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "mmkt_dry_email_report.txt").write_text(report, encoding="utf-8")
    with quiet_external():
        enabled = _truthy(env_value("SEG_EMAIL_NOTIFY", "0"))
        dry_run = _truthy(env_value("SEG_EMAIL_DRY_RUN", "0"))
    result = {"batch_id": manifest.get("batch_id"), "table": table_name, "success_report": success,
              "enabled": enabled, "dry_run": dry_run, "sent": False, "failed": False,
              "state": "disabled" if not enabled else "dry_run" if dry_run else "sending"}

    def save():
        manifest["notification"] = dict(result)
        manifest.setdefault("notifications", {})[table_name] = dict(result)
        write_json(output_dir / "mmkt_dry_manifest.json", manifest)

    save()  # fail before SMTP if the status cannot be persisted
    if enabled and not dry_run:
        emit("mail_start", success_report=success)
        try:
            with quiet_external():
                sent, recipients, send_error = _send(subject, report)
            # The shared sender returns raw errors: deliberately never retain them.
            result.update(sent=bool(sent), failed=not sent,
                          state="sent" if sent else "failed", recipients_count=recipients,
                          error=None if sent else send_error_code(send_error))
        except Exception as exc:
            result.update(failed=True, state="failed", error="SMTP_SEND_FAILED", error_type=type(exc).__name__)
        try:
            save()
        except Exception as exc:
            # Sent may be True. Don't turn a persistence error into another send.
            result.update(failed=True, error="MAIL_STATUS_SAVE_FAILED", error_type=type(exc).__name__)
    emit("mail_complete", **result)
    return result
