"""Step14: load Amazon full output into the SEG retail.com DB table."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from common.io_util import ACCOUNT_NAME, category_output_root, db_config, read_csv, split_table, write_json

INT_COLUMNS = {"main_rank", "bsr_rank"}


def _quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _as_int(value):
    try:
        if value in ("", None):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _empty_to_none(value, column: str):
    if column in INT_COLUMNS:
        return _as_int(value)
    return None if value in ("", None) else value


def run(cfg, *, dry_run: bool | None = None) -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT)
    input_csv = out / "amzn_full_output.csv"
    rows = read_csv(input_csv)
    schema, table = split_table(cfg.DB_TABLE)
    if dry_run is None:
        dry_run = os.getenv("AMZN_DB_DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "y"}
    batch_ids = sorted({(r.get("batch_id") or "").strip() for r in rows if (r.get("batch_id") or "").strip()})
    manifest: dict[str, Any] = {
        "run_type": "db_save",
        "product": cfg.PRODUCT,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": schema,
        "table": table,
        "account_name": ACCOUNT_NAME,
        "csv_rows": len(rows),
        "batch_ids": batch_ids,
        "dry_run": dry_run,
    }
    if not rows:
        manifest.update(success=False, reason="no input rows")
        write_json(out / "step14_db_save_manifest.json", manifest)
        print(f"[db/{cfg.PRODUCT}] no rows; skip")
        return manifest
    if dry_run:
        manifest.update(success=True, skipped=True)
        write_json(out / "step14_db_save_manifest.json", manifest)
        print(f"[db/{cfg.PRODUCT}] dry_run rows={len(rows)} target={schema}.{table}")
        return manifest

    config = db_config()
    if not config:
        raise RuntimeError("config.DB_CONFIG missing")
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
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (schema, table),
                )
                existing = [r[0] for r in cur.fetchall()]
                if not existing:
                    manifest.update(success=True, skipped=True, dry_run=True, reason=f"table {schema}.{table} not found")
                    write_json(out / "step14_db_save_manifest.json", manifest)
                    print(f"[db/{cfg.PRODUCT}] table {schema}.{table} not found -> skipped")
                    return manifest
                csv_fields = list(rows[0].keys())
                insert_cols = [c for c in existing if c != "id" and c in csv_fields]
                deleted = 0
                if batch_ids:
                    cur.execute(
                        f"DELETE FROM {_quote(schema)}.{_quote(table)} WHERE batch_id = ANY(%s) AND account_name = %s",
                        (batch_ids, ACCOUNT_NAME),
                    )
                    deleted = cur.rowcount
                col_sql = ", ".join(_quote(c) for c in insert_cols)
                placeholders = ", ".join(["%s"] * len(insert_cols))
                cur.executemany(
                    f"INSERT INTO {_quote(schema)}.{_quote(table)} ({col_sql}) VALUES ({placeholders})",
                    [tuple(_empty_to_none(r.get(c), c) for c in insert_cols) for r in rows],
                )
                inserted = len(rows)
    finally:
        conn.close()
    manifest.update(success=True, skipped=False, inserted=inserted, deleted_existing=deleted, inserted_columns=insert_cols)
    write_json(out / "step14_db_save_manifest.json", manifest)
    print(f"[db/{cfg.PRODUCT}] target={schema}.{table} deleted={deleted} inserted={inserted} cols={len(insert_cols)}")
    return manifest
