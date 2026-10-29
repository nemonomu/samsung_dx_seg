"""One read-only history query; fill only validated blanks for the same option."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from urllib.parse import parse_qs, urlsplit

from common.last_known_db import _item_key, _name_key
from dry.config import ACCOUNT_NAME, COUNTRY
from dry.fields import capacity_candidate, loading, text, valid_sku
from dry.storage import connect, table_parts

FIELDS = ("sku", "capacity", "loading_type")


def identity(row):
    """LDY item+normalized name, with retailer/country and explicit OTTO option."""
    item, name = _item_key(row.get("item")), _name_key(row.get("retailer_sku_name"))
    if not item or not name:
        return None, "item_or_name_missing"
    if (_item_key(row.get("account_name")) != _item_key(ACCOUNT_NAME)
            or _item_key(row.get("country")) != _item_key(COUNTRY)):
        return None, "retailer_or_country_mismatch"
    try:
        url = urlsplit(text(row.get("product_url")))
        options = parse_qs(url.query).get("variationId", [])
        if (url.scheme != "https" or url.hostname not in {"otto.de", "www.otto.de"}
                or url.username or url.password or not url.path.startswith("/p/")
                or len(options) != 1 or not re.fullmatch(r"[A-Za-z0-9]+", options[0])):
            return None, "explicit_option_unconfirmed"
    except ValueError:
        return None, "explicit_option_unconfirmed"
    return (item, name, options[0].casefold()), None


def timestamp(raw):
    try:
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(text(raw))
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def normalized(field, raw):
    value = text(raw)
    if not value:
        return None
    if field == "sku":
        return valid_sku(value)
    if field == "capacity":
        return capacity_candidate(value, labelled=True)[0]
    # The standard stored spelling "Front loader" is not a German source label.
    if value in {"Front loader", "Top-loading"}:
        return value
    if re.search(r"\b(?:nicht|kein\w*|not|no)\b|geeignet für|kompatibel|compatible", value, re.I):
        return None
    value = re.sub(r"\b(front|top) loader\b", r"\1-loaded", value, flags=re.I)
    return loading(value)


def backfill(table, events, rows, *, connector=None):
    stats = dict(status="not_needed", eligible_rows=0, queried_items=0, history_rows=0,
                 recovered_rows=0, recovered_fields={}, rejected={}, error=None)
    rejected = Counter()
    eligible = []
    for row in rows:
        if not any(not text(row.get(field)) for field in FIELDS):
            continue
        key, reason = identity(row)
        when = timestamp(row.get("crawl_datetime"))
        if key is None or when is None or not text(row.get("batch_id")):
            reason = reason or "current_time_or_batch_missing"
            rejected[reason] += 1
            events.emit("history_skipped", item=row.get("item"), reason=reason)
            continue
        eligible.append((row, key, when))
    stats["eligible_rows"] = len(eligible)
    if not eligible:
        if rejected:
            stats["status"] = "skipped_identity"
        stats["rejected"] = dict(rejected)
        return stats

    schema, name = table_parts(table)
    items = sorted({key[0] for _, key, _ in eligible})
    batches = sorted({text(row["batch_id"]) for row in rows})
    needed = [field for field in FIELDS if any(not text(row.get(field)) for row, _, _ in eligible)]
    columns = ("id", "country", "account_name", "item", "retailer_sku_name", "product_url",
               "crawl_datetime", "batch_id", *needed)
    selected = ", ".join('"' + c + '"' for c in columns)
    values_present = " OR ".join(f"NULLIF(trim(\"{field}\"::text), '') IS NOT NULL" for field in needed)
    sql = (f'SELECT {selected} FROM "{schema}"."{name}" '
           "WHERE lower(trim(country::text)) = %s AND lower(trim(account_name::text)) = %s "
           "AND lower(trim(item::text)) = ANY(%s) AND batch_id <> ALL(%s) "
           f"AND ({values_present})")
    stats["queried_items"] = len(items)
    connection = None
    try:
        with events.activity("history_database", items=len(items)):
            connection = (connector or connect)()
            connection.set_session(readonly=True)
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '60s'")
                    cursor.execute("SET LOCAL lock_timeout = '15s'")
                    cursor.execute(sql, (_item_key(COUNTRY), _item_key(ACCOUNT_NAME), items, batches))
                    records = [dict(zip(columns, record)) for record in cursor.fetchall()]
    except Exception as exc:
        # Separate connection: a failed SELECT cannot poison the later INSERT transaction.
        stats.update(status="failed", error="history_query_failed:" + type(exc).__name__)
        events.emit("history_failure", reason=stats["error"], normal_values_preserved=True)
        stats["rejected"] = dict(rejected)
        return stats
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    stats["history_rows"] = len(records)
    wanted = {key for _, key, _ in eligible}
    indexed = defaultdict(list)
    for record in records:
        key, reason = identity(record)
        when = timestamp(record.get("crawl_datetime"))
        if key not in wanted or when is None or not text(record.get("batch_id")) or text(record["batch_id"]) in batches:
            rejected[reason or "history_identity_time_or_batch_mismatch"] += 1
            continue
        indexed[key].append((when, int(record["id"]), record))
    for records_for_key in indexed.values():
        records_for_key.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    recovered = Counter()
    for row, key, when in eligible:
        changed = []
        for field in needed:
            if text(row.get(field)):
                continue
            for previous_time, _, previous in indexed.get(key, ()):
                if previous_time >= when:
                    rejected["not_earlier_history"] += 1
                    continue
                raw = previous.get(field)
                if not text(raw):
                    continue
                value = normalized(field, raw)
                if value is None:
                    rejected["invalid_" + field] += 1
                    events.emit("history_rejected", item=row["item"], variation_id=key[2], field=field,
                                raw=text(raw)[:500], reason="invalid_" + field,
                                source_batch=previous["batch_id"])
                    continue
                row[field] = value
                recovered[field] += 1
                changed.append(field)
                events.emit("history_field", item=row["item"], variation_id=key[2], field=field,
                            raw=text(raw)[:500], selected=value, source="past_database",
                            source_batch=previous["batch_id"], source_time=str(previous["crawl_datetime"]))
                break
        stats["recovered_rows"] += bool(changed)
        events.emit("history_product", item=row["item"], variation_id=key[2], recovered_fields=changed,
                    missing_fields=[field for field in FIELDS if not text(row.get(field))],
                    reason="supplemented" if changed else "matching_valid_history_unavailable")
    stats.update(status="complete", recovered_fields=dict(recovered), rejected=dict(rejected))
    events.emit("history_summary", **stats)
    return stats
