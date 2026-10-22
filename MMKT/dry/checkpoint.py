"""Batch-scoped, non-loadable snapshots of public output fields and progress."""
from __future__ import annotations

import copy
import re
from datetime import datetime

from dry.detail import CollectionError
from dry.events import emit, failure
from dry.output import write_manifest


def save_checkpoint(output_dir, manifest, rows, *, sort, page, attempt):
    batch = manifest["batch_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", batch):
        raise CollectionError("INTERMEDIATE_BATCH_INVALID")
    path = output_dir / "intermediate" / batch / f"{sort}_{page:04d}_attempt_{attempt:02d}.json"
    previous = manifest.get("intermediate") or {}
    saved = {"state": "saved", "last_path": str(path), "rows": len(rows),
             "saved_at": datetime.now().isoformat(timespec="seconds")}
    progress = copy.deepcopy(manifest)
    progress["intermediate"] = saved
    payload = {"artifact_kind": "intermediate", "success": False, "batch_id": batch,
               "loadable": False, "rows": rows, "progress": progress}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_manifest(path, payload)
    except Exception as exc:
        manifest["intermediate"] = {**previous, "state": "failed", "error": "INTERMEDIATE_SAVE_FAILED",
                                    "error_type": type(exc).__name__, "failed_path": str(path)}
        failure(exc, code="INTERMEDIATE_SAVE_FAILED")
        raise CollectionError("INTERMEDIATE_SAVE_FAILED") from None
    manifest["intermediate"] = saved
    emit("intermediate_saved", path=str(path), rows=len(rows), sort=sort, page=page, attempt=attempt,
         loadable=False)
