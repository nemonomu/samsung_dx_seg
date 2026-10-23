"""DRY-only orchestration: eligible rows, durable checkpoints, safe failure reporting."""
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dry import config, storage
from dry.client import Client, RequestFailure
from dry.events import Events
from dry.fields import candidates, choose, classify, name_key, title_kind


def collect(client, events, state, save):
    rows, names, fingerprints = [], set(), set()
    stats = state["counts"] = dict(reviewed=0, excluded=0, duplicates=0, ambiguous=0,
                                   item_failures=0, eligible=0, tile_http_400_failures=0)
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
                        events.emit("failure", item=target["product_id"], reason="tile_missing",
                                    product_url=target.get("product_url"))
                        continue
                    target.update({k: v for k, v in tile.items() if v not in (None, "")})
                    if not target.get("product_url") or not target.get("retailer_sku_name"):
                        stats["item_failures"] += 1
                        events.emit("failure", item=target["product_id"], reason="required_identity_missing")
                        continue
                    decision, reason = title_kind(original_name)
                    if decision != "exclude":
                        decision, reason = title_kind(target["retailer_sku_name"])
                    if decision == "exclude":
                        stats["excluded"] += 1
                        events.emit("classification", item=target["product_id"], name=target["retailer_sku_name"],
                                    product_url=target["product_url"], accepted=False, reason=reason)
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
                    accepted, reason = classify(target["retailer_sku_name"], chars, target.get("top_infos"))
                    events.emit("classification", item=target["product_id"], name=target["retailer_sku_name"],
                                product_url=target["product_url"], accepted=accepted, reason=reason,
                                needs_review=reason.startswith("ambiguous"))
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
                    if need_sku or need_capacity:
                        events.emit("pdf_fallback", item=target["product_id"], variation_id=target["variation_id"],
                                    need_sku=need_sku, need_capacity=need_capacity, own_link_available=bool(target.get("pdf_uri")))
                    pdf = client.pdf(target.get("pdf_uri"), need_sku=need_sku, need_capacity=need_capacity) if need_sku or need_capacity else {}
                    selected = choose(target, chars, pdf, events)
                    rating = client.rating(target["product_id"])
                    problems = [p for p in (chars.get("_error"), pdf.get("_error"),
                                            rating.get("rating_reason") if rating["rating_state"] == "failed" else None) if p]
                    if problems:
                        stats["item_failures"] += 1
                        events.emit("failure", item=target["product_id"], stage="product", reasons=problems)
                    row = dict.fromkeys(config.COLUMNS)
                    row.update(country=config.COUNTRY, account_name=config.ACCOUNT_NAME,
                               item=target["product_id"], retailer_sku_name=target["retailer_sku_name"],
                               product_url=target["product_url"], count_of_reviews=rating["rating_count"],
                               count_of_star_ratings=rating["rating_count"], star_rating=rating["average_rating"],
                               main_rank=number, bsr_rank=number, calendar_week=state["calendar_week"],
                               crawl_datetime=state["crawl_datetime"], batch_id=state["batch_id"], **selected)
                    for field in ("final_sku_price", "original_sku_price", "savings"):
                        row[field] = target.get(field)
                    rows.append(row)
                    names.add(key)
                    stats["eligible"] = len(rows)
                    with events.stage("CSV 저장"):
                        save(rows)
                    elapsed = time.monotonic() - started
                    average = elapsed / len(rows)
                    events.emit("product_end", number=len(rows), target=limit, percent=round(len(rows)/limit*100, 1),
                                remaining=limit-len(rows), item_seconds=round(time.monotonic()-item_started, 2),
                                average_seconds=round(average, 2), estimated_remaining_seconds=round(average*(limit-len(rows)), 2))
            events.emit("listing_progress", page=page, **stats, remaining=limit-len(rows))
        if end:
            state["end_reason"] = "actual_search_end"
            break
        if consumed <= 0:
            raise RequestFailure("listing_offset_stalled")
        offset += consumed
        page += 1
    if not rows and stats["item_failures"]:
        raise RequestFailure("no_rows_collected_with_item_failures")
    state["collection_status"] = "complete" if len(rows) == limit else "shortfall"
    state["shortfall_reason"] = None if len(rows) == limit else {
        "reason": "actual_search_end_after_filtering", "missing": limit-len(rows), **stats}
    return rows


def execute(*, limit=config.DEFAULT_LIMIT, table=config.DEFAULT_TABLE, output_root=config.OUTPUT_ROOT,
            load_only=None, client_factory=Client, database=storage.database):
    storage.table_parts(table)
    if limit <= 0:
        raise config.DryError("limit_must_be_positive")
    if load_only:
        rows, original = storage.read_output(load_only)
        batch_id = original["batch_id"]
        directory = Path(load_only).resolve().parent / ("reload_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:6])
        state = dict(batch_id=batch_id, mode="load_only", source_csv=str(Path(load_only).resolve()),
                     collection_status=original["collection_status"], target=original["target"], output_rows=len(rows))
    else:
        now = datetime.now()
        batch_id = "o_dry_" + now.strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
        directory = Path(output_root).resolve() / batch_id
        rows = []
        state = dict(batch_id=batch_id, mode="collect", target=limit, collection_status="not_started",
                     crawl_datetime=now.strftime("%Y-%m-%d %H:%M:%S"), calendar_week="w" + str(now.isocalendar().week))
    directory.mkdir(parents=True, exist_ok=False)
    events = Events(directory, batch_id)
    state.update(table=table, db_status="not_started", started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    code = 0
    phase = "database"
    events.emit("start", target=state["target"], table=table, log=str(directory / "run.log"),
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
        if state["collection_status"] == "shortfall" or state.get("counts", {}).get("item_failures"):
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
