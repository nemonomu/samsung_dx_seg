"""SEG item_mst upsert helpers for Amazon rows."""
from __future__ import annotations

from typing import Any, Iterable


SPEC_COLS = {
    "tv": ["screen_size", "model_year", "estimated_annual_electricity_use"],
    "ref": ["ref_refrigerator_type", "ref_capacity"],
    "hhp": ["hhp_storage", "hhp_color", "hhp_memory_ram", "trade_in"],
    "ldy": ["ldy_loading_type", "ldy_capacity"],
}


def _quote(ident: str) -> str:
    return '"' + str(ident).replace('"', '""') + '"'


def _table_name(schema: str, product_lower: str) -> tuple[str, str, str]:
    table = f"dx_seg_{product_lower}_item_mst"
    return schema, table, f"{_quote(schema)}.{_quote(table)}"


def _existing_columns(conn, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def upsert_item_mst_batch(conn, schema: str, product_lower: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    product_lower = (product_lower or "").lower()
    if product_lower not in SPEC_COLS:
        return {"attempted": 0, "upserted": 0, "skipped": True, "reason": "unsupported product"}

    schema, table, table_sql = _table_name(schema, product_lower)
    existing = _existing_columns(conn, schema, table)
    if not existing:
        return {"attempted": 0, "upserted": 0, "skipped": True, "reason": f"table {schema}.{table} not found"}
    required = {"item", "account_name"}
    if not required.issubset(existing):
        return {"attempted": 0, "upserted": 0, "skipped": True, "reason": "missing item/account_name columns"}

    candidate_cols = ["item", "account_name", "product_url", "sku", *SPEC_COLS[product_lower]]
    insert_cols = [c for c in candidate_cols if c in existing]
    values: list[tuple[Any, ...]] = []
    for row in rows:
        item = _clean(row.get("item"))
        account = _clean(row.get("account_name"))
        if not item or not account:
            continue
        values.append(tuple(_clean(row.get(c)) for c in insert_cols))
    if not values:
        return {"attempted": 0, "upserted": 0, "skipped": True, "reason": "no valid rows"}

    import psycopg2.extras

    update_parts = []
    if "product_url" in insert_cols:
        update_parts.append("product_url = EXCLUDED.product_url")
    for col in [c for c in ["sku", *SPEC_COLS[product_lower]] if c in insert_cols]:
        update_parts.append(f"{_quote(col)} = COALESCE(NULLIF(t.{_quote(col)}, ''), EXCLUDED.{_quote(col)})")
    if "updated_at" in existing:
        update_parts.append("updated_at = NOW()")
    if not update_parts:
        update_parts.append("item = EXCLUDED.item")

    sql = (
        f"INSERT INTO {table_sql} AS t ({', '.join(_quote(c) for c in insert_cols)}) "
        f"VALUES ({', '.join(['%s'] * len(insert_cols))}) "
        "ON CONFLICT (item, account_name) DO UPDATE SET "
        + ", ".join(update_parts)
    )
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, values, page_size=50)
    return {"attempted": len(values), "upserted": len(values), "skipped": False, "reason": None}


def fill_from_mst(conn, schema: str, product_lower: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    product_lower = (product_lower or "").lower()
    if product_lower not in SPEC_COLS or not rows:
        return {"filled": 0, "skipped": True, "reason": "unsupported product or no rows"}
    schema, table, table_sql = _table_name(schema, product_lower)
    existing = _existing_columns(conn, schema, table)
    if not existing:
        return {"filled": 0, "skipped": True, "reason": f"table {schema}.{table} not found"}
    fill_cols = [c for c in ["sku", *SPEC_COLS[product_lower]] if c in existing]
    if not fill_cols:
        return {"filled": 0, "skipped": True, "reason": "no fill columns"}
    keys = []
    for row in rows:
        item = _clean(row.get("item"))
        account = _clean(row.get("account_name"))
        if item and account:
            keys.append((item, account))
    if not keys:
        return {"filled": 0, "skipped": True, "reason": "no keys"}

    placeholders = ", ".join(["(%s, %s)"] * len(keys))
    params: list[Any] = []
    for key in keys:
        params.extend(key)
    select_cols = ["item", "account_name", *fill_cols]
    mst: dict[tuple[Any, Any], dict[str, Any]] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_quote(c) for c in select_cols)} FROM {table_sql} "
            f"WHERE (item, account_name) IN ({placeholders})",
            params,
        )
        for db_row in cur.fetchall():
            mst[(db_row[0], db_row[1])] = {col: db_row[2 + i] for i, col in enumerate(fill_cols)}

    filled = 0
    for row in rows:
        specs = mst.get((row.get("item"), row.get("account_name")))
        if not specs:
            continue
        for col, value in specs.items():
            if _clean(row.get(col)) is None and _clean(value) is not None:
                row[col] = value
                filled += 1
    return {"filled": filled, "skipped": False, "reason": None}
