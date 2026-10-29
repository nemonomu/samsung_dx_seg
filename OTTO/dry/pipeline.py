"""DRY-only orchestration: eligible rows, durable checkpoints, safe failure reporting."""
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dry import config, storage, history, notify
from dry.client import Client, RequestFailure, COMPARISON_FIELD_LABELS
from dry.events import Events
from dry.fields import candidates, choose, classify, name_key, classification_evidence, text


def product_outcome(target, chars, pdf, rating, row, *, options=None):
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
               for field, values in (candidates(target, chars, pdf) if options is None else options).items()}
    comparison_fields = [field for field, source in sources.items() if row.get(field)
                         and source in chars.get("_supplemented_sources", {}).get(field, [])]
    supplemented = bool(comparison_fields) or any(v == "product_pdf" for v in sources.values()) or sources["sku"] == "listing_title"
    return dict(status="supplemented" if supplemented else "collected", missing_fields=missing, issues=issues,
                comparison_supplemented_fields=comparison_fields,
                supplemented_items=supplemented, incomplete_items=bool(missing),
                request_error_items=any(i["kind"] == "request" for i in issues),
                extraction_error_items=any(i["kind"] == "extraction" for i in issues), price_warning_items=price_warning)


def collect_listing(client, events, state):
    """Acquire positions first; never interleave card/spec/PDF/rating requests."""
    result, fingerprints, item_ids, variation_ids = [], set(), set(), set()
    offset, page = 0, 1
    summary = state["listing"] = dict(pages=0, occurrences=0, organic_occurrences=0,
        sponsored_occurrences=0, unique_items=0, unique_variations=0, api_counts=[], display_counts=[])
    state["listing_complete"] = False
    with events.stage("목록 확보"):
        while True:
            page_rows, consumed, end, fingerprint = client.page(page, offset)
            if fingerprint and fingerprint in fingerprints:
                raise RequestFailure("listing_repeated_page_not_end")
            fingerprints.add(fingerprint)
            if not page_rows and not end:
                raise RequestFailure("listing_no_progress_not_end")
            for raw in page_rows:
                target = dict(raw, listing_position=len(result) + 1, listing_page=page)
                result.append(target)
                item_ids.add(target["product_id"])
                variation_ids.add(target["variation_id"])
            meta = getattr(client, "last_page_meta", {})
            summary.update(pages=page, occurrences=len(result), unique_items=len(item_ids), unique_variations=len(variation_ids))
            summary["organic_occurrences"] += meta.get("organic_occurrences", consumed)
            summary["sponsored_occurrences"] += meta.get("sponsored_occurrences", 0)
            for key, destination in (("count", "api_counts"), ("display_count", "display_counts")):
                value = meta.get(key)
                if value is not None and value not in summary[destination]:
                    summary[destination].append(value)
            # Minimal listing evidence is written once in diagnostics, not copied per product.
            events.emit("listing_candidates", page=page, candidates=page_rows)
            events.emit("page", page=page, offset=offset, candidates=len(page_rows), consumed=consumed,
                        actual_end=end, **summary)
            if end:
                state["listing_complete"] = True
                break
            if consumed <= 0:
                raise RequestFailure("listing_offset_stalled")
            offset += consumed
            page += 1
    return result


def select_targets(client, events, state, listing_rows):
    """Classify once from the long title; card names retain LDY's dedup key."""
    selected, names, position = [], set(), 0
    stats, limit = state["counts"], state["target"]

    def record(target, accepted, reason):
        events.emit("classification", item=target["product_id"], name=target.get("retailer_sku_name"),
                    product_url=target.get("product_url"), accepted=accepted, reason=reason,
                    needs_review=accepted and reason.startswith("ambiguous"),
                    listing_position=target["listing_position"],
                    evidence=classification_evidence(target.get("retailer_sku_name"), selection_name=target.get("selection_name")))

    with events.stage("목록 보완·선별"):
        while position < len(listing_rows) and len(selected) < limit:
            pending = []
            while position < len(listing_rows) and len(pending) < min(5, limit-len(selected)):
                target = dict(listing_rows[position])
                position += 1
                stats["reviewed"] += 1
                target["selection_name"] = text(target.get("retailer_sku_name"))
                decision = classify(target["selection_name"], selection_name=target["selection_name"]) if target["selection_name"] else None
                if decision and not decision[0]:
                    stats["excluded"] += 1
                    record(target, *decision)
                else:
                    pending.append((target, decision))
            if not pending:
                continue
            try:
                tiles = client.tiles(list(dict.fromkeys(t["variation_id"] for t, _ in pending)))
            finally:
                stats["tile_http_400_failures"] = getattr(client, "tile_http_400_failures", 0)
            for target, decision in pending:
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
                accepted, reason = decision or classify(target["retailer_sku_name"])
                if not accepted:
                    stats["excluded"] += 1
                    record(target, accepted, reason)
                    continue
                key = name_key(target["retailer_sku_name"])
                if key in names:
                    stats["duplicates"] += 1
                    record(target, False, "duplicate_name")
                    continue
                names.add(key)
                selected.append(target)
                stats["selected"] = len(selected)
                stats["ambiguous"] += int(reason.startswith("ambiguous"))
                record(target, accepted, reason)
            events.emit("selection_progress", **stats, remaining=limit-len(selected),
                        unreviewed_candidates=len(listing_rows)-position)
    events.emit("selection_end", **stats, target=limit, unreviewed_candidates=len(listing_rows)-position)
    return selected


