"""Step00 (shared): load the Deloitte schema rows for this category (informational)."""
from __future__ import annotations

import json
from typing import Any

from common.io_util import SCHEMA_ROOT, category_output_root, write_json


def run(cfg) -> dict[str, Any]:
    src = SCHEMA_ROOT / "deloitte_schema_rows.json"
    rows: list[dict] = []
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
        for row in data:
            source = str(row.get("Data Source") or "").lower()
            product = str(row.get("제품군") or row.get("상품군") or "").upper()
            if source == "otto" and (not product or product == cfg.PRODUCT):
                rows.append(row)
    out = category_output_root(cfg.PRODUCT.lower())
    write_json(out / "otto_schema_rows.json", {"product": cfg.PRODUCT, "rows": rows, "row_count": len(rows)})
    print(f"[schema/{cfg.PRODUCT}] rows={len(rows)}")
    return {"run_type": "schema", "product": cfg.PRODUCT, "rows": len(rows)}
