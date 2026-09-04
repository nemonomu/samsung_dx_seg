"""Fill stable blank fields from matching rows in the retail.com history."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable


def _quote(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _text(value: Any) -> str:
    return str(value or "").strip()


def _item_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", _text(value)).casefold()


def _name_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _text(value)).casefold()
    return re.sub(r"\s+", " ", normalized)


def empty_stats(*, error: str = "") -> dict[str, Any]:
    return {
        "eligible_rows": 0,
        "queried_items": 0,
        "history_rows": 0,
        "matched_history_rows": 0,
        "recovered_rows": 0,
        "recovered_fields": {},
        "error": error,
    }


def _identity(row: dict[str, Any]) -> tuple[str, str] | None:
    item = _item_key(row.get("item"))
    name = _name_key(row.get("retailer_sku_name"))
    return (item, name) if item and name else None


def _eligible_rows(rows, *, account_keys, product_key, fill_fields):
    return [
        row
        for row in rows
        if _text(row.get("account_name")).casefold() in account_keys
        and _text(row.get("product")).upper() == product_key
        and _identity(row)
        and any(not _text(row.get(field)) for field in fill_fields)
    ]


def _failure_stats(kwargs, error: str) -> dict[str, Any]:
    stats = empty_stats(error=error)
    fill_fields = tuple(dict.fromkeys(str(field) for field in kwargs.get("fields", ()) if str(field)))
    account_keys = {
        _text(name).casefold()
        for name in kwargs.get("account_names", ())
        if _text(name)
    }
    eligible = _eligible_rows(
        kwargs.get("rows", ()),
        account_keys=account_keys,
        product_key=_text(kwargs.get("product")).upper(),
        fill_fields=fill_fields,
    )
    stats["eligible_rows"] = len(eligible)
    stats["queried_items"] = len({_identity(row)[0] for row in eligible if _identity(row)})
    return stats


def backfill_from_retail_history(
    cursor,
    *,
    schema: str,
    table: str,
    rows: list[dict[str, Any]],
    account_names: Iterable[str],
    product: str,
    fields: Iterable[str],
    excluded_batch_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Fill blanks field-by-field from newest exact item+name history."""
    stats = empty_stats()
    fill_fields = tuple(dict.fromkeys(str(field) for field in fields if str(field)))
    account_keys = sorted({_text(name).casefold() for name in account_names if _text(name)})
    product_key = _text(product).upper()
    eligible = _eligible_rows(
        rows,
        account_keys=account_keys,
        product_key=product_key,
        fill_fields=fill_fields,
    )
    stats["eligible_rows"] = len(eligible)
    if not eligible or not fill_fields:
        return stats

    item_keys = sorted({_identity(row)[0] for row in eligible if _identity(row)})
    stats["queried_items"] = len(item_keys)
    selected_columns = (
        "item",
        "retailer_sku_name",
        *fill_fields,
        "crawl_strdatetime",
        "batch_id",
    )
    column_sql = ", ".join(_quote(column) for column in selected_columns)
    sql = (
        f"SELECT {column_sql} FROM {_quote(schema)}.{_quote(table)} "
        "WHERE lower(trim(coalesce(\"account_name\"::text, ''))) = ANY(%s) "
        "AND upper(trim(coalesce(\"product\"::text, ''))) = %s "
        "AND lower(trim(coalesce(\"item\"::text, ''))) = ANY(%s) "
        "ORDER BY NULLIF(trim(coalesce(\"crawl_strdatetime\"::text, '')), '') DESC NULLS LAST, "
        "NULLIF(trim(coalesce(\"batch_id\"::text, '')), '') DESC NULLS LAST"
    )
    cursor.execute(sql, (account_keys, product_key, item_keys))
    history = [dict(zip(selected_columns, record)) for record in cursor.fetchall()]
    stats["history_rows"] = len(history)

    excluded = {_text(batch_id) for batch_id in excluded_batch_ids if _text(batch_id)}
    eligible_identities = {_identity(row) for row in eligible}
    history_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for historical in history:
        if _text(historical.get("batch_id")) in excluded:
            continue
        identity = _identity(historical)
        if identity and identity in eligible_identities:
            history_by_identity[identity].append(historical)
            stats["matched_history_rows"] += 1

    recovered_counts: Counter[str] = Counter()
    recovered_rows = 0
    for row in eligible:
        candidates = history_by_identity.get(_identity(row), ())
        changed = False
        for field in fill_fields:
            if _text(row.get(field)):
                continue
            for candidate in candidates:
                value = _text(candidate.get(field))
                if value:
                    row[field] = value
                    recovered_counts[field] += 1
                    changed = True
                    break
        if changed:
            recovered_rows += 1

    stats["recovered_rows"] = recovered_rows
    stats["recovered_fields"] = dict(sorted(recovered_counts.items()))
    return stats


def safe_backfill_from_retail_history(connection, **kwargs) -> dict[str, Any]:
    """Keep a failed history SELECT from aborting the caller's DB insert."""
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("SAVEPOINT seg_last_known_db_backfill")
        try:
            stats = backfill_from_retail_history(cursor, **kwargs)
        except Exception as exc:  # noqa: BLE001 - fail open by contract.
            cursor.execute("ROLLBACK TO SAVEPOINT seg_last_known_db_backfill")
            return _failure_stats(kwargs, type(exc).__name__)
        cursor.execute("RELEASE SAVEPOINT seg_last_known_db_backfill")
        return stats
    except Exception as exc:  # noqa: BLE001 - fail open before/after the SELECT too.
        return _failure_stats(kwargs, type(exc).__name__)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