def supplement_comparisons(client, events, chunk, chars_by_id, options, references):
    """Recover proven row omissions, never every NULL or another option's value."""
    for target in chunk:
        vid = target["variation_id"]
        chars = chars_by_id.get(vid, {})
        if "_present_labels" not in chars or chars.get("_error"):
            continue
        for field, labels in COMPARISON_FIELD_LABELS.items():
            if field not in references and any(value and (
                    source == "comparison_model" or source in labels
                    or source.startswith("comparison_spec:"))
                    for source, _, value in options[vid][field]):
                references[field] = vid
    groups = {}
    for target in chunk:
        vid = target["variation_id"]
        chars = chars_by_id.get(vid, {})
        if "_present_labels" not in chars or chars.get("_error"):
            continue  # No successful response/presence evidence: use existing PDF path.
        for field, labels in COMPARISON_FIELD_LABELS.items():
            if (any(value for _, _, value in options[vid][field])
                    or any(label in chars["_present_labels"] for label in labels)):
                continue
            reference = references.get(field)
            if reference:
                groups.setdefault(reference, {}).setdefault(vid, []).append(field)
            else:
                events.emit("comparison_supplement_skipped", variation_id=vid, field=field,
                            reason="reference_unavailable_use_existing_fallback")
    for reference, pending in groups.items():
        ids = list(pending)
        for start in range(0, len(ids), 4):
            batch = ids[start:start+4]
            fresh = client.comparison([reference, *batch], supplement={vid: pending[vid] for vid in batch})
            for vid in batch:
                before, after = chars_by_id.get(vid, {}), fresh.get(vid, {})
                chars_by_id[vid] = after
                # Recompute candidates only when field evidence actually changed.
                if any(before.get(label) != after.get(label)
                       for field in pending[vid] for label in COMPARISON_FIELD_LABELS[field]):
                    target = next(t for t in chunk if t["variation_id"] == vid)
                    options[vid] = candidates(target, after)


