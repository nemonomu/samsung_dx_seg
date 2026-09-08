"""Step09 (shared): build the DB-loadable full output per category (Kasada-free core).

For each target: category spec fields (cfg.extract_spec from datasheet/listing),
similar products (reco-core API), reviews (direct review page). Optionally supplements
PDP-only fields (cfg.PDP_SUPPLEMENT_FIELDS, e.g. LDY Bauart) via a ZenRows browser.
"""
from __future__ import annotations

import csv
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import datasheet, raw_html
from common.io_util import category_output_root
from common.parsers import format_detailed_review_content, parse_review_html

REVIEW_DETAIL_LIMIT = 20  # detailed_review_content collects up to this many written reviews
SUMMARY_RANK_LIMIT = 20  # the client requirement limits review-summary QA/retries to main top 20
SUMMARY_CONTENT_ATTEMPTS = 4  # bounded content retries for intermittent summary omission on valid HTTP 200 pages
SUMMARY_NO_SOURCE_CONFIRM_ATTEMPTS = 3  # do not classify one marker-free response as a genuine source NULL
SUMMARY_RETRY_SLEEP_RANGE_SECONDS = (0.8, 1.5)
SUMMARY_UI_TEXT = (
    "Das sagen unsere Kunden",
    "Bewertungen ansehen",
    "Ist diese Zusammenfassung hilfreich?",
    "Nicht hilfreich",
)
QA_FILL_WARN = 0.90  # spec-field fill rate below this logs a loud [QA][WARN] (advisory only)
from common.reco import fetch_similar_product_names

BASE_HEAD = [
    "account_name", "product", "country", "page_type", "crawl_strdatetime", "calendar_week", "batch_id",
    "main_rank", "bsr_rank", "item", "product_url", "retailer_sku_name",
    "final_sku_price", "original_sku_price", "savings", "sku_popularity", "sku_status",
    "discount_type", "delivery_availability", "sku",
]
BASE_TAIL = [
    "retailer_sku_name_similar", "star_rating", "count_of_star_ratings", "count_of_reviews",
    "recommendation_intent", "summarized_review_content", "detailed_review_content",
]
REVIEW_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.otto.de/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}


def final_fields(cfg) -> list[str]:
    return BASE_HEAD + list(cfg.SPEC_FIELDS) + BASE_TAIL


def sku_from_name(name: str | None) -> str | None:
    if not name:
        return None
    for tok in re.findall(r"[A-Z0-9][A-Z0-9/_.\-]{4,}", name):
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return tok
    return None


def product_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"-((?:C)?[A-Z0-9]+)/(?:\?|$)", url, re.I)
    return m.group(1) if m else None


def review_url_for(target: dict[str, Any]) -> str | None:
    pid = (target.get("product_id") or "").strip() or product_id_from_url(target.get("product_url"))
    return f"https://www.otto.de/kundenbewertungen/{pid}/" if pid else None


def fetch_html(url: str, timeout: int = 45, retries: int = 1) -> dict[str, Any]:
    last = {"status": None, "body": b"", "error": "missing_url"}
    if not url:
        return last
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(url, headers=REVIEW_HEADERS, method="GET"), timeout=timeout) as r:
                return {"status": r.status, "body": r.read(), "error": None}
        except HTTPError as exc:
            last = {"status": exc.code, "body": exc.read(), "error": repr(exc)}
            if exc.code not in (429, 503):
                return last
        except URLError as exc:
            last = {"status": None, "body": b"", "error": repr(exc)}
        if attempt < retries:
            time.sleep(2)
    return last


def first(*values):
    for v in values:
        if v not in (None, ""):
            return v
    return None


def has_value(value: Any) -> bool:
    return bool(str(value or "").strip())


def missing_spec_fields(spec: dict[str, Any], cfg) -> list[str]:
    return [f for f in cfg.SPEC_FIELDS if not has_value(spec.get(f))]


def _policy_null_fields(spec: dict[str, Any]) -> set[str]:
    raw = spec.get("_policy_null_fields") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(field) for field in raw if str(field).strip()}


