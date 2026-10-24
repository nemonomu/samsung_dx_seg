"""DRY-only orchestration: eligible rows, durable checkpoints, safe failure reporting."""
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dry import config, storage
from dry.client import Client, RequestFailure
from dry.events import Events
from dry.fields import candidates, choose, classify, name_key, classification_evidence


def product_outcome(target, chars, pdf, rating, row):
    issues = []
    for source, payload in (("comparison", chars), ("product_pdf", pdf)):
        if payload.get("_unavailable"):
            issues.append(dict(source=source, kind="unavailable", reason=payload["_unavailable"],
                               missing_fields=payload.get("_missing", [])))
        if payload.get("_error"):
            issues.append(dict(source=source, kind=payload.get("_error_kind", "extraction"), reason=payload["_error"]))
    if rating["rating_state"] == "failed":
        reason = rating.get("rating_reason") or "rating_unconfirmed"
        issues.append(dict(source="rating", kind="request" if reason.startswith("request_failed:") else "extraction", reason=reason))
    missing = [k for k in ("sku", "capacity", "loading_type", "final_sku_price", "star_rating", "count_of_reviews")
               if row.get(k) in (None, "")]
    price_warning = bool(row.get("final_sku_price") and row.get("original_sku_price") and not row.get("savings"))
    if price_warning:
        issues.append(dict(source="price", kind="warning", reason="prices_present_savings_missing"))
    sources = {field: next((source for source, _, value in values if value), None)
               for field, values in candidates(target, chars, pdf).items()}
    supplemented = any(v == "product_pdf" for v in sources.values()) or sources["sku"] == "listing_title"
    return dict(status="supplemented" if supplemented else "collected", missing_fields=missing, issues=issues,
                supplemented_items=supplemented, incomplete_items=bool(missing),
                request_error_items=any(i["kind"] == "request" for i in issues),
                extraction_error_items=any(i["kind"] == "extraction" for i in issues), price_warning_items=price_warning)


