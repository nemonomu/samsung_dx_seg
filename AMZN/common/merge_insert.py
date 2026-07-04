"""SIEL-style JSONL merge and retail_com insert for SEG Amazon."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from common import item_mst
from common.config import run_meta
from common.full_output import BASE_FIELDS
from common.io_util import ACCOUNT_NAME, COUNTRY, category_output_root, db_config, split_table, write_csv, write_json
from common.jsonl import read_jsonl
from common.translations import translate_record_fields

INT_COLUMNS = {"main_rank", "bsr_rank"}
BOOL_COLUMNS = {"redirect"}


def _quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _as_int(value: Any) -> int | None:
    try:
        if value in ("", None):
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if value in ("", None):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _db_value(value: Any, column: str) -> Any:
    if column in INT_COLUMNS:
        return _as_int(value)
    if column in BOOL_COLUMNS:
        return _as_bool(value)
    return None if value in ("", None) else value


def _record_key(rec: dict[str, Any] | None) -> str:
    if not rec:
        return ""
    return str(rec.get("asin") or rec.get("item") or "").strip()


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _calendar_week(value: str | None = None) -> str:
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return "w" + str(dt.isocalendar().week)
        except ValueError:
            pass
    return run_meta("a")["calendar_week"]


def split_records(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    main: dict[str, dict[str, Any]] = {}
    bsr: dict[str, dict[str, Any]] = {}
    details: list[dict[str, Any]] = []
    for rec in records:
        stage = rec.get("stage")
        key = _record_key(rec)
        if stage == "main" and key:
            main.setdefault(key, rec)
        elif stage == "bsr" and key:
            bsr.setdefault(key, rec)
        elif stage == "detail":
            details.append(rec)
    return main, bsr, details


def make_row(cfg, main_rec: dict[str, Any] | None, bsr_rec: dict[str, Any] | None,
             detail_rec: dict[str, Any] | None) -> dict[str, Any] | None:
    detail_rec = detail_rec or {}
    detail_skip = detail_rec.get("_detail_skip")
    redirect_listing_only = detail_skip == "asin_mismatch" and detail_rec.get("redirect") is True
    if detail_skip and not redirect_listing_only:
        return None

    primary = main_rec or bsr_rec or detail_rec
    if not primary:
        return None
    redirect_use_landing = detail_rec.get("_redirect_use_landing") is True and detail_rec.get("redirect") is True
    detail_first = redirect_use_landing
    crawl_dt = _first(detail_rec.get("crawl_datetime"), primary.get("crawl_datetime"), detail_rec.get("crawl_strdatetime"))
    item = _first(detail_rec.get("item"), detail_rec.get("landing_asin"), primary.get("item"), primary.get("asin"))
    if redirect_listing_only:
        item = _first(primary.get("item"), primary.get("asin"), detail_rec.get("asin"))
    row = {
        "account_name": ACCOUNT_NAME,
        "product": getattr(cfg, "PRODUCT", "").upper(),
        "country": COUNTRY,
        "page_type": "main" if main_rec else "bsr",
        "crawl_strdatetime": crawl_dt,
        "calendar_week": _first(detail_rec.get("calendar_week"), primary.get("calendar_week"), _calendar_week(crawl_dt)),
        "batch_id": _first(detail_rec.get("batch_id"), primary.get("batch_id")),
        "main_rank": (main_rec or {}).get("main_rank"),
        "bsr_rank": (bsr_rec or {}).get("bsr_rank"),
        "item": item,
        "product_url": _first(primary.get("product_url"), detail_rec.get("product_url")),
        "redirect": detail_rec.get("redirect") if detail_rec.get("redirect") is not None else False,
        "retailer_sku_name": _first(
            detail_rec.get("retailer_sku_name") if detail_first else None,
            primary.get("retailer_sku_name"),
            detail_rec.get("retailer_sku_name"),
        ),
        "final_sku_price": _first(
            detail_rec.get("final_sku_price") if detail_first else None,
            primary.get("final_sku_price"),
            detail_rec.get("final_sku_price"),
        ),
        "original_sku_price": _first(
            detail_rec.get("original_sku_price") if detail_first else None,
            primary.get("original_sku_price"),
            detail_rec.get("original_sku_price"),
        ),
        "savings": _first(primary.get("savings"), detail_rec.get("savings")),
        "sku_popularity": _first(primary.get("sku_popularity"), detail_rec.get("sku_popularity")),
        "number_of_units_purchased_past_month": _first(
            primary.get("number_of_units_purchased_past_month"),
            detail_rec.get("number_of_units_purchased_past_month"),
        ),
        "sku_status": _first(primary.get("sku_status"), detail_rec.get("sku_status")),
        "discount_type": _first(primary.get("discount_type"), detail_rec.get("discount_type")),
    }
    detail_fields = [
        "available_quantity_for_purchase", "delivery_availability", "fastest_delivery",
        "inventory_status", "screen_size", "model_year", "sku",
        "estimated_annual_electricity_use", "retailer_sku_name_similar",
        "star_rating", "count_of_star_ratings", "count_of_reviews",
        "summarized_review_content", "detailed_review_content",
        "ref_refrigerator_type", "ref_capacity",
    ]
    for field in detail_fields:
        row[field] = _first(detail_rec.get(field), primary.get(field))
    if redirect_listing_only:
        row["_redirect_listing_only"] = True
    translate_record_fields(row)
    return row


def merge_jsonl(cfg, jsonl_path: str | Path) -> list[dict[str, Any]]:
    records = read_jsonl(jsonl_path)
    main, bsr, details = split_records(records)
    rows: list[dict[str, Any]] = []
    detail_keys = set()
    for detail in details:
        key = _record_key(detail)
        if not key:
            continue
        detail_keys.add(key)
        row = make_row(cfg, main.get(key), bsr.get(key), detail)
        if row:
            rows.append(row)
    return rows


def _connect():
    config = db_config()
    if not config:
        raise RuntimeError("DB_CONFIG missing from .env")
    import psycopg2

    return psycopg2.connect(
        host=config.get("host"),
        port=int(config.get("port") or 5432),
        user=config.get("user"),
        password=config.get("password"),
        dbname=config.get("database"),
        connect_timeout=10,
    )


def _existing_columns(conn, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


def _safe_item_mst_fill(conn, schema: str, product_lower: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT seg_item_mst_fill")
        result = item_mst.fill_from_mst(conn, schema, product_lower, rows)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT seg_item_mst_fill")
        return result
    except Exception as exc:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT seg_item_mst_fill")
        return {"filled": 0, "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}


def _safe_item_mst_upsert(conn, schema: str, product_lower: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT seg_item_mst_upsert")
        result = item_mst.upsert_item_mst_batch(conn, schema, product_lower, rows)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT seg_item_mst_upsert")
        return result
    except Exception as exc:  # noqa: BLE001
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT seg_item_mst_upsert")
        return {"attempted": 0, "upserted": 0, "skipped": True, "reason": f"{type(exc).__name__}: {exc}"}


def insert_rows(cfg, rows: list[dict[str, Any]], *, dry_run: bool = False,
                manifest_name: str = "step14_jsonl_db_save_manifest.json") -> dict[str, Any]:
    out = category_output_root(cfg.PRODUCT)
    schema, table = split_table(cfg.DB_TABLE)
    batch_ids = sorted({str(r.get("batch_id") or "").strip() for r in rows if r.get("batch_id")})
    product_lower = getattr(cfg, "PRODUCT", "").lower()
    manifest: dict[str, Any] = {
        "run_type": "jsonl_db_save",
        "product": cfg.PRODUCT,
        "schema": schema,
        "table": table,
        "rows_full": len(rows),
        "batch_ids": batch_ids,
        "dry_run": dry_run,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    preview_path = out / "amzn_full_output.csv"
    write_csv(preview_path, rows, [f for f in BASE_FIELDS if any(f in row for row in rows)])
    manifest["preview_csv"] = str(preview_path)
    if not rows:
        manifest.update(success=False, inserted_total=0, message="no merged rows")
        write_json(out / manifest_name, manifest)
        return manifest
    if dry_run:
        manifest.update(success=True, skipped=True, inserted_total=0)
        write_json(out / manifest_name, manifest)
        return manifest

    conn = _connect()
    try:
        with conn:
            existing = _existing_columns(conn, schema, table)
            if not existing:
                manifest.update(success=True, skipped=True, inserted_total=0, message=f"table {schema}.{table} not found")
                write_json(out / manifest_name, manifest)
                return manifest
            _safe_item_mst_fill(conn, schema, product_lower, rows)
            insert_cols = [c for c in existing if c != "id" and any(c in r for r in rows)]
            deleted = 0
            with conn.cursor() as cur:
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
                    [tuple(_db_value(r.get(c), c) for c in insert_cols) for r in rows],
                )
            mst = _safe_item_mst_upsert(conn, schema, product_lower, [r for r in rows if not r.get("_redirect_listing_only")])
    finally:
        conn.close()

    manifest.update(
        success=True,
        skipped=False,
        deleted_existing=deleted,
        inserted_total=len(rows),
        inserted_columns=insert_cols,
        item_mst=mst,
    )
    write_json(out / manifest_name, manifest)
    return manifest


def insert_jsonl(cfg, jsonl_path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    rows = merge_jsonl(cfg, jsonl_path)
    manifest = insert_rows(cfg, rows, dry_run=dry_run)
    manifest["jsonl_path"] = str(jsonl_path)
    write_json(category_output_root(cfg.PRODUCT) / "step14_jsonl_db_save_manifest.json", manifest)
    return manifest


class StreamingRetailInserter:
    """Streaming retail_com inserter. Product_list is intentionally excluded."""

    def __init__(self, cfg, *, batch_id: str, dry_run: bool = False):
        self.cfg = cfg
        self.batch_id = batch_id
        self.dry_run = dry_run
        self.schema, self.table = split_table(cfg.DB_TABLE)
        self.product_lower = getattr(cfg, "PRODUCT", "").lower()
        self.main: dict[str, dict[str, Any]] = {}
        self.bsr: dict[str, dict[str, Any]] = {}
        self.inserted = 0
        self.deleted = 0
        self.errors: list[str] = []
        self.conn = None
        self.insert_cols: list[str] = []
        if not dry_run:
            self.conn = _connect()
            existing = _existing_columns(self.conn, self.schema, self.table)
            self.insert_cols = [c for c in existing if c != "id"]
            if self.insert_cols:
                with self.conn:
                    with self.conn.cursor() as cur:
                        cur.execute(
                            f"DELETE FROM {_quote(self.schema)}.{_quote(self.table)} WHERE batch_id = %s AND account_name = %s",
                            (batch_id, ACCOUNT_NAME),
                        )
                        self.deleted = cur.rowcount

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def handle(self, rec: dict[str, Any]) -> None:
        stage = rec.get("stage")
        key = _record_key(rec)
        if stage == "main" and key:
            self.main.setdefault(key, rec)
            return
        if stage == "bsr" and key:
            self.bsr.setdefault(key, rec)
            return
        if stage == "detail" and key:
            row = make_row(self.cfg, self.main.get(key), self.bsr.get(key), rec)
            if row:
                self.insert_row(row)

    def insert_row(self, row: dict[str, Any]) -> None:
        if self.dry_run:
            self.inserted += 1
            return
        if self.conn is None or not self.insert_cols:
            return
        insert_cols = [c for c in self.insert_cols if c in row]
        if not insert_cols:
            return
        try:
            with self.conn:
                _safe_item_mst_fill(self.conn, self.schema, self.product_lower, [row])
                with self.conn.cursor() as cur:
                    col_sql = ", ".join(_quote(c) for c in insert_cols)
                    placeholders = ", ".join(["%s"] * len(insert_cols))
                    cur.execute(
                        f"INSERT INTO {_quote(self.schema)}.{_quote(self.table)} ({col_sql}) VALUES ({placeholders})",
                        tuple(_db_value(row.get(c), c) for c in insert_cols),
                    )
                if not row.get("_redirect_listing_only"):
                    _safe_item_mst_upsert(self.conn, self.schema, self.product_lower, [row])
            self.inserted += 1
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def summary(self) -> dict[str, Any]:
        return {
            "stage": "db_insert_summary",
            "inserted_total": self.inserted,
            "deleted_existing": self.deleted,
            "rows_full": self.inserted,
            "errors": self.errors[:20],
        }