def pdp_supplement_plan(spec: dict[str, Any], cfg, pdp_supplement: str,
                        missing_fields: list[str]) -> dict[str, Any]:
    policy_null = _policy_null_fields(spec)
    allowed = [
        str(field)
        for field in (getattr(cfg, "PDP_SUPPLEMENT_FIELDS", []) or [])
        if str(field) in cfg.SPEC_FIELDS
    ]
    target = [
        field for field in allowed
        if field not in policy_null and not has_value(spec.get(field))
    ]
    skipped_reason = None
    if pdp_supplement != "zenrows":
        skipped_reason = "disabled"
    elif not allowed:
        skipped_reason = "no_allowed_fields"
    elif not target:
        skipped_reason = (
            "policy_null_only"
            if missing_fields and all(field in policy_null for field in missing_fields)
            else "no_allowed_missing_fields"
        )
    return {
        "policy_null_fields": sorted(policy_null),
        "allowed_fields": allowed,
        "target_fields": target,
        "skipped_reason": skipped_reason,
    }


def collect_review(base_url: str | None, out: Path, save_pid: str, timeout: int = 45,
                   require_summary: bool = False,
                   summary_attempts: int = SUMMARY_CONTENT_ATTEMPTS) -> dict[str, Any]:
    """Fetch the review page and follow ?page=N until REVIEW_DETAIL_LIMIT written reviews
    are gathered (OTTO paginates reviews). Rating summary / recommendation come from page 1.
    Returns the page-1 parse dict with reviews/detailed_review_content spanning all pages."""
    if not base_url:
        return {}
    rp = out / "_tmp_review.html"

    def _parse(body: bytes) -> dict[str, Any]:
        rp.write_bytes(body)
        try:
            return parse_review_html(rp)
        finally:
            try:
                rp.unlink()
            except OSError:
                pass

    max_summary_attempts = max(1, summary_attempts) if require_summary else 1
    page1: dict[str, Any] | None = None
    resp: dict[str, Any] = {"status": None, "body": b"", "error": "not_requested"}
    summary_eligible = False
    summary_container_present = False
    attempts_made = 0
    for attempt in range(1, max_summary_attempts + 1):
        attempts_made = attempt
        resp = fetch_html(base_url, timeout=timeout, retries=3 if attempt == 1 else 1)
        if resp.get("status") != 200:
            break
        body = resp.get("body", b"")
        raw_html.save(f"review_{save_pid}_summary_a{attempt}", body)
        parsed = _parse(body)
        page1 = parsed
        summary_container_present = summary_container_present or bool(
            parsed.get("summary_container_present")
        )
        summary_eligible = summary_eligible or bool(
            parsed.get("summary_placeholder_present")
            or parsed.get("summary_container_present")
            or parsed.get("summary_rendered")
        )
        if parsed.get("summary_rendered"):
            break
        if not require_summary:
            break
        if not summary_eligible and attempt >= min(
            SUMMARY_NO_SOURCE_CONFIRM_ATTEMPTS, max_summary_attempts
        ):
            break
        if attempt < max_summary_attempts:
            time.sleep(random.uniform(*SUMMARY_RETRY_SLEEP_RANGE_SECONDS))

    if page1 is None:
        return {
            "_review_status": resp.get("status"),
            "_summary_required": require_summary,
            "_summary_attempts": attempts_made,
            "_summary_eligible": False,
            "_summary_rendered": False,
            "_summary_container_present": False,
            "_summary_item_count": 0,
            "_summary_failure_reason": (
                f"http_status={resp.get('status')}" if resp.get("status") is not None
                else resp.get("error") or "request_failed"
            ),
        }

    raw_html.save(f"review_{save_pid}", resp.get("body", b""))
    summary_rendered = bool(page1.get("summary_rendered"))
    if summary_rendered:
        summary_failure_reason = None
    elif summary_container_present:
        summary_failure_reason = "selector_mismatch"
    elif summary_eligible:
        summary_failure_reason = "eligible_not_rendered"
    else:
        summary_failure_reason = "no_source"
    page1["_review_status"] = resp.get("status")
    page1["_summary_required"] = require_summary
    page1["_summary_attempts"] = attempts_made
    page1["_summary_eligible"] = summary_eligible
    page1["_summary_rendered"] = summary_rendered
    page1["_summary_container_present"] = summary_container_present
    page1["_summary_item_count"] = page1.get("summary_item_count") or 0
    page1["_summary_failure_reason"] = summary_failure_reason

    reviews = list(page1.get("reviews") or [])
    last_page = page1.get("last_page") or 1
    seen = {r.get("review_id") for r in reviews if r.get("review_id")}
    written = lambda: sum(1 for r in reviews if r.get("review_text"))
    page = 1
    sep = "&" if "?" in base_url else "?"
    while written() < REVIEW_DETAIL_LIMIT and page < last_page:
        page += 1
        nxt = fetch_html(f"{base_url}{sep}page={page}", timeout=timeout, retries=2)
        if nxt.get("status") != 200:
            break
        raw_html.save(f"review_{save_pid}_p{page}", nxt.get("body", b""))
        more = _parse(nxt.get("body", b"")).get("reviews") or []
        added = 0
        for r in more:
            rid = r.get("review_id")
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            reviews.append(r)
            added += 1
        if added == 0:
            break
    page1["reviews"] = reviews
    page1["review_text_rows"] = written()
    page1["detailed_review_content"] = format_detailed_review_content(reviews, limit=REVIEW_DETAIL_LIMIT)
    return page1


