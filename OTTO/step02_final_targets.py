"""Step02: build final OTTO targets from the Topseller listing."""
from __future__ import annotations

from step00_config import BSR_TARGET_RANK, MAIN_TARGET_UNIQUE, OUTPUT_ROOT, read_csv, write_csv, write_json

INPUT_CSV = OUTPUT_ROOT / "otto_listing_topseller_rows.csv"
OUTPUT_CSV = OUTPUT_ROOT / "otto_final_targets.csv"
MANIFEST_OUTPUT = OUTPUT_ROOT / "step02_final_targets_manifest.json"


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_final_targets(rows: list[dict], main_limit: int, bsr_rank_limit: int) -> list[dict]:
    final_rows = []
    seen = set()
    sorted_rows = sorted(rows, key=lambda row: (to_int(row.get("list_position")) is None, to_int(row.get("list_position")) or 999999, to_int(row.get("row_index")) or 999999))
    for row in sorted_rows:
        variation_id = row.get("variation_id")
        if not variation_id or variation_id in seen:
            continue
        seen.add(variation_id)
        main_rank = to_int(row.get("list_position")) or to_int(row.get("row_index"))
        out = dict(row)
        out["main_rank"] = main_rank
        out["bsr_rank"] = main_rank if isinstance(main_rank, int) and main_rank <= bsr_rank_limit else ""
        out["selection_source"] = "topseller_main_bsr" if out["bsr_rank"] != "" else "topseller_main"
        final_rows.append(out)
        if len(final_rows) >= main_limit:
            break
    return final_rows


def main() -> int:
    rows = read_csv(INPUT_CSV)
    final_rows = build_final_targets(rows, MAIN_TARGET_UNIQUE, BSR_TARGET_RANK)
    write_csv(OUTPUT_CSV, final_rows)
    manifest = {
        "run_type": "step02_final_targets",
        "input_rows": len(rows),
        "output_rows": len(final_rows),
        "main_target_unique": MAIN_TARGET_UNIQUE,
        "bsr_rank_limit": BSR_TARGET_RANK,
        "bsr_tagged_rows": sum(1 for row in final_rows if row.get("bsr_rank") not in (None, "")),
        "unique_key": "variation_id",
        "rank_rule": "main_rank and bsr_rank are from the same Topseller order; bsr_rank is populated only for raw ranks 1..100 before SKU dedupe.",
        "output_csv": str(OUTPUT_CSV),
    }
    write_json(MANIFEST_OUTPUT, manifest)
    print(f"[step02] final_targets={len(final_rows)} bsr_tagged={manifest['bsr_tagged_rows']} output={OUTPUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
