"""Batch-scoped, non-loadable snapshots of public output fields and progress."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from dry.detail import CollectionError
from dry.events import emit, failure
from dry.output import write_manifest


def read_journal(path, *, batch=None, sort=None):
    """Inspect committed newline-delimited records; never resume or load a DB.

    The newline is the commit marker. A torn last write is reported separately
    and never included, even if its JSON happens to parse. Do not skip over a
    broken interior record and pretend that the remainder is a complete run.
    """
    records, issue = [], None
    with Path(path).open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                issue = "incomplete_final_record"
                break
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                issue = "invalid_record"
                break
            if (not isinstance(value, dict) or value.get("artifact_kind") != "intermediate"
                    or value.get("success") is not False or value.get("loadable") is not False
                    or type(value.get("sequence")) is not int or value["sequence"] != len(records) + 1
                    or not isinstance(value.get("kind"), str)
                    or value.get("kind") not in {"listing", "detail", "tail_read", "phase", "failure"}):
                issue = "invalid_record_contract"
                break
            batch = batch or value.get("batch_id")
            sort = sort or value.get("sort")
            if (not isinstance(batch, str) or not batch or not isinstance(sort, str)
                    or sort not in {"main", "bsr"}
                    or (value.get("batch_id"), value.get("sort")) != (batch, sort)):
                issue = "record_scope_mismatch"
                break
            records.append(value)
    return {"records": records, "issue": issue, "complete": issue is None,
            "artifact_kind": "intermediate", "loadable": False}


class IntermediateJournal:
    """Append only new public results; full snapshots belong at phase boundaries.

    Opening/closing each line flushes completed writes before the next product.
    This preserves records on a process crash, not a guarantee against disk or
    machine failure. Existing batches are never silently appended or overwritten.
    """
    def __init__(self, output_dir, manifest, sort):
        batch = manifest["batch_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", batch) or sort not in {"main", "bsr"}:
            raise CollectionError("INTERMEDIATE_BATCH_INVALID")
        self.path = output_dir / "intermediate" / batch / f"{sort}_journal.jsonl"
        self.manifest, self.batch, self.sort = manifest, batch, sort
        self.records, self.bytes, self.failed = 0, 0, False

    def _write_line(self, line):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb" if self.records == 0 else "ab") as handle:
            if handle.write(line) != len(line):
                raise OSError("INCOMPLETE_JOURNAL_WRITE")
            handle.flush()

    def _failure(self, exc):
        self.failed = True
        state = {"state": "failed", "path": str(self.path), "records": self.records,
                 "bytes": self.bytes, "error": "INTERMEDIATE_SAVE_FAILED", "error_type": type(exc).__name__}
        self.manifest.setdefault("intermediate_journals", {})[self.sort] = state
        previous = self.manifest.get("intermediate") or {}
        self.manifest["intermediate"] = {**previous, "state": "failed", "error": "INTERMEDIATE_SAVE_FAILED",
                                         "failed_path": str(self.path)}
        failure(exc, code="INTERMEDIATE_SAVE_FAILED")

    def append(self, kind, **fields):
        if self.failed or kind not in {"listing", "detail", "tail_read", "phase", "failure"}:
            raise CollectionError("INTERMEDIATE_SAVE_FAILED")
        record = {**fields, "artifact_kind": "intermediate", "success": False, "loadable": False,
                  "batch_id": self.batch, "sort": self.sort, "sequence": self.records + 1, "kind": kind}
        try:
            line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self._write_line(line)
        except BaseException as exc:
            self._failure(exc)
            if not isinstance(exc, Exception):
                raise
            raise CollectionError("INTERMEDIATE_SAVE_FAILED") from None
        self.records += 1
        self.bytes += len(line)
        self.manifest.setdefault("intermediate_journals", {})[self.sort] = {
            "state": "saved", "path": str(self.path), "records": self.records, "bytes": self.bytes}
        self.manifest["intermediate"] = {"state": "saved", "last_path": str(self.path),
            "format": "jsonl", "rows": self.manifest.get("output_rows", 0),
            "saved_at": datetime.now().isoformat(timespec="seconds")}
        emit("intermediate_appended", path=str(self.path), sort=self.sort, phase=kind,
             page=fields.get("page"), item=(fields.get("result") or {}).get("item"),
             records=self.records, bytes=self.bytes, loadable=False)

    def verify(self):
        try:
            result = read_journal(self.path, batch=self.batch, sort=self.sort)
            if result["issue"] or len(result["records"]) != self.records:
                raise CollectionError("INTERMEDIATE_JOURNAL_INVALID")
        except Exception as exc:
            self._failure(exc)
            raise CollectionError("INTERMEDIATE_SAVE_FAILED") from None


def save_checkpoint(output_dir, manifest, rows, *, sort, page, attempt, phase="listing", sequence=None,
                    candidates=None, exclusions=None, detail_results=None):
    batch = manifest["batch_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", batch):
        raise CollectionError("INTERMEDIATE_BATCH_INVALID")
    if phase == "listing":
        name = f"{sort}_{page:04d}_attempt_{attempt:02d}.json"
    elif phase == "candidates":
        name = f"{sort}_candidates_{page:04d}_attempt_{attempt:02d}.json"
    elif phase == "detail" and isinstance(sequence, int) and sequence >= 0:
        name = f"{sort}_detail_{sequence:04d}.json"
    elif phase in {"tail_read", "tail_result"} and isinstance(sequence, int) and sequence > 0:
        name = f"{sort}_{phase}_{sequence:02d}_page_{page:04d}.json"
    elif phase == "failure":
        name = f"{sort}_failure_{page:04d}.json"
    else:
        raise CollectionError("INTERMEDIATE_PHASE_INVALID")
    path = output_dir / "intermediate" / batch / name
    previous = manifest.get("intermediate") or {}
    saved = {"state": "saved", "last_path": str(path), "rows": len(rows),
             "saved_at": datetime.now().isoformat(timespec="seconds")}
    if candidates is not None:
        saved["candidate_count"] = len(candidates)
    progress = copy.deepcopy(manifest)
    progress["intermediate"] = saved
    payload = {"artifact_kind": "intermediate", "success": False, "batch_id": batch,
               "loadable": False, "phase": phase, "sort": sort, "rows": rows, "progress": progress}
    if candidates is not None:
        payload["candidates"] = candidates
    if exclusions is not None:
        payload["exclusions"] = exclusions
    if detail_results is not None:
        payload["detail_results"] = detail_results
    try:
        if candidates is not None:
            validate_candidates(candidates, sort)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.with_suffix(path.suffix + ".tmp").exists():
            raise CollectionError("INTERMEDIATE_SNAPSHOT_EXISTS")
        write_manifest(path, payload)
        # Candidates authorize a later API phase only after the saved artifact
        # has been read back. No raw HTML or API responses enter this contract.
        if candidates is not None:
            saved_payload = json.loads(path.read_text(encoding="utf-8"))
            if (saved_payload.get("artifact_kind") != "intermediate"
                    or saved_payload.get("loadable") is not False
                    or saved_payload.get("batch_id") != batch or saved_payload.get("sort") != sort
                    or saved_payload.get("candidates") != candidates):
                raise CollectionError("CANDIDATE_SAVE_INVALID")
    except Exception as exc:
        manifest["intermediate"] = {**previous, "state": "failed", "error": "INTERMEDIATE_SAVE_FAILED",
                                    "error_type": type(exc).__name__, "failed_path": str(path)}
        failure(exc, code="INTERMEDIATE_SAVE_FAILED")
        raise CollectionError("INTERMEDIATE_SAVE_FAILED") from None
    manifest["intermediate"] = saved
    emit("intermediate_saved", path=str(path), rows=len(rows), sort=sort, page=page, attempt=attempt, phase=phase,
         loadable=False)
    return path


def validate_candidates(rows, sort):
    from dry.output import canonical_product_url
    seen, previous = set(), (0, 0)
    for row in rows:
        item = row.get("sku_id")
        order = tuple(row.get("listing_order") or ())
        if (not item or item in seen or row.get("listing_sort") != sort
                or len(order) != 2 or not all(isinstance(v, int) and v > 0 for v in order)
                or order <= previous or row.get("page") != order[0] or not row.get("observed_at")):
            raise CollectionError("CANDIDATE_INVALID")
        canonical_product_url(row.get("product_url"), item)
        seen.add(item)
        previous = order
