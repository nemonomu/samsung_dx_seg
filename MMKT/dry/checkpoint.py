"""Batch-scoped, non-loadable snapshots of public output fields and progress."""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime

from dry.detail import CollectionError
from dry.events import emit, failure
from dry.output import write_manifest


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