def collect(client, events, state, save):
    rows, names, fingerprints = [], set(), set()
    stats = state["counts"] = dict(reviewed=0, excluded=0, duplicates=0, ambiguous=0,
                                   item_failures=0, eligible=0, tile_http_400_failures=0,
                                   supplemented_items=0, incomplete_items=0, request_error_items=0,
                                   extraction_error_items=0, price_warning_items=0)
    offset, page = 0, 1
    limit = state["target"]
    started = time.monotonic()
    while len(rows) < limit:
        with events.stage("목록 수집"):
            page_rows, consumed, end, fingerprint = client.page(page, offset)
        if fingerprint and fingerprint in fingerprints:
            raise RequestFailure("listing_repeated_page_not_end")
        fingerprints.add(fingerprint)
        if not page_rows and not end:
            raise RequestFailure("listing_no_progress_not_end")
        events.emit("page", page=page, offset=offset, candidates=len(page_rows), remaining=limit-len(rows))
        position = 0
        while position < len(page_rows) and len(rows) < limit:
            chunk = page_rows[position:position + min(5, limit-len(rows))]
            position += len(chunk)
            with events.stage("목록 수집"):
                try:
                    tiles = client.tiles(list(dict.fromkeys(t["variation_id"] for t in chunk)))
                finally:
                    # Also persist the run total when this batch aborts before returning.
                    stats["tile_http_400_failures"] = getattr(client, "tile_http_400_failures", 0)
            pending = []
            with events.stage("선별"):
                for target in chunk:
                    stats["reviewed"] += 1
                    original_name = target.get("retailer_sku_name")
                    target["selection_name"] = original_name
                    tile = tiles.get(target["variation_id"])
                    if tile is None:
                        stats["item_failures"] += 1
                        events.emit("failure", item=target["product_id"], reason="tile_missing", row_saved=False,
                                    product_url=target.get("product_url"))
                        continue
                    target.update({k: v for k, v in tile.items() if v not in (None, "")})
                    if not target.get("product_url") or not target.get("retailer_sku_name"):
                        stats["item_failures"] += 1
                        events.emit("failure", item=target["product_id"], reason="required_identity_missing", row_saved=False)
                        continue
                    accepted, reason = classify(target["retailer_sku_name"], infos=target.get("top_infos"),
                                                selection_name=original_name)
                    if not accepted:
                        stats["excluded"] += 1
                        events.emit("classification", item=target["product_id"], name=target["retailer_sku_name"],
                                    product_url=target["product_url"], accepted=False, reason=reason,
                                    evidence=classification_evidence(target["retailer_sku_name"], infos=target.get("top_infos"),
                                                                     selection_name=original_name))
                    elif name_key(target["retailer_sku_name"]) in names:
                        stats["duplicates"] += 1
                        events.emit("classification", item=target["product_id"], accepted=False, reason="duplicate_name")
                    else:
                        pending.append(target)
            with events.stage("사양·평점 수집"):
                chars_by_id = client.comparison([t["variation_id"] for t in pending]) if pending else {}
                for target in pending:
                    item_started = time.monotonic()
                    number = len(rows) + 1
                    events.emit("product_start", number=number, target=limit,
                                percent=round(len(rows)/limit*100, 1), remaining=limit-len(rows),
                                item=target["product_id"], name=target["retailer_sku_name"], product_url=target["product_url"])
                    chars = chars_by_id.get(target["variation_id"], {})
                    accepted, reason = classify(target["retailer_sku_name"], chars, target.get("top_infos"), target.get("selection_name"))
                    events.emit("classification", item=target["product_id"], name=target["retailer_sku_name"],
                                product_url=target["product_url"], accepted=accepted, reason=reason,
                                needs_review=reason.startswith("ambiguous"),
                                evidence=classification_evidence(target["retailer_sku_name"], chars, target.get("top_infos"),
                                                                 target.get("selection_name")))
                    stats["ambiguous"] += int(reason.startswith("ambiguous"))
                    if not accepted:
                        stats["excluded"] += 1
                        continue
                    key = name_key(target["retailer_sku_name"])
                    if key in names:
                        stats["duplicates"] += 1
                        events.emit("classification", item=target["product_id"], accepted=False, reason="duplicate_name")
                        continue
                    options = candidates(target, chars)
                    need_sku = not options["sku"][0][2]  # PDF precedes name fallback
                    need_capacity = not any(v for _, _, v in options["capacity"])
                    need_loading = not any(v for _, _, v in options["loading_type"])
                    if need_sku or need_capacity or need_loading:
                        events.emit("pdf_fallback", item=target["product_id"], variation_id=target["variation_id"],
                                    need_sku=need_sku, need_capacity=need_capacity, need_loading=need_loading, own_link_available=bool(target.get("pdf_uri")))
                    pdf = client.pdf(target.get("pdf_uri"), need_sku=need_sku, need_capacity=need_capacity, need_loading=need_loading) if need_sku or need_capacity or need_loading else {}
                    selected = choose(target, chars, pdf, events)
                    rating = client.rating(target["product_id"])
                    row = dict.fromkeys(config.COLUMNS)
                    row.update(country=config.COUNTRY, account_name=config.ACCOUNT_NAME,
                               item=target["product_id"], retailer_sku_name=target["retailer_sku_name"],
                               product_url=target["product_url"], count_of_reviews=rating["rating_count"],
                               count_of_star_ratings=rating["rating_count"], star_rating=rating["average_rating"],
                               main_rank=number, bsr_rank=number if number <= config.BSR_LIMIT else None,
                               calendar_week=state["calendar_week"],
                               crawl_datetime=state["crawl_datetime"], batch_id=state["batch_id"], **selected)
                    for field in ("final_sku_price", "original_sku_price", "savings"):
                        row[field] = target.get(field)
                    outcome = product_outcome(target, chars, pdf, rating, row)
                    for counter in ("supplemented_items", "incomplete_items", "request_error_items",
                                    "extraction_error_items", "price_warning_items"):
                        stats[counter] += int(outcome[counter])
                    rows.append(row)
                    names.add(key)
                    stats["eligible"] = len(rows)
                    with events.stage("CSV 저장"):
                        save(rows)
                    events.emit("product_result", item=target["product_id"], row_saved=True,
                                comparison_title_reason=chars.get("_title_reason"), **outcome)
                    elapsed = time.monotonic() - started
                    average = elapsed / len(rows)
                    events.emit("product_end", number=len(rows), target=limit, percent=round(len(rows)/limit*100, 1),
                                remaining=limit-len(rows), item_seconds=round(time.monotonic()-item_started, 2),
                                average_seconds=round(average, 2), estimated_remaining_seconds=round(average*(limit-len(rows)), 2))
            events.emit("listing_progress", page=page, **stats, remaining=limit-len(rows))
        if len(rows) == limit:
            state["end_reason"] = "target_reached"
            break
        if end:
            state["end_reason"] = "actual_search_end"
            break
        if consumed <= 0:
            raise RequestFailure("listing_offset_stalled")
        offset += consumed
        page += 1
    if not rows and stats["item_failures"]:
        raise RequestFailure("no_rows_collected_with_item_failures")
    state["collection_status"] = "complete"
    # Retain the existing informational field; actual search exhaustion is not an error.
    state["shortfall_reason"] = None if len(rows) == limit else {
        "reason": "actual_search_end_after_filtering", "missing": limit-len(rows), **stats}
    return rows


