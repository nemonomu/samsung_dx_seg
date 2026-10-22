"""DRY-only rendered pagination. Shared TV/REF/LDY collectors are untouched.

Only already-observed page structure is used: the listing counter/control and
ProductListPage.products. No speculative pageCount/currentPage API fields.
"""
from __future__ import annotations

import re
import time

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


class EndEvidence:
    """A repeat is terminal only after independently verified slot coverage.

    Slots are category positions BEFORE eligibility/dedup, not accepted SKUs.
    Banner/extra DOM products never increase this ledger. Changed totals or
    variable earlier block sizes make this fallback unavailable.
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


def collect_pages(fetch_page, *, product="dry", target, retries=2, sleep_s=0,
                  eligibility, on_page=None):
    """Existing listing protocol, isolated to DRY, with progress checkpoints.

    on_page is called after every attempt with accepted rows and safe diagnostics.
    It can fail loudly; a checkpoint error must never authorize DB insertion.
    """
    seen, fingerprints, page_log = {}, set(), []
    ledger = EndEvidence()
    page = 1
    while True:
        for attempt in range(1, retries + 2):
            rows, diagnostics, new, excluded = [], {}, 0, {}
            view = {"listing_valid": False, "last_page": False, "has_next": None,
                    "shown": None, "total": None}
            decision = {"end_basis": None, "end_candidate": False}
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
                else:
                    decision = ledger.assess(page, html, view, repeated)
                    if repeated and not decision["end_basis"]:
                        error = "repeated_page"
                    elif decision.get("pagination_error"):
                        error = decision["pagination_error"]
                    elif not view.get("signature") and not decision["end_basis"] and view["has_next"] is not True:
                        error = "parse_failed"
                if not error and not repeated:
                    fingerprints.add(signature)
                    for row in rows:
                        reason = eligibility(row)
                        if reason:
                            excluded[reason] = excluded.get(reason, 0) + 1
                            continue
                        item = str(row.get("sku_id") or "").strip()
                        if item and item not in seen:
                            row.update(sku_id=item, page=page)
                            seen[item] = row
                            new += 1
                            if len(seen) >= target:
                                break
                    ledger.commit(page, view, decision)
            except BaseException as exc:
                interrupted = exc
                error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            stop = (error if interrupted or (error and attempt == retries + 1) else
                    "target_reached" if not error and len(seen) >= target else
                    "last_page" if not error and decision["end_basis"] else None)
            entry = {"page": page, "attempt": attempt, "status": status, "parsed": len(rows),
                     "new_unique": new, "accepted": len(seen), "elapsed": elapsed,
                     "sponsored_extra_wait": extra_wait, "error": error, "excluded": excluded,
                     **diagnostics, **{key: value for key, value in view.items()
                                      if key not in {"state_ids", "signature"}}, **decision,
                     "raw_last_page": view["last_page"], "raw_has_next": view["has_next"],
                     "last_page": bool(decision["end_basis"]),
                     "has_next": False if decision["end_basis"] else view["has_next"]}
            page_log.append(entry)
            emit("pagination_decision", page=page, attempt=attempt, accepted=len(seen),
                 error=error, last_page=bool(decision["end_basis"]),
                 raw_last_page=view["last_page"], **decision)
            if on_page is not None:
                on_page(list(seen.values()), page_log, stop)
            if interrupted:
                raise interrupted
            if stop:
                return list(seen.values()), page_log, stop
            if not error:
                break
            if sleep_s:
                time.sleep(sleep_s)
        page += 1
        if sleep_s:
            time.sleep(sleep_s)
