"""DRY-only rendered pagination. Shared TV/REF/LDY collectors are untouched.

Only already-observed page structure is used: the listing counter/control and
ProductListPage.products. No speculative pageCount/currentPage API fields.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from common.listing_policy import is_banner_ad
from common.parsers import (extract_listing_pagination, extract_preloaded_state,
                            extract_rendered_listing_rows, parse_listing_html, _norm_id)
from dry.detail import CollectionError
from dry.events import emit


def page_view(html, rows):
    """Reduce the rendered document to public IDs, counts and control states."""
    pagination = extract_listing_pagination(html)
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find(id="mms-search-productlist") or soup.find(attrs={"data-test": "mms-search-srp-productlist"})
    controls = [] if root is None else [control for control in root.find_all(["button", "a"])
        if re.search(r"weitere\s+Produkte\s+anzeigen", control.get_text(" ", strip=True), re.I)]
    next_enabled = any(not control.has_attr("disabled") and control.get("aria-disabled") != "true"
                       for control in controls)
    # The visible German category heading is also present in supplied screenshots.
    # When available, it can contradict an old counter after a live total change.
    heading_totals = {int(match.group(1).replace(".", "")) for heading in soup.find_all("h1")
                      if (match := re.search(r"\b(?:Wäsche)?Trockner\s*\(\s*([\d.]+)\s+Artikel\s*\)",
                                             heading.get_text(" ", strip=True), re.I))}
    state = extract_preloaded_state(html) or {}
    apollo = state.get("apolloState") or {}
    lists = [value for value in apollo.values() if isinstance(value, dict)
             and value.get("__typename") == "ProductListPage"]
    # Match the shared parser's ProductListPage ordering. Entries without a
    # productId are not category product slots (e.g. non-product placeholders).
    valid_lists = bool(lists) and all(isinstance(value.get("products"), list) for value in lists)
    entries = [entry for value in lists for entry in value["products"]] if valid_lists else []
    state_ids = tuple(pid for entry in entries if isinstance(entry, dict)
                      and (pid := _norm_id(entry.get("productId"))))
    normal_ids = tuple(dict.fromkeys(str(row.get("sku_id")) for row in rows
                                    if row.get("sku_id") and not is_banner_ad(row)))
    extras = [row for row in rows if row.get("sku_id") and not is_banner_ad(row)
              and str(row["sku_id"]) not in set(state_ids)]
    coherent = bool(valid_lists and len(state_ids) == len(set(state_ids))
                    and set(state_ids).issubset(normal_ids)
                    and all(row.get("sku_status") == "Sponsored" for row in extras))
    return {**pagination, "next_control_present": bool(controls), "next_enabled": next_enabled,
            "heading_totals": sorted(heading_totals),
            "state_page_objects": len(lists),
            "ignored_state_entries": len(entries) - len(state_ids),
            "state_ids": state_ids, "signature": state_ids if state_ids else normal_ids,
            "slot_count": len(state_ids), "state_coherent": coherent}


def complete_dom(html, view):
    rendered, _ = extract_rendered_listing_rows(html)
    ordinary = [row for row in rendered if not is_banner_ad(row)]
    ids = [str(row["sku_id"]) for row in ordinary]
    extras = [row for row in ordinary if str(row["sku_id"]) not in set(view["state_ids"])]
    return bool(view["state_coherent"] and set(view["state_ids"]).issubset(ids)
                and all(row.get("sku_status") == "Sponsored" for row in extras))


def first_page_evidence(rows, view):
    """Public occurrence-level evidence; DOM completeness is not admission.

    The shared parser can complete ordinary cards from the internal listing.
    A banner occurrence never disqualifies an ordinary occurrence of that ID.
    """
    state_ids = view.get("state_ids", ())
    normal = [row for row in rows if row.get("sku_id") and not is_banner_ad(row)]
    normal_ids = {str(row["sku_id"]) for row in normal}
    banner_ids = {str(row["sku_id"]) for row in rows if row.get("sku_id") and is_banner_ad(row)}
    missing = sorted(set(state_ids) - normal_ids)
    duplicates = sorted({pid for pid in state_ids if state_ids.count(pid) > 1})
    extras = sorted({str(row["sku_id"]) for row in normal
                     if str(row["sku_id"]) not in state_ids and row.get("sku_status") != "Sponsored"})
    issues = []
    if missing:
        issues.append("state_products_missing_from_normal_cards")
    if duplicates:
        issues.append("duplicate_state_product_ids")
    if extras:
        issues.append("extra_normal_cards_without_sponsored_evidence")
    if not view.get("state_coherent") and not issues:
        issues.append("internal_listing_state_unconfirmed")
    return {"listing_valid": view.get("listing_valid", False),
            "state_coherent": view.get("state_coherent", False),
            "state_slots": len(state_ids), "normal_unique": len(normal_ids),
            "missing_from_normal": missing, "duplicate_state_ids": duplicates,
            "non_sponsored_extra_ids": extras, "banner_ids": sorted(banner_ids),
            "banner_only_state_ids": sorted(set(missing) & banner_ids),
            "banner_and_normal_ids": sorted(banner_ids & normal_ids),
            "normal_sponsored_ids": sorted({str(row["sku_id"]) for row in normal
                                            if row.get("sku_status") == "Sponsored"}),
            "card_evidence": [{"item": str(row["sku_id"]),
                               "listing_card_type": row.get("listing_card_type"),
                               "sponsored_label": row.get("sku_status") == "Sponsored"}
                              for row in rows if row.get("sku_id")],
            "issues": issues}


class EndEvidence:
    """A repeat is terminal only after independently verified slot coverage.

    Slots are category positions BEFORE eligibility/dedup, not accepted SKUs.
    Banner/extra DOM products never increase this ledger. Changed totals or
    variable earlier block sizes make this historical fallback unavailable.
    TailConfirmation can independently establish a fresh current boundary.
    """
    def __init__(self):
        self.totals = set()
        self.slots = 0
        self.width = None
        self.full_prefix = True
        self.coherent_prefix = True
        self.last_page = 0
        self.last_signature = None
        self.tail_candidate = False
        self.views = {}

    def assess(self, page, html, view, repeated):
        total = view.get("total")
        totals = self.totals | set(view.get("heading_totals", [])) | ({total} if total is not None else set())
        changed = len(totals) > 1
        known_total = next(iter(totals)) if len(totals) == 1 else None
        count = view["slot_count"]
        slots = self.slots if repeated else self.slots + count
        result = {"known_total": known_total, "total_changed": changed,
                  "observed_totals": sorted(totals), "uniform_prefix": self.full_prefix,
                  "category_slots_read": slots, "observed_block_size": self.width,
                  "next_control_present": view["next_control_present"], "next_enabled": view["next_enabled"],
                  "state_slots": count, "state_coherent": view["state_coherent"],
                  "end_basis": None, "end_candidate": False, "tail_dom_verified": None}
        if not repeated and view["last_page"]:
            if view["next_enabled"]:
                result["pagination_error"] = "counter_next_control_conflict"
            elif changed:
                result["pagination_error"] = "total_changed_at_end"
            elif view["total"] == 0 and (view["slot_count"] or not view["state_coherent"]):
                result["pagination_error"] = "zero_total_with_products"
            elif view["total"] != 0 and (not view["state_coherent"] or not self.coherent_prefix
                                          or slots != view["total"]):
                result["pagination_error"] = "counter_slot_mismatch"
            else:
                result["end_basis"] = "explicit_listing_counter"
            return result
        if (not repeated and known_total == 0 and not changed and not count
                and view["state_coherent"] and not view["next_enabled"] and complete_dom(html, view)):
            result.update(end_basis="zero_category_heading_and_empty_state", tail_dom_verified=True)
            return result
        # The next page must repeat the immediately preceding, coherent tail.
        if repeated:
            if (self.tail_candidate and not changed and page == self.last_page + 1
                    and view["signature"] == self.last_signature and not view["next_enabled"]):
                result["tail_dom_verified"] = complete_dom(html, view)
                if result["tail_dom_verified"]:
                    result.update(end_basis="stable_total_slots_and_verified_tail_repeat",
                                  end_for_page=self.last_page)
            return result
        width = self.width or count
        coverage = (known_total is not None and known_total > 0 and slots == known_total
                    and self.full_prefix and page == self.last_page + 1
                    and width > 0 and 0 < count <= width
                    and (page - 1) * width + count == known_total)
        if coverage and not changed and not view["next_enabled"] and view["state_coherent"]:
            result["tail_dom_verified"] = complete_dom(html, view)
            result["end_candidate"] = result["tail_dom_verified"]
        return result

    def commit(self, page, view, decision):
        if view.get("total") is not None:
            self.totals.add(view["total"])
        self.totals.update(view.get("heading_totals", []))
        count = view["slot_count"]
        if self.width is None:
            self.width = count
        self.full_prefix = self.full_prefix and view["state_coherent"] and count == self.width
        self.coherent_prefix = self.coherent_prefix and view["state_coherent"]
        self.slots += count
        self.last_page = page
        self.last_signature = view["signature"]
        self.tail_candidate = decision["end_candidate"]
        self.views[page] = dict(view)


def document_total(view):
    """A current document must supply one unambiguous counter/heading total."""
    values = set(view.get("heading_totals", []))
    if view.get("total") is not None:
        values.add(view["total"])
    return next(iter(values)) if len(values) == 1 else None


class TailConfirmation:
    """Read-only boundary checks, with one eight-read budget per listing.

    History stores public listing state only, never HTML. Probe HTML lives only
    for the current check. No probe may commit rows, ranks or category slots.
    Unvisited middle-page changes remain outside this boundary proof.
    """
    LIMIT = 8

    def __init__(self, fetch, on_probe=None):
        self.fetch, self.on_probe = fetch, on_probe
        self.queries = 0
        self.cycle = 0
        self.latest_total = None

    def _read(self, page, role, ledger, candidate):
        self.queries += 1
        view, html, error, status = {}, "", None, None
        try:
            html, status, _, fetch_error, _ = self.fetch(page)
            if status != 200 or fetch_error:
                error = "probe_fetch_failed"
            else:
                rows = parse_listing_html(html, page=page)
                view = page_view(html, rows)
                if (not view["listing_valid"] or not view["state_coherent"]
                        or any(not row.get("product_url") for row in rows if not is_banner_ad(row))
                        or (view["last_page"] and view["next_enabled"])):
                    error = "probe_listing_invalid"
        except Exception as exc:
            error = "probe_failed:" + type(exc).__name__
        current_total = document_total(view)
        if current_total is not None:
            self.latest_total = current_total
        record = {"phase": "tail_confirmation", "page": page, "role": role,
                  "confirmation_cycle": self.cycle, "candidate_page": candidate,
                  "queries": self.queries, "query_limit": self.LIMIT,
                  "remaining": self.LIMIT - self.queries, "ready_status": status,
                  "error": error, "observed_totals": sorted(ledger.totals),
                  "category_slots_read": ledger.slots, "current_total": current_total,
                  "state_slots": view.get("slot_count"), "state_coherent": view.get("state_coherent"),
                  "next_enabled": view.get("next_enabled"), "state_ids": list(view.get("state_ids", ()))}
        emit("tail_confirmation_read", **record)
        if self.on_probe is not None:
            self.on_probe(record)
        return None if error else (html, view)

    def confirm(self, ledger, tail_html):
        self.cycle += 1
        self.latest_total = None
        page, views = ledger.last_page, ledger.views
        latest, reason, mismatch = None, "confirmation_budget_exhausted", {}

        def result(action, code, next_ids=None):
            return {"action": action, "reason": code, "queries": self.queries,
                    "query_limit": self.LIMIT, "remaining": self.LIMIT - self.queries,
                    "confirmation_cycle": self.cycle, "candidate_page": page,
                    "latest_total": self.latest_total, "reference_total": latest,
                    "confirmed_total": latest if action == "complete" else None,
                    "category_slots_read": ledger.slots,
                    "observed_totals": sorted(ledger.totals), **mismatch,
                    "next_ids": next_ids}

        if page < 2 or set(views) != set(range(1, page + 1)):
            return result("failed", "non_contiguous_history")
        signatures = [view["signature"] for view in views.values()]
        if (not all(view["state_coherent"] for view in views.values())
                or len(set(signatures)) != len(signatures)):
            return result("failed", "incoherent_or_repeated_history")
        width, tail, previous = views[1]["slot_count"], views[page], views[page - 1]
        if not width or any(views[p]["slot_count"] != width for p in range(1, page)):
            return result("failed", "non_uniform_prefix")
        if not 0 < tail["slot_count"] <= width or tail["next_enabled"] or not complete_dom(tail_html, tail):
            return result("failed", "incomplete_tail_candidate")

        # At most two rounds here; later candidates share the same LIMIT.
        for _ in range(2):
            if self.queries + 4 > self.LIMIT:
                break
            before = self._read(page - 1, "preceding_before", ledger, page)
            if before is None:
                reason = "preceding_fetch_or_parse_failed"
                continue
            before = before[1]
            latest = document_total(before)
            if latest is None:
                reason = "current_total_missing_or_conflicting"
                continue
            if before["signature"] != previous["signature"] or not before["next_enabled"]:
                mismatch = {"missing_ids": sorted(set(previous["state_ids"]) - set(before["state_ids"])),
                            "unexpected_ids": sorted(set(before["state_ids"]) - set(previous["state_ids"]))}
                reason = "preceding_products_changed"
                continue
            if latest < ledger.slots:
                reason = "current_total_below_read_slots"
                continue
            fresh_tail = self._read(page, "tail", ledger, page)
            if fresh_tail is None:
                reason = "tail_fetch_or_parse_failed"
                continue
            html, fresh_tail = fresh_tail
            if fresh_tail["signature"] != tail["signature"] or not complete_dom(html, fresh_tail):
                mismatch = {"missing_ids": sorted(set(tail["state_ids"]) - set(fresh_tail["state_ids"])),
                            "unexpected_ids": sorted(set(fresh_tail["state_ids"]) - set(tail["state_ids"]))}
                reason = "tail_changed_or_incomplete"
                continue
            following = self._read(page + 1, "following", ledger, page)
            if following is None:
                reason = "following_fetch_or_parse_failed"
                continue
            html, following = following
            if not complete_dom(html, following):
                reason = "following_cards_incomplete"
                continue
            current_docs = (fresh_tail, following)
            if any((v.get("total") is not None or v.get("heading_totals"))
                   and document_total(v) != latest for v in current_docs):
                reason = "current_document_totals_conflict"
                continue
            new_following = following["signature"] != tail["signature"]
            if new_following:
                if (not following["slot_count"] or following["signature"] in signatures
                        or tail["slot_count"] != width or latest < ledger.slots + following["slot_count"]):
                    reason = "following_not_a_contiguous_expansion"
                    continue
            elif fresh_tail["next_enabled"] or following["next_enabled"] or latest != ledger.slots:
                reason = "current_total_or_more_control_mismatch"
                continue
            after = self._read(page - 1, "preceding_after", ledger, page)
            if (after is None or document_total(after[1]) != latest
                    or after[1]["signature"] != before["signature"] or not after[1]["next_enabled"]):
                reason = "reference_unstable_during_confirmation"
                continue
            if new_following:
                mismatch = {}
                return result("continue", "new_following_products", following["signature"])
            mismatch = {}
            return result("complete", "fresh_total_and_verified_tail_boundary")
        return result("failed", reason)


def collect_pages(fetch_page, *, product="dry", target, retries=2, sleep_s=0,
                  eligibility, on_page=None, probe_page=None, on_probe=None,
                  candidates_sufficient=None):
    """Existing listing protocol, isolated to DRY, with progress checkpoints.

    on_page receives regular attempts and separate confirmation decisions.
    probe_page must be an independent read-only fetcher; on_probe checkpoints
    each extra read without changing the normal collection cursor or rows.
    target=None captures the complete candidate listing without a numeric cap.
    Its eligibility callback must be listing-only, never a detail/API callback.
    candidates_sufficient is a pure cache-only check of an incoming block (or
    an empty block after commit). It may finish candidate capture at a target
    without claiming the category ended. Structural/first-page/repeat failures
    still fail; end-counter disagreements are irrelevant once enough is proven.
    A checkpoint error must never authorize DB insertion.
    """
    seen, fingerprints, page_log = {}, set(), []
    ledger = EndEvidence()
    confirmation = TailConfirmation(probe_page, on_probe) if probe_page is not None else None
    expected_next = None
    page = 1
    while True:
        for attempt in range(1, retries + 2):
            rows, diagnostics, new, excluded = [], {}, 0, {}
            first_validation = None
            view = {"listing_valid": False, "last_page": False, "has_next": None,
                    "shown": None, "total": None}
            decision = {"end_basis": None, "end_candidate": False}
            candidate_ready = False
            error, elapsed, extra_wait, status = None, 0, 0, None
            interrupted = None
            try:
                try:
                    html, status, elapsed, error, extra_wait = fetch_page(page)
                except Exception as exc:
                    html, error = "", type(exc).__name__
                if status == 200 and not error:
                    try:
                        rows = parse_listing_html(html, page=page, diagnostics=diagnostics)
                        view = page_view(html, rows)
                    except Exception as exc:
                        error = "parse_failed:" + type(exc).__name__
                signature = view.get("signature") or ("empty_listing", str(view.get("shown")), str(view.get("total")))
                repeated = signature in fingerprints
                if status != 200 or error:
                    error = "fetch_failed" if not str(error).startswith("parse_failed:") else error
                elif not view["listing_valid"]:
                    error = "parse_failed"
                elif any(not row.get("product_url") for row in rows if not is_banner_ad(row)):
                    error = "parse_failed"
                elif page == 1 and not view["state_coherent"]:
                    error = "first_page_inconsistent"
                elif expected_next is not None and view["signature"] != expected_next:
                    error = "confirmed_following_products_changed"
                else:
                    candidate_ready = bool(target is None and not repeated and candidates_sufficient
                                           and candidates_sufficient(rows))
                    decision = ledger.assess(page, html, view, repeated)
                    if repeated and not decision["end_basis"]:
                        error = "repeated_page"
                    elif decision.get("pagination_error"):
                        # This one historical-total error can be resolved by a
                        # separate fresh boundary proof, after normal rows save.
                        target_only = candidate_ready and decision["pagination_error"] in {
                            "counter_slot_mismatch", "counter_next_control_conflict", "total_changed_at_end"}
                        if target_only:
                            decision["unused_end_check"] = decision.pop("pagination_error")
                        if not target_only and not (confirmation and page >= 2 and not repeated
                                and decision["pagination_error"] == "total_changed_at_end"
                                and (view.get("total") or 0) > 0 and view["state_coherent"]):
                            error = decision["pagination_error"]
                    elif not view.get("signature") and not decision["end_basis"] and view["has_next"] is not True:
                        error = "parse_failed"
                if page == 1:
                    # Failed attempts remain diagnostic only: no fingerprint,
                    # eligibility/detail call, accepted row or end-ledger commit.
                    # Reuse this attempt loop; do not nest another retry loop.
                    first_validation = {**first_page_evidence(rows, view),
                        "passed": not bool(error), "reason": error,
                        "action": ("start_candidate_collection" if target is None else
                                   "start_detail_collection") if not error else
                                  "retry_first_page" if attempt <= retries else "stop_listing",
                        "details_allowed": target is not None and not bool(error),
                        "retry_wait_s": sleep_s if error and attempt <= retries else 0}
                    emit("first_page_validation", stage="first_page_validation", page=page,
                         attempt=attempt, max_attempts=retries + 1, **first_validation)
                if not error and not repeated:
                    expected_next = None
                    fingerprints.add(signature)
                    observed_at = datetime.now().isoformat(timespec="seconds")
                    for position, row in enumerate(rows, 1):
                        if target is None:
                            # Public occurrence order, independent of category slots
                            # and final eligible ranks. Preserve every parser field.
                            row = dict(row, page=page, page_position=position,
                                       listing_order=[page, position], observed_at=observed_at)
                        reason = eligibility(row)
                        if reason:
                            excluded[reason] = excluded.get(reason, 0) + 1
                            continue
                        item = str(row.get("sku_id") or "").strip()
                        if item and item not in seen:
                            row.update(sku_id=item, page=page)
                            seen[item] = row
                            new += 1
                            if target is not None and len(seen) >= target:
                                break
                    ledger.commit(page, view, decision)
                    candidate_ready = bool(target is None and candidates_sufficient
                                           and candidates_sufficient(()))
            except BaseException as exc:
                interrupted = exc
                error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            deferred_end = not error and decision.get("pagination_error") == "total_changed_at_end"
            stop = (error if interrupted or (error and attempt == retries + 1) else
                    "confirmed_candidates" if not error and candidate_ready else
                    "target_reached" if target is not None and not error and len(seen) >= target and not deferred_end else
                    "last_page" if not error and decision["end_basis"] else None)
            entry = {"page": page, "attempt": attempt, "status": status, "parsed": len(rows),
                     "phase": "listing",
                     "new_unique": new, "accepted": len(seen) if target is not None else 0,
                     "count_kind": "eligible" if target is not None else "candidate",
                     "candidate_count": len(seen) if target is None else None, "elapsed": elapsed,
                     "sponsored_extra_wait": extra_wait, "error": error, "excluded": excluded,
                     **diagnostics, **{key: value for key, value in view.items()
                                      if key not in {"state_ids", "signature"}}, **decision,
                     "raw_last_page": view["last_page"], "raw_has_next": view["has_next"],
                     "last_page": bool(decision["end_basis"]),
                     "has_next": False if decision["end_basis"] else view["has_next"]}
            if first_validation is not None:
                entry["first_page_validation"] = first_validation
            page_log.append(entry)
            emit("pagination_decision", page=page, attempt=attempt,
                 accepted=len(seen) if target is not None else 0,
                 candidate_count=len(seen) if target is None else None,
                 error=error, last_page=bool(decision["end_basis"]),
                 raw_last_page=view["last_page"], **decision)
            if on_page is not None:
                on_page(list(seen.values()), page_log, stop)
            if interrupted:
                raise interrupted
            if stop:
                return list(seen.values()), page_log, stop
            if (not error and not repeated and confirmation and page >= 2
                    and len(ledger.totals) > 1 and not view["next_enabled"]):
                # The regular block/checkpoint is already committed. Probes
                # cannot alter this ledger, rows, eligibility or detail cache.
                verdict = confirmation.confirm(ledger, html)
                expected_next = verdict.pop("next_ids")
                summary = {**entry, "phase": "tail_confirmation", "new_unique": 0, "excluded": {},
                           "confirmation": verdict,
                           "error": "tail_confirmation_failed:" + verdict["reason"]
                                    if verdict["action"] == "failed" else None,
                           "end_basis": verdict["reason"] if verdict["action"] == "complete" else None,
                           "last_page": verdict["action"] == "complete",
                           "initial_end_candidate": entry["end_candidate"],
                           "end_candidate": verdict["action"] == "complete",
                           "tail_dom_verified": True if verdict["action"] in {"complete", "continue"} else None,
                           "has_next": False if verdict["action"] == "complete" else
                                       True if verdict["action"] == "continue" else entry["has_next"],
                           "end_for_page": page}
                if "pagination_error" in summary:
                    summary["initial_pagination_error"] = summary.pop("pagination_error")
                stop = ("last_page" if verdict["action"] == "complete" else
                        summary["error"] if verdict["action"] == "failed" else
                         "target_reached" if target is not None and len(seen) >= target else None)
                page_log.append(summary)
                emit("tail_confirmation_result", page=page, attempt=attempt, accepted=len(seen), **verdict)
                if on_page is not None:
                    on_page(list(seen.values()), page_log, stop)
                if stop:
                    return list(seen.values()), page_log, stop
            if not error:
                break
            if sleep_s:
                time.sleep(sleep_s)
        page += 1
        if sleep_s:
            time.sleep(sleep_s)
