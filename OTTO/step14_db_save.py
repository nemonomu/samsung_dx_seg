"""Step14: DB save dry-run for OTTO outputs."""
from __future__ import annotations

from step00_config import OUTPUT_ROOT, read_csv, read_json, write_json

FINAL_TARGETS = OUTPUT_ROOT / "otto_final_targets.csv"
DETAIL_SUMMARY = OUTPUT_ROOT / "otto_detail_probe_summary.json"
COMPARE_SUMMARY = OUTPUT_ROOT / "otto_compare_probe_summary.json"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step14_db_save_manifest.json"


def main() -> int:
    final_rows = read_csv(FINAL_TARGETS) if FINAL_TARGETS.exists() else []
    detail = read_json(DETAIL_SUMMARY) if DETAIL_SUMMARY.exists() else {}
    compare = read_json(COMPARE_SUMMARY) if COMPARE_SUMMARY.exists() else {}
    manifest = {
        "run_type": "step14_db_save_dry_run",
        "dry_run": True,
        "reason": "DB connection and target table contract are not configured yet.",
        "planned_input_rows": len(final_rows),
        "planned_detail_available": bool(detail),
        "planned_compare_available": bool(compare),
        "unique_key": "variation_id",
        "required_before_live_insert": [
            "confirm final DB table names",
            "confirm full field mapping from Deloitte workbook",
            "validate review 20 collection source",
            "run insert with transaction and row-count manifest",
        ],
    }
    write_json(MANIFEST_OUTPUT, manifest)
    print(f"[step14] dry_run final_rows={len(final_rows)} manifest={MANIFEST_OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
