"""Step14: load mmkt_full_output.csv into dx_seg.dx_seg_tv_retail_com (PostgreSQL).

INSERT-ONLY: this step NEVER deletes or updates existing DB rows. Each run
appends the current crawl (distinguished by its batch_id). Any deletion or
edit of existing rows must be done manually by the user — the pipeline is not
permitted to remove DB data automatically. Only columns that exist in the target
table are written; empty strings become SQL NULL. DB credentials come from
DB_CONFIG in the project .env and are never printed.

  python MMKT/step14_db_save.py --dry-run   # map + report, no DB connection
  python MMKT/step14_db_save.py             # actually load (insert-only)
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import importlib

from common.config import ACCOUNT_NAME, db_config, read_csv, write_json


def load_cfg(product: str):
    return importlib.import_module(f"{product}.config")



INT_COLUMNS = {"main_rank", "bsr_rank"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load MMKT full output into the SEG TV retail.com table.")
    p.add_argument("--product", required=True, choices=["tv", "ref", "ldy"])
    p.add_argument("--input", default="")
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("MMKT_DB_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "y"},
        help="Map rows and report, but do not connect/write.",
    )
    return p.parse_args()


def quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def as_int(value):
    try:
        if value in ("", None):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def empty_to_none(value, column: str):
    if column in INT_COLUMNS:
        return as_int(value)
    return None if value in ("", None) else value


def table_columns(cur, schema: str, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table),
    )
    return [row[0] for row in cur.fetchall()]


def main() -> int:
    args = parse_args()
    cfg = load_cfg(args.product)
    schema, table = cfg.DB_TABLE
    input_path = Path(args.input or (cfg.OUTPUT_ROOT / "mmkt_full_output.csv"))
    rows = read_csv(input_path)
    csv_fields = list(rows[0].keys()) if rows else []
    batch_ids = sorted({(r.get("batch_id") or "").strip() for r in rows if (r.get("batch_id") or "").strip()})

    manifest: dict[str, Any] = {
        "run_type": "mmkt_step14_db_save",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_csv": str(input_path),
        "schema": schema,
        "table": table,
        "account_name": ACCOUNT_NAME,
        "csv_rows": len(rows),
        "batch_ids": batch_ids,
        "dry_run": args.dry_run,
    }

    if not rows:
        manifest["success"] = False
        manifest["reason"] = "no input rows (run step09 first)"
        write_json(cfg.OUTPUT_ROOT / "step14_db_save_manifest.json", manifest)
        print(f"[step14] no rows in {input_path}; nothing to load.")
        return 1

    if args.dry_run:
        manifest["success"] = True
        manifest["skipped"] = True
        write_json(cfg.OUTPUT_ROOT / "step14_db_save_manifest.json", manifest)
        print(f"[step14] dry_run rows={len(rows)} target={schema}.{table} "
              f"account={ACCOUNT_NAME} batch_ids={batch_ids}")
        return 0

    config = db_config()
    if not config:
        raise RuntimeError("DB_CONFIG is missing from .env")

    import psycopg2

    conn = psycopg2.connect(
        host=config.get("host"),
        port=int(config.get("port") or 5432),
        user=config.get("user"),
        password=config.get("password"),
        dbname=config.get("database"),
        connect_timeout=10,
    )
    try:
        with conn:
            with conn.cursor() as cur:
                existing = table_columns(cur, schema, table)
                if not existing:
                    raise RuntimeError(f"DB table not found: {schema}.{table}")
                insert_columns = [c for c in existing if c != "id" and c in csv_fields]

                # INSERT-ONLY: never delete/update existing rows. Each run appends
                # the current crawl (its own batch_id). Removing DB rows is a manual,
                # user-authorized action only — the pipeline must not do it.
                column_sql = ", ".join(quote_ident(c) for c in insert_columns)
                placeholders = ", ".join(["%s"] * len(insert_columns))
                sql = (
                    f"INSERT INTO {quote_ident(schema)}.{quote_ident(table)} "
                    f"({column_sql}) VALUES ({placeholders})"
                )
                values = [tuple(empty_to_none(r.get(c), c) for c in insert_columns) for r in rows]
                cur.executemany(sql, values)
                inserted = len(values)
    finally:
        conn.close()

    manifest.update(
        {
            "success": True,
            "skipped": False,
            "inserted_columns": insert_columns,
            "inserted": inserted,
        }
    )
    write_json(cfg.OUTPUT_ROOT / "step14_db_save_manifest.json", manifest)
    print(f"[step14] target={schema}.{table} account={ACCOUNT_NAME} "
          f"inserted={inserted} columns={len(insert_columns)} (insert-only, no delete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
