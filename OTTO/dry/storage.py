"""20-column checkpoints and transactional, idempotent loading. No DDL/backfill."""
import csv
import hashlib
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dry.config import ACCOUNT_NAME, COUNTRY, COLUMNS, OPTIONAL_COLUMNS, MAX_LIMIT, BSR_LIMIT, DryError
from dry.fields import name_key


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def checkpoint(directory, rows, state):
    path = directory / "otto_dry.csv"
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    state.update(output_rows=len(rows), csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    atomic_json(directory / "state.json", state)


def read_output(path):
    path = Path(path).resolve()
    try:
        state = json.loads((path.parent / "state.json").read_text(encoding="utf-8"))
        body = path.read_bytes()
    except (OSError, ValueError):
        raise DryError("output_or_state_unreadable") from None
    if path.name != "otto_dry.csv" or state.get("collection_status") not in {"complete", "shortfall"}:
        raise DryError("output_not_completed")
    if state.get("csv_sha256") != hashlib.sha256(body).hexdigest():
        raise DryError("output_changed_since_collection")
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        raise DryError("output_column_order_invalid")
    rows = list(reader)
    if not rows or len(rows) != state.get("output_rows") or len(rows) > MAX_LIMIT:
        raise DryError("output_row_count_invalid")
    names = set()
    for rank, row in enumerate(rows, 1):
        expected_bsr = str(rank) if rank <= BSR_LIMIT else ""
        if (row.get("id") or row.get("batch_id") != state.get("batch_id")
                or row.get("main_rank") != str(rank) or row.get("bsr_rank") != expected_bsr
                or row.get("country") != COUNTRY or row.get("account_name") != ACCOUNT_NAME
                or not row.get("item") or not row.get("product_url")
                or row.get("count_of_reviews") != row.get("count_of_star_ratings")):
            raise DryError("output_row_identity_or_rank_invalid")
        name = name_key(row.get("retailer_sku_name"))
        if not name or name in names:
            raise DryError("output_duplicate_or_missing_name")
        names.add(name)
    return rows, state


TEXT_TYPES = {"text", "character varying", "character"}
INTEGER_TYPES = {"smallint", "integer", "bigint"}
NUMBER_TYPES = INTEGER_TYPES | {"numeric", "decimal", "real", "double precision"}
TIME_TYPES = {"timestamp without time zone", "timestamp with time zone"}
NUMBER_COLUMNS = {"count_of_reviews", "count_of_star_ratings", "star_rating", "final_sku_price",
                  "original_sku_price", "savings", "main_rank", "bsr_rank"}


def table_parts(table):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", table):
        raise DryError("table_requires_schema_and_name")
    return tuple(table.split("."))


def metadata(cursor, table):
    cursor.execute("SELECT column_name, data_type, is_nullable, column_default, is_identity, "
                   "character_maximum_length, is_generated FROM information_schema.columns "
                   "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", table_parts(table))
    result = {}
    for name, kind, nullable, default, identity, length, generated in cursor.fetchall():
        result[name] = dict(type=kind, nullable=nullable == "YES", has_default=default is not None,
                            auto_id=identity == "YES" or bool(default and re.match(r"^nextval\(", default)),
                            max_length=length, generated=generated not in (None, "NEVER"))
    return result


def validate_schema(meta):
    missing = set(COLUMNS) - set(meta)
    if missing:
        raise DryError("table_missing_columns:" + ",".join(sorted(missing)))
    if not meta["id"]["auto_id"] or meta["id"]["type"] not in INTEGER_TYPES:
        raise DryError("id_requires_integer_identity_or_sequence")
    for column in COLUMNS[1:]:
        allowed = TEXT_TYPES | (NUMBER_TYPES if column in NUMBER_COLUMNS else set())
        if column == "crawl_datetime":
            allowed |= TIME_TYPES
        if meta[column]["type"] not in allowed or meta[column]["generated"]:
            raise DryError("table_incompatible_type:" + column)
        if column in OPTIONAL_COLUMNS and not meta[column]["nullable"]:
            raise DryError("table_requires_nullable:" + column)
    for column, value in meta.items():
        if column not in COLUMNS and not (value["nullable"] or value["has_default"] or value["generated"]):
            raise DryError("table_extra_required_column:" + column)


def typed(value, column, meta):
    if value in (None, ""):
        if not meta["nullable"]:
            raise DryError("db_required_value_missing:" + column)
        return None
    raw, kind = str(value), meta["type"]
    if kind in TEXT_TYPES:
        if meta["max_length"] is not None and len(raw) > meta["max_length"]:
            raise DryError("db_value_too_long:" + column)
        return raw
    if kind in TIME_TYPES:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise DryError("db_datetime_invalid") from None
        return parsed.astimezone() if kind == "timestamp with time zone" else parsed
    if kind in NUMBER_TYPES:
        raw = raw.strip().replace("\u00a0", "").replace(" ", "")
        if column in {"final_sku_price", "original_sku_price"}:
            raw = raw.replace("EUR", "").replace("€", "")
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".").replace("–", "00").replace("-", "00")
        elif column == "savings":
            raw = raw.removesuffix("%").replace(",", ".")
        try:
            number = Decimal(raw)
        except InvalidOperation:
            raise DryError("db_numeric_invalid:" + column) from None
        if not number.is_finite() or (kind in INTEGER_TYPES and number != number.to_integral_value()):
            raise DryError("db_numeric_invalid:" + column)
        return int(number) if kind in INTEGER_TYPES else number
    raise DryError("db_type_unsupported:" + column)