def execute(*, limit=config.DEFAULT_LIMIT, table=config.DEFAULT_TABLE, output_root=config.OUTPUT_ROOT,
            load_only=None, client_factory=Client, database=storage.database):
    storage.table_parts(table)
    if limit <= 0:
        raise config.DryError("limit_must_be_positive")
    if limit > config.MAX_LIMIT:
        raise config.DryError(f"limit_exceeds_maximum:{config.MAX_LIMIT}")
    if load_only:
        rows, original = storage.read_output(load_only)
        batch_id = original["batch_id"]
        directory = Path(load_only).resolve().parent / ("reload_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:6])
        state = dict(batch_id=batch_id, mode="load_only", source_csv=str(Path(load_only).resolve()),
                     collection_status=original["collection_status"], target=original["target"], output_rows=len(rows))
        # Preserve original item errors when an old shortfall or completed batch is reloaded.
        state.update({key: original[key] for key in ("counts", "end_reason", "shortfall_reason") if key in original})
    else:
        now = datetime.now()
        batch_id = "o_dry_" + now.strftime("%Y%m%d_%H%M%S")
        directory = Path(output_root).resolve() / batch_id
        rows = []
        state = dict(batch_id=batch_id, mode="collect", target=limit, collection_status="not_started",
                     crawl_datetime=now.strftime("%Y-%m-%d %H:%M:%S"), calendar_week="w" + str(now.isocalendar().week))
    directory.mkdir(parents=True, exist_ok=False)
    events = Events(directory, batch_id)
    state.update(table=table, bsr_limit=config.BSR_LIMIT, db_status="not_started",
                 started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    code = 0
    phase = "database"
    events.emit("start", target=state["target"], bsr_limit=config.BSR_LIMIT, table=table, log=str(directory / "run.log"),
                mode=state["mode"], estimated_remaining_seconds=None)
    try:
        storage.atomic_json(directory / "state.json", state)
        if not load_only:
            with events.stage("DB 구조 확인"):
                database(table, events)
            state["collection_status"] = "running"
            phase = "collection"
            storage.checkpoint(directory, rows, state)
            rows = collect(client_factory(events), events, state,
                           lambda current: storage.checkpoint(directory, current, state))
            with events.stage("CSV 저장"):
                storage.checkpoint(directory, rows, state)
        state["db_status"] = "running"
        phase = "database"
        storage.atomic_json(directory / "state.json", state)
        if rows:
            with events.stage("DB 적재"):
                state["database"] = database(table, events, rows)
            state["db_status"] = state["database"]["status"]
        else:
            state["db_status"] = "skipped_empty"
        if any(state.get("counts", {}).get(k) for k in
                ("item_failures", "request_error_items", "extraction_error_items", "price_warning_items")):
            code = 2
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if isinstance(exc, config.DryError) else type(exc).__name__
        if phase == "collection":
            state["collection_status"] = "failed"
            code = 3
        else:
            state["db_status"] = "failed"
            code = 4
        state["failure"] = reason
        events.emit("failure", scope="collection" if code == 3 else "database", reason=reason)
        # Checkpoints already hold completed rows even if collect() did not return.
    finally:
        if not load_only and (directory / "otto_dry.csv").exists():
            import csv
            with (directory / "otto_dry.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        state.update(exit_code=code, finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                     durations={k: round(v, 2) for k, v in events.timings.items()},
                     total_seconds=round(time.monotonic()-events.started, 2), output_rows=len(rows),
                     null_counts={c: sum(r.get(c) in (None, "") for r in rows) for c in config.COLUMNS},
                     csv=str(Path(load_only).resolve()) if load_only else str(directory / "otto_dry.csv"),
                     log=str(directory / "run.log"), diagnostics=str(directory / "diagnostics.jsonl"))
        storage.atomic_json(directory / "state.json", state)
        events.emit("summary", **state)
        events.close()
    return code, state
