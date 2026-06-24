"""Load the Deloitte workbook-derived schema snapshot for OTTO planning."""
from __future__ import annotations

from step00_config import SCHEMA_ROOT, write_json

SCHEMA_JSON = SCHEMA_ROOT / "deloitte_schema_rows.json"
OUTPUT_JSON = SCHEMA_ROOT / "otto_schema_rows.json"


def load_schema_rows() -> list[dict]:
    import json

    if not SCHEMA_JSON.exists():
        return []
    rows = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    otto_rows = []
    for row in rows:
        source = str(row.get("Data Source") or "").lower()
        if source == "otto":
            otto_rows.append(row)
    return otto_rows or rows


def main() -> int:
    rows = load_schema_rows()
    write_json(OUTPUT_JSON, {"rows": rows, "row_count": len(rows)})
    print(f"[schema] rows={len(rows)} output={OUTPUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