def connect():
    # Existing approved process-internal loader. Never log configuration or exceptions.
    import psycopg2
    from common.io_util import db_config
    values = db_config()
    if not values:
        raise DryError("db_configuration_missing")
    return psycopg2.connect(host=values.get("host"), port=values.get("port") or 5432,
                            user=values.get("user"), password=values.get("password"),
                            dbname=values.get("database"), connect_timeout=10)


def database(table, events, rows=None, connector=None):
    """Read-only preflight if rows=None; atomic insert or exact same-batch no-op otherwise."""
    schema, name = table_parts(table)
    qualified = f'"{schema}"."{name}"'
    connection, phase = None, "connect"
    try:
        with events.activity("database", phase=phase):
            connection = (connector or connect)()
        connection.set_session(readonly=rows is None)
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '60s'")
                cursor.execute("SET LOCAL lock_timeout = '15s'")
                phase = "schema"
                meta = metadata(cursor, table)
                validate_schema(meta)
                events.emit("schema", table=table, columns={key: dict(type=v["type"], nullable=v["nullable"], auto_id=v["auto_id"])
                                                           for key, v in meta.items() if key in COLUMNS})
                if rows is None:
                    return {"status": "preflight_ok", "inserted": 0, "duplicates": 0}
                if not rows or len({r["batch_id"] for r in rows}) != 1:
                    raise DryError("db_empty_or_mixed_batch")
                columns = COLUMNS[1:]
                values = [tuple(typed(r.get(c), c, meta[c]) for c in columns) for r in rows]
                phase = "batch_check"
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (table, rows[0]["batch_id"]))
                fields = ",".join(f'"{c}"' for c in columns)
                cursor.execute(f"SELECT {fields} FROM {qualified} WHERE batch_id=%s", (rows[0]["batch_id"],))
                existing = cursor.fetchall()
                if existing:
                    # Exact content check also rejects a changed CSV using an old batch ID.
                    def canonical(record):
                        return tuple(str(Decimal(str(v)).normalize()) if v is not None and meta[c]["type"] in NUMBER_TYPES
                                     else str(v) if v is not None else None for c, v in zip(columns, record))
                    expected_set = {canonical(v) for v in values}
                    actual_set = {canonical(v) for v in existing}
                    if len(existing) != len(values) or expected_set != actual_set:
                        raise DryError("db_batch_partial_or_content_conflict")
                    return {"status": "already_loaded", "inserted": 0, "duplicates": len(rows)}
                phase = "insert"
                cursor.executemany(f"INSERT INTO {qualified} ({fields}) VALUES ({','.join(['%s'] * len(columns))})", values)
            phase = "commit"
        return {"status": "loaded", "inserted": len(rows), "duplicates": 0}
    except DryError:
        raise
    except Exception as exc:
        # Database messages can include DSNs or values; only phase and class are safe.
        raise DryError("db_failed:" + phase + ":" + type(exc).__name__) from None
    finally:
        if connection is not None:
            connection.close()