def write_output(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = [k for r in rows for k in r if k not in fields]
    seen, ordered_extra = set(), []
    for k in extra:
        if k not in seen:
            seen.add(k); ordered_extra.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields + ordered_extra)
        w.writeheader()
        w.writerows(rows)


def run(cfg, *, limit: int = 0, start: int = 1, pdp_supplement: str = "zenrows", timeout: int = 45,
        detail_sleep: float = 1.0, proxy_country: str = "de") -> dict[str, Any]:
    from common.io_util import write_json

    out = category_output_root(cfg.PRODUCT.lower())
    targets = list(csv.DictReader(open(out / "otto_final_targets.csv", encoding="utf-8-sig")))
    start_i = max(start, 1) - 1
    end_i = len(targets) if limit <= 0 else min(len(targets), start_i + limit)
    selected = targets[start_i:end_i]

    now = datetime.now()
    run_meta = {
        "crawl_strdatetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "batch_id": "o_" + now.strftime("%Y%m%d_%H%M%S"),
        "calendar_week": "w" + str(now.isocalendar().week),
    }
    fields = final_fields(cfg)

    # one-time category context, built from the selected targets (e.g. LDY Bauart map
    # via the Kasada-free /vergleich/ comparison page). prepare_context may accept the
    # target list; fall back to a no-arg call for older configs.
    ctx = {}
    if hasattr(cfg, "prepare_context"):
        try:
            ctx = cfg.prepare_context(selected)
        except TypeError:
            ctx = cfg.prepare_context()

    session = None

    def ensure_pdp_session():
        nonlocal session
        if pdp_supplement != "zenrows":
            return None
        if session is None:
            from common.browser import BrowserSession
            # Lazy and bounded: only rows with missing spec/PDP supplement fields pay the
            # ZenRows browser cost. One warmed browser is then reused for the run.
            session = BrowserSession(
                mode="zenrows", proxy_country=proxy_country, warmup_listing_url=cfg.WARMUP_LISTING_URL,
                nav_timeout_ms=90000, detail_wait_ms=20000, settle_ms=3000, max_attempts=2, retry_backoff_ms=4000,
            )
            session.open()
            print(f"[full/{cfg.PRODUCT}] pdp supplement warmup={session.warmup_status}", flush=True)
        return session

    rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    try:
        for target in selected:
            ds = {}
            ds_status = None
            if cfg.USE_DATASHEET and (target.get("energy_datasheet_uri") or "").strip():
                body, ds_status, _ = datasheet.fetch_datasheet_bytes(target["energy_datasheet_uri"], timeout)
                ds = datasheet.parse(body)
            # sku first: datasheet Modellkennung, then a category hook (e.g. /vergleich/
            # Modellbezeichnung), then the name-token heuristic. Passed to extract_spec so
            # spec extractors can reuse it (e.g. TV model-aware source parsing).
            ctx_sku = cfg.extract_sku(target, ds, ctx) if hasattr(cfg, "extract_sku") else None
            sku = first(ds.get("sku") if ds else None, ctx_sku, sku_from_name(target.get("retailer_sku_name")))
            spec = cfg.extract_spec(target, ds, ctx, sku=sku)

            reco = fetch_similar_product_names(target.get("variation_id"), timeout=timeout)
            pid = (target.get("product_id") or "").strip() or product_id_from_url(target.get("product_url")) or str(target.get("main_rank"))
            try:
                main_rank = int(str(target.get("main_rank") or "").strip())
            except ValueError:
                main_rank = None
            summary_required = main_rank is not None and 1 <= main_rank <= SUMMARY_RANK_LIMIT
            review = collect_review(
                review_url_for(target), out, pid, timeout=timeout,
                require_summary=summary_required,
            )
            review_resp = {"status": review.get("_review_status") if review else None}

            missing_before_pdp = missing_spec_fields(spec, cfg)
            missing_after_pdp = list(missing_before_pdp)
            pdp_status = None
            pdp_nav_status = None
            pdp_error = None
            pdp_attempts = 0
            pdp_elapsed_seconds = None
            pdp_final_url = None
            pdp_wait_state = None
            pdp_detail_present = None
            pdp_recovered_fields: list[str] = []
            pdp_failure_reason = None
            plan = pdp_supplement_plan(spec, cfg, pdp_supplement, missing_before_pdp)
            policy_null_fields = plan["policy_null_fields"]
            supplement_fields = plan["allowed_fields"]
            pdp_target_fields = plan["target_fields"]
            pdp_skipped_reason = plan["skipped_reason"]
            needs_pdp = pdp_supplement == "zenrows" and bool(pdp_target_fields)
            if needs_pdp:
                try:
                    active_session = ensure_pdp_session()
                    if active_session is not None:
                        from bs4 import BeautifulSoup
                        pdp = active_session.fetch_pdp(target.get("product_url"))
                        pdp_status = pdp.get("status")
                        pdp_nav_status = pdp.get("nav_status")
                        pdp_error = pdp.get("error")
                        pdp_attempts = pdp.get("attempts") or 0
                        pdp_elapsed_seconds = pdp.get("elapsed_seconds")
                        pdp_final_url = pdp.get("final_url")
                        pdp_wait_state = pdp.get("wait_state")
                        pdp_detail_present = pdp.get("detail_present")
                        pdp_body = pdp.get("body", b"")
                        if pdp_body:
                            pid = (target.get("product_id") or "").strip() or product_id_from_url(target.get("product_url")) or str(target.get("main_rank"))
                            raw_html.save(f"pdp_{pid}", pdp_body)  # opt-in audit copy (OTTO_SAVE_HTML)
                        if pdp.get("detail_present"):
                            soup = BeautifulSoup(pdp_body.decode("utf-8", errors="replace"), "lxml")
                            pdp_spec = cfg.extract_pdp_spec(soup)
                            for key in pdp_target_fields:
                                value = pdp_spec.get(key)
                                if not has_value(value):
                                    continue
                                if not has_value(spec.get(key)):
                                    pdp_recovered_fields.append(key)
                                spec[key] = value
                except Exception as exc:  # noqa: BLE001
                    pdp_error = type(exc).__name__ + ": " + str(exc)
                    print(f"[full/{cfg.PRODUCT}][WARN] pdp supplement failed rank={target.get('main_rank')} error={pdp_error}", flush=True)
                missing_after_pdp = missing_spec_fields(spec, cfg)
                remaining_pdp_fields = [
                    field for field in pdp_target_fields
                    if not has_value(spec.get(field))
                ]
                if remaining_pdp_fields:
                    if pdp_error:
                        pdp_failure_reason = pdp_error
                    elif pdp_status != 200:
                        pdp_failure_reason = f"http_status={pdp_status}"
                    elif not pdp_detail_present:
                        pdp_failure_reason = "detail_not_present"
                    else:
                        pdp_failure_reason = "no_allowed_fields_recovered"

            # star rating: per-variation, from the review page ONLY (Kasada-free, matches
            # the actual product page). The crocotile listing aggregate over-counts (it is a
            # model-level number projected onto every variation tile), so it is NOT used.
            # No rating block on the page (or a failed fetch) -> 0 / 0.0.
            count_reviews = review.get("rating_count") or 0
            star_rating = review.get("average_rating") or "0.0"
            row = {
                "account_name": cfg.ACCOUNT_NAME, "product": cfg.PRODUCT, "country": cfg.COUNTRY,
                "page_type": target.get("page_type") or "main",
                **run_meta,
                "main_rank": target.get("main_rank"), "bsr_rank": target.get("bsr_rank"),
                "item": target.get("product_id"), "product_url": target.get("product_url"),
                "retailer_sku_name": target.get("retailer_sku_name"),
                "final_sku_price": target.get("final_sku_price"), "original_sku_price": target.get("original_sku_price"),
                "savings": target.get("savings"), "sku_popularity": target.get("sku_popularity"),
                "sku_status": target.get("sku_status"), "discount_type": target.get("discount_type"),
                "delivery_availability": target.get("delivery_availability"), "sku": sku,
                "retailer_sku_name_similar": reco.get("retailer_sku_name_similar"),
                "star_rating": star_rating,
                "count_of_star_ratings": count_reviews, "count_of_reviews": count_reviews,
                "recommendation_intent": review.get("recommendation_intent"),
                "summarized_review_content": review.get("summarized_review_content"),
                "detailed_review_content": review.get("detailed_review_content"),
            }
            for f in cfg.SPEC_FIELDS:
                row[f] = spec.get(f)
            rows.append(row)
            attempts.append({"rank": target.get("main_rank"), "item": target.get("product_id"),
                             "product_url": target.get("product_url"), "datasheet_status": ds_status,
                             "reco": reco.get("similar_count"), "review_status": review_resp.get("status"),
                             "summary_required": review.get("_summary_required", summary_required),
                             "summary_attempts": review.get("_summary_attempts", 0),
                             "summary_eligible": review.get("_summary_eligible", False),
                             "summary_rendered": review.get("_summary_rendered", False),
                             "summary_container_present": review.get("_summary_container_present", False),
                             "summary_item_count": review.get("_summary_item_count", 0),
                             "summary_failure_reason": review.get("_summary_failure_reason"),
                             "spec": {f: spec.get(f) for f in cfg.SPEC_FIELDS},
                             "missing_spec_before_pdp": missing_before_pdp,
                             "missing_spec_after_pdp": missing_after_pdp,
                             "policy_null_fields": policy_null_fields,
                             "pdp_supplement_allowed_fields": supplement_fields,
                             "pdp_supplement_target_fields": pdp_target_fields,
                             "pdp_supplement_skipped_reason": pdp_skipped_reason,
                             "pdp_supplement_status": pdp_status,
                             "pdp_supplement_nav_status": pdp_nav_status,
                             "pdp_supplement_detail_present": pdp_detail_present,
                             "pdp_supplement_attempts": pdp_attempts,
                             "pdp_supplement_elapsed_seconds": pdp_elapsed_seconds,
                             "pdp_supplement_wait_state": pdp_wait_state,
                             "pdp_supplement_final_url": pdp_final_url,
                             "pdp_supplement_recovered_fields": pdp_recovered_fields,
                             "pdp_supplement_failure_reason": pdp_failure_reason,
                             "pdp_supplement_error": pdp_error})
            print(f"[full/{cfg.PRODUCT}] rank={target.get('main_rank')} sku={sku} spec={ {f: spec.get(f) for f in cfg.SPEC_FIELDS} } reco={reco.get('similar_count')} review={review_resp.get('status')} summary={review.get('_summary_rendered', False)} attempts={review.get('_summary_attempts', 0)} reason={review.get('_summary_failure_reason')}", flush=True)
            if detail_sleep > 0:
                time.sleep(detail_sleep)
    finally:
        if session is not None:
            session.close()

    output_csv = out / "otto_full_output.csv"
    write_output(output_csv, fields, rows)

    # Read-only fill-rate QA: never changes a collected value, only reports coverage so a
    # silent regression (e.g. a truncated category set nulling loading_type) is visible in
    # the log + manifest instead of passing unnoticed. WARN threshold is advisory.
    qa_fields = list(cfg.SPEC_FIELDS) + ["sku"]
    n = len(rows) or 1
    fill_rate = {f: round(sum(1 for r in rows if str(r.get(f) or "").strip()) / n, 4) for f in qa_fields}
    print(f"[full/{cfg.PRODUCT}][QA] rows={len(rows)} fill_rate=" +
          ", ".join(f"{f}={v:.1%}" for f, v in fill_rate.items()), flush=True)
    low = [f for f in cfg.SPEC_FIELDS if fill_rate[f] < QA_FILL_WARN]
    if low:
        print(f"[full/{cfg.PRODUCT}][QA][WARN] low coverage (<{QA_FILL_WARN:.0%}): " +
              ", ".join(f"{f}={fill_rate[f]:.1%}" for f in low) +
              " - check /vergleich/, EPREL, PDP fallback or category throttling before trusting this batch", flush=True)
    missing_spec_counts = {f: sum(1 for r in rows if not has_value(r.get(f))) for f in cfg.SPEC_FIELDS}
    rows_with_missing_specs = sum(
        1 for r in rows if any(not has_value(r.get(f)) for f in cfg.SPEC_FIELDS)
    )
    summary_top20 = [a for a in attempts if a.get("summary_required")]
    summary_eligible_count = sum(1 for a in summary_top20 if a.get("summary_eligible"))
    summary_rendered_count = sum(1 for a in summary_top20 if a.get("summary_rendered"))
    summary_eligible_missing = sum(
        1 for a in summary_top20 if a.get("summary_eligible") and not a.get("summary_rendered")
    )
    summary_selector_mismatch = sum(
        1 for a in summary_top20 if a.get("summary_failure_reason") == "selector_mismatch"
    )
    summary_http_failed = sum(
        1 for a in summary_top20
        if str(a.get("summary_failure_reason") or "").startswith("http_status=")
    )
    summary_ui_contamination = sum(
        1 for row in rows
        if any(text in str(row.get("summarized_review_content") or "") for text in SUMMARY_UI_TEXT)
    )
    summary_qa = {
        "rank_limit": SUMMARY_RANK_LIMIT,
        "checked": len(summary_top20),
        "eligible": summary_eligible_count,
        "rendered": summary_rendered_count,
        "eligible_missing": summary_eligible_missing,
        "no_source": sum(1 for a in summary_top20 if a.get("summary_failure_reason") == "no_source"),
        "selector_mismatch": summary_selector_mismatch,
        "http_failed": summary_http_failed,
        "ui_text_contamination": summary_ui_contamination,
    }
    print(
        f"[full/{cfg.PRODUCT}][QA] review_summary_top20 checked={summary_qa['checked']} "
        f"eligible={summary_qa['eligible']} rendered={summary_qa['rendered']} "
        f"eligible_missing={summary_qa['eligible_missing']} no_source={summary_qa['no_source']} "
        f"selector_mismatch={summary_qa['selector_mismatch']} http_failed={summary_qa['http_failed']} "
        f"ui_text_contamination={summary_qa['ui_text_contamination']}",
        flush=True,
    )
    if summary_eligible_missing:
        print(
            f"[full/{cfg.PRODUCT}][QA][WARN] summarized_review_content eligible but missing "
            f"{summary_eligible_missing}/{summary_eligible_count} after {SUMMARY_CONTENT_ATTEMPTS} attempts",
            flush=True,
        )
    summary_anomalies = [
        {
            "rank": a.get("rank"),
            "item": a.get("item"),
            "product_url": a.get("product_url"),
            "review_status": a.get("review_status"),
            "summary_attempts": a.get("summary_attempts"),
            "summary_eligible": a.get("summary_eligible"),
            "summary_rendered": a.get("summary_rendered"),
            "summary_container_present": a.get("summary_container_present"),
            "summary_item_count": a.get("summary_item_count"),
            "summary_failure_reason": a.get("summary_failure_reason"),
        }
        for a in summary_top20
        if (a.get("summary_attempts") or 0) > 1 or a.get("summary_failure_reason")
    ]
    summary_anomaly_output = out / "otto_review_summary_anomalies.json"
    write_json(summary_anomaly_output, {
        "product": cfg.PRODUCT,
        "batch_id": run_meta["batch_id"],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary_qa": summary_qa,
        "anomalies": summary_anomalies,
    })

    manifest = {
        "run_type": "full_output", "product": cfg.PRODUCT,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_id": run_meta["batch_id"], "output_rows": len(rows),
        "spec_fields": list(cfg.SPEC_FIELDS), "use_datasheet": cfg.USE_DATASHEET,
        "pdp_supplement": pdp_supplement, "output": str(output_csv), "attempts": attempts,
        "fill_rate": fill_rate,
        "missing_spec_counts": missing_spec_counts,
        "rows_with_missing_specs": rows_with_missing_specs,
        "summary_qa": summary_qa,
        "summary_anomaly_output": str(summary_anomaly_output),
    }
    write_json(out / "step09_full_output_manifest.json", manifest)
    print(f"[full/{cfg.PRODUCT}] output={output_csv} rows={len(rows)}")
    return manifest
