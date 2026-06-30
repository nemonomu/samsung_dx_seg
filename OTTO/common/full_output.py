"""Step09 (shared): build the DB-loadable full output per category (Kasada-free core).

For each target: category spec fields (cfg.extract_spec from datasheet/listing),
similar products (reco-core API), reviews (direct review page). Optionally supplements
PDP-only fields (cfg.PDP_SUPPLEMENT_FIELDS, e.g. LDY Bauart) via a ZenRows browser.
"""
from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import datasheet, raw_html
from common.io_util import category_output_root
from common.parsers import parse_review_html
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


def run(cfg, *, limit: int = 0, start: int = 1, pdp_supplement: str = "none", timeout: int = 45,
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
    if pdp_supplement == "zenrows" and getattr(cfg, "PDP_SUPPLEMENT_FIELDS", None):
        from common.browser import BrowserSession
        # bounded fast-fail settings: PDP supplement is best-effort (Kasada), so cap
        # waits/retries to keep per-item time predictable instead of hanging on 429.
        session = BrowserSession(
            mode="zenrows", proxy_country=proxy_country, warmup_listing_url=cfg.WARMUP_LISTING_URL,
            nav_timeout_ms=30000, detail_wait_ms=8000, settle_ms=2000, max_attempts=2, retry_backoff_ms=3000,
        )
        session.open()
        print(f"[full/{cfg.PRODUCT}] pdp supplement warmup={session.warmup_status}", flush=True)

    rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    try:
        for target in selected:
            ds = {}
            ds_status = None
            if cfg.USE_DATASHEET and (target.get("energy_datasheet_uri") or "").strip():
                body, ds_status, _ = datasheet.fetch_datasheet_bytes(target["energy_datasheet_uri"], timeout)
                ds = datasheet.parse(body)
            spec = cfg.extract_spec(target, ds, ctx)
            sku = first(ds.get("sku") if ds else None, sku_from_name(target.get("retailer_sku_name")))

            reco = fetch_similar_product_names(target.get("variation_id"), timeout=timeout)
            # no listing fallback for ratings, so retry harder to avoid a transient 0
            review_resp = fetch_html(review_url_for(target), timeout=timeout, retries=3)
            review = {}
            if review_resp.get("status") == 200:
                body = review_resp.get("body", b"")
                pid = (target.get("product_id") or "").strip() or product_id_from_url(target.get("product_url")) or str(target.get("main_rank"))
                raw_html.save(f"review_{pid}", body)  # opt-in audit copy (OTTO_SAVE_HTML)
                rp = out / "_tmp_review.html"
                rp.write_bytes(body)
                try:
                    review = parse_review_html(rp)
                finally:
                    try:
                        rp.unlink()
                    except OSError:
                        pass

            # optional PDP-only fields (e.g. LDY Bauart) via ZenRows
            if session is not None:
                from bs4 import BeautifulSoup
                pdp = session.fetch_pdp(target.get("product_url"))
                pdp_body = pdp.get("body", b"")
                if pdp_body:
                    pid = (target.get("product_id") or "").strip() or product_id_from_url(target.get("product_url")) or str(target.get("main_rank"))
                    raw_html.save(f"pdp_{pid}", pdp_body)  # opt-in audit copy (OTTO_SAVE_HTML)
                if pdp.get("detail_present"):
                    soup = BeautifulSoup(pdp_body.decode("utf-8", errors="replace"), "lxml")
                    spec.update({k: v for k, v in cfg.extract_pdp_spec(soup).items() if v})

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
            attempts.append({"rank": target.get("main_rank"), "datasheet_status": ds_status,
                             "reco": reco.get("similar_count"), "review_status": review_resp.get("status"),
                             "spec": {f: spec.get(f) for f in cfg.SPEC_FIELDS}})
            print(f"[full/{cfg.PRODUCT}] rank={target.get('main_rank')} sku={sku} spec={ {f: spec.get(f) for f in cfg.SPEC_FIELDS} } reco={reco.get('similar_count')} review={review_resp.get('status')}", flush=True)
            if detail_sleep > 0:
                time.sleep(detail_sleep)
    finally:
        if session is not None:
            session.close()

    output_csv = out / "otto_full_output.csv"
    write_output(output_csv, fields, rows)
    manifest = {
        "run_type": "full_output", "product": cfg.PRODUCT,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "batch_id": run_meta["batch_id"], "output_rows": len(rows),
        "spec_fields": list(cfg.SPEC_FIELDS), "use_datasheet": cfg.USE_DATASHEET,
        "pdp_supplement": pdp_supplement, "output": str(output_csv), "attempts": attempts,
    }
    write_json(out / "step09_full_output_manifest.json", manifest)
    print(f"[full/{cfg.PRODUCT}] output={output_csv} rows={len(rows)}")
    return manifest