def collect(client, events, state, save):
    rows = []
    stats = state["counts"] = dict(reviewed=0, excluded=0, duplicates=0, ambiguous=0, selected=0,
        item_failures=0, eligible=0, tile_http_400_failures=0, supplemented_items=0,
        incomplete_items=0, request_error_items=0, extraction_error_items=0, price_warning_items=0)
    listing_rows = collect_listing(client, events, state)
    targets = select_targets(client, events, state, listing_rows)
    del listing_rows
    total, started = len(targets), time.monotonic()
    references = {}
    with events.stage("사양·평점 수집"):
        for position in range(0, total, 5):
            chunk = targets[position:position+5]
            chars_by_id = client.comparison([t["variation_id"] for t in chunk])
            options_by_id = {t["variation_id"]: candidates(t, chars_by_id.get(t["variation_id"], {})) for t in chunk}
            supplement_comparisons(client, events, chunk, chars_by_id, options_by_id, references)
            for target in chunk:
                item_started = time.monotonic()
                number = len(rows) + 1
                events.emit("product_start", number=number, target=total, goal=state["target"],
                            percent=round(len(rows)/total*100, 1), remaining=total-len(rows),
                            item=target["product_id"], name=target["retailer_sku_name"], product_url=target["product_url"])
                chars = chars_by_id.get(target["variation_id"], {})
                # Title-only selection is final. Comparison/PDF fields cannot exclude rows.
                options = options_by_id[target["variation_id"]]
                need_sku = not options["sku"][0][2]
                need_capacity = not any(v for _, _, v in options["capacity"])
                need_loading = not any(v for _, _, v in options["loading_type"])
                if need_sku or need_capacity or need_loading:
                    events.emit("pdf_fallback", item=target["product_id"], variation_id=target["variation_id"],
                                need_sku=need_sku, need_capacity=need_capacity, need_loading=need_loading,
                                own_link_available=bool(target.get("pdf_uri")))
                pdf = client.pdf(target.get("pdf_uri"), need_sku=need_sku, need_capacity=need_capacity,
                                 need_loading=need_loading) if need_sku or need_capacity or need_loading else {}
                if any(field in pdf or field + "_raw" in pdf for field in options):
                    options = candidates(target, chars, pdf)
                selected = choose(target, chars, pdf, events, options=options)
                rating = client.rating(target["product_id"])
                row = dict.fromkeys(config.COLUMNS)
                row.update(country=config.COUNTRY, account_name=config.ACCOUNT_NAME,
                    item=target["product_id"], retailer_sku_name=target["retailer_sku_name"],
                    product_url=target["product_url"], count_of_reviews=rating["rating_count"],
                    count_of_star_ratings=rating["rating_count"], star_rating=rating["average_rating"],
                    main_rank=number, bsr_rank=number if number <= config.BSR_LIMIT else None,
                    calendar_week=state["calendar_week"], crawl_datetime=state["crawl_datetime"],
                    batch_id=state["batch_id"], **selected)
                for field in ("final_sku_price", "original_sku_price", "savings"):
                    row[field] = target.get(field)
                outcome = product_outcome(target, chars, pdf, rating, row, options=options)
                for counter in ("supplemented_items", "incomplete_items", "request_error_items",
                                "extraction_error_items", "price_warning_items"):
                    stats[counter] += int(outcome[counter])
                rows.append(row)
                stats["eligible"] = len(rows)
                with events.stage("CSV 저장"):
                    save(rows)
                events.emit("product_result", item=target["product_id"], row_saved=True,
                            comparison_title_reason=chars.get("_title_reason"), **outcome)
                average = (time.monotonic()-started)/len(rows)
                events.emit("product_end", number=len(rows), target=total, goal=state["target"],
                    percent=round(len(rows)/total*100, 1), remaining=total-len(rows),
                    item_seconds=round(time.monotonic()-item_started, 2), average_seconds=round(average, 2),
                    estimated_remaining_seconds=round(average*(total-len(rows)), 2))
    if not rows and stats["item_failures"]:
        raise RequestFailure("no_rows_collected_with_item_failures")
    state["end_reason"] = "target_reached" if len(rows) == state["target"] else "actual_search_end"
    state["collection_status"] = "complete"
    state["shortfall_reason"] = None if len(rows) == state["target"] else {
        "reason": "actual_search_end_after_filtering", "missing": state["target"]-len(rows), **stats}
    return rows


def execute(*, limit=config.DEFAULT_LIMIT, table=config.DEFAULT_TABLE, output_root=config.OUTPUT_ROOT,
            load_only=None, client_factory=Client, database=storage.database, backfill=None, notifier=None):
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
        state.update({key: original[key] for key in ("counts", "end_reason", "shortfall_reason", "history_backfill") if key in original})
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
            with events.stage("과거 DB 보충"):
                state["history_backfill"] = (backfill or history.backfill)(table, events, rows)
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
        if state.get("history_backfill", {}).get("error") or any(state.get("counts", {}).get(k) for k in
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
        state.update(exit_code=code, collection_exit_code=code, request_statistics=events.statistics(), finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                     durations={k: round(v, 2) for k, v in events.timings.items()},
                     total_seconds=round(time.monotonic()-events.started, 2), output_rows=len(rows),
                     null_counts={c: sum(r.get(c) in (None, "") for r in rows) for c in config.COLUMNS},
                     csv=str(Path(load_only).resolve()) if load_only else str(directory / "otto_dry.csv"),
                     log=str(directory / "run.log"), diagnostics=str(directory / "diagnostics.jsonl"))
        storage.atomic_json(directory / "state.json", state)
        try:
            with events.stage("메일 보고"):
                state["notification"] = (notifier or notify.send)(state, rows)
        except Exception as exc:
            state["notification"] = dict(status="failed", sent=False, error="email_failed:" + type(exc).__name__)
        if state["notification"].get("status") == "failed" and code == 0:
            code = 6
        state.update(exit_code=code, total_seconds=round(time.monotonic()-events.started, 2),
                     finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                     durations={k: round(v, 2) for k, v in events.timings.items()})
        storage.atomic_json(directory / "state.json", state)
        events.emit("summary", **state)
        events.close()
    return code, state
