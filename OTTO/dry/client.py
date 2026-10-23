"""Bounded direct HTTP. No browser, raw captures, review pagination or extra APIs."""
import io
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from common import compare, datasheet, listing, review_rating
from dry.config import CATEGORY_LABELS, COMPARE_LABELS, SEARCH_TERM, SEARCH_URL, TYPE_LABELS, DryError
from dry.fields import capacity, capacity_label, public_product_url, text


class RequestFailure(DryError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.status = status


class Client:
    def __init__(self, events, timeout=30, attempts=3, pause=0.25):
        self.events, self.timeout, self.attempts, self.pause = events, timeout, attempts, pause
        self.compare_cache = {}
        self.tile_http_400_failures = 0  # Run total for diagnostics, never the abort counter.

    def fetch(self, url, endpoint, *, kind="html", headers=None, attempts=None):
        attempts = attempts or self.attempts
        for attempt in range(1, attempts + 1):
            started, status = time.monotonic(), None
            self.events.emit("request_start", endpoint=endpoint, attempt=attempt, max_attempts=attempts)
            try:
                with self.events.activity("request:" + endpoint, attempt=attempt):
                    request = Request(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
                        "Accept-Language": "de-DE,de;q=0.9", "Referer": SEARCH_URL,
                        "Accept": "application/json" if kind == "json" else "*/*", **(headers or {})})
                    with urlopen(request, timeout=self.timeout) as response:
                        status = response.status
                        final_url = response.geturl()
                        if status != 200:
                            raise RequestFailure("unexpected_http_status", status)
                        body = bytearray()
                        max_bytes = 20_000_000 if kind == "pdf" else 8_000_000
                        while True:
                            if time.monotonic() - started > self.timeout:
                                raise RequestFailure("response_deadline", status)
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise RequestFailure("response_too_large", status)
                        body = bytes(body)
                payload = json.loads(body.decode("utf-8")) if kind == "json" else body
                self.events.emit("request_end", endpoint=endpoint, status=status, attempt=attempt,
                                 seconds=round(time.monotonic() - started, 2), outcome="success")
                if self.pause:
                    time.sleep(self.pause)
                return payload, {"status": status, "final_url": final_url}
            except (HTTPError, URLError, OSError, ValueError, RequestFailure) as exc:
                status = getattr(exc, "code", None) if isinstance(exc, HTTPError) else getattr(exc, "status", status)
                reason = str(exc) if isinstance(exc, RequestFailure) else type(exc).__name__
                self.events.emit("request_end", endpoint=endpoint, status=status, attempt=attempt,
                                 seconds=round(time.monotonic() - started, 2), outcome="failed", reason=reason)
                retryable = status is None or status in (200, 408, 425, 429) or status >= 500
                if not retryable or attempt == attempts:
                    raise RequestFailure("request_failed:" + endpoint + ":" + reason, status) from None
                time.sleep(min(attempt, 3))
        raise RequestFailure("request_failed:" + endpoint)

    def page(self, page, offset):
        data, _ = self.fetch(listing.build_everglades_url(SEARCH_TERM, offset), "listing", kind="json")
        intents = data.get("intents") if isinstance(data, dict) else None
        ranked = [it for it in intents or [] if isinstance(it, dict) and it.get("intent") == "ranked"]
        if len(ranked) != 1 or not isinstance(ranked[0].get("products"), list):
            raise RequestFailure("listing_ranked_contract_changed")
        products = ranked[0]["products"]
        if any(not isinstance(p, dict) or not p.get("id") for p in products):
            raise RequestFailure("listing_product_contract_changed")
        count = ranked[0].get("count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise RequestFailure("listing_count_invalid")
        if not products and count is not None and offset < count:
            raise RequestFailure("listing_empty_before_reported_end")
        # Pure LDY slot composition; immediately discard unrelated output fields.
        rows = [{k: row.get(k) for k in ("product_id", "variation_id", "product_url", "retailer_sku_name")}
                for row in listing.compose_rows(page, offset, data)]
        for row in rows:
            if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(row.get(k) or "")) for k in ("product_id", "variation_id")):
                raise RequestFailure("listing_identifier_missing_or_invalid")
            row["product_url"] = public_product_url(row["product_url"], row["variation_id"])
        # Advance by displayed organic positions, like LDY (never skip undisplayed results).
        sponsored = listing.products_for_intent(data, "sponsored")
        consumed = min(len(products), listing.LISTING_POSITIONS_PER_PAGE - min(len(sponsored), len(listing.SPONSORED_SLOT_POSITIONS)))
        end = not products or (count is not None and offset + consumed >= count)
        fingerprint = tuple(str(p["id"]) + ":" + str(p.get("bestVariationId")) for p in products)
        return rows, consumed, end, fingerprint

    def tiles(self, ids, _state=None):
        if not ids:
            return {}
        # LDY scope: a new top-level batch starts fresh; recursive splits share state.
        state = _state if _state is not None else {"bad_ids": 0}
        self.events.emit("tile_targets", variation_ids=ids)
        try:
            data, _ = self.fetch(listing.CROCOTILE_URL + "?variationIds=" + ",".join(quote(v, safe="") for v in ids),
                                 "tiles", kind="json", headers=listing.CROCOTILE_HEADERS)
        except RequestFailure as exc:
            if exc.status != 400:
                raise
            if len(ids) > 1:
                mid = len(ids) // 2
                return {**self.tiles(ids[:mid], state), **self.tiles(ids[mid:], state)}
            state["bad_ids"] += 1
            self.tile_http_400_failures += 1
            limit = listing.CROCOTILE_SINGLETON_FAILURE_LIMIT
            abort = state["bad_ids"] >= limit
            self.events.emit("failure", stage="listing", variation_id=ids[0], reason="tile_id_rejected",
                             status=400, batch_failures=state["bad_ids"], total_failures=self.tile_http_400_failures,
                             failure_limit=limit, outcome="aborted_systemic_400" if abort else "skipped_id")
            if abort:
                raise RequestFailure("systemic_tile_rejection", 400) from None
            return {}
        if not isinstance(data, list) or any(not isinstance(t, dict) for t in data):
            raise RequestFailure("tile_contract_changed")
        result = {}
        for tile in data:
            vid = str(tile.get("variationId") or "")
            if vid in ids:
                result[vid] = minimal_tile(tile)
        return result

    def comparison(self, ids):
        needed = [v for v in dict.fromkeys(ids) if v not in self.compare_cache]
        if needed:
            self.events.emit("comparison_targets", variation_ids=needed)
            url = compare.VERGLEICH_URL + "?" + urlencode({"variationIds": ",".join(needed)})
            try:
                body, response = self.fetch(url, "comparison")
                if review_rating.response_problem(response, url):
                    raise RequestFailure("comparison_redirect")
                soup = BeautifulSoup(body, "lxml")
                names = compare._names_from_html(soup, set(needed))
                values = {label: compare._row_values(soup, label) for label in COMPARE_LABELS}
                for vid in needed:
                    chars = {label: vals[vid] for label, vals in values.items() if vals.get(vid)}
                    if names.get(vid):
                        chars["_name"] = names[vid]
                    if not chars:
                        chars["_error"] = "comparison_option_missing"
                    self.compare_cache[vid] = chars
            except RequestFailure as exc:
                for vid in needed:
                    self.compare_cache[vid] = {"_error": str(exc)}
        return {v: self.compare_cache[v] for v in ids}

    def pdf(self, uri, *, need_sku, need_capacity):
        if not uri:
            return {"_error": "own_pdf_link_missing"}
        parsed = urlsplit(uri)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return {"_error": "own_pdf_link_invalid"}
        try:
            body, _ = self.fetch(uri, "product_pdf", kind="pdf")
            if not body.startswith(b"%PDF"):
                raise RequestFailure("pdf_document_invalid")
            with self.events.activity("pdf_parse"):
                return parse_pdf(body, need_sku=need_sku, need_capacity=need_capacity)
        except RequestFailure as exc:
            return {"_error": str(exc)}
        except Exception as exc:
            return {"_error": "pdf_parse:" + type(exc).__name__}

    def rating(self, product_id):
        url = f"https://www.otto.de/kundenbewertungen/{quote(product_id, safe='')}/"
        retained = {"average_rating": None, "rating_count": None, "rating_state": "failed"}
        for attempt in range(1, self.attempts + 1):
            try:
                body, response = self.fetch(url, "rating", attempts=1)
                problem = review_rating.response_problem(response, url)
                if not problem:
                    soup = BeautifulSoup(body, "lxml")
                    problem = review_rating.product_problem({"review_product_ids": review_rating.review_product_ids(soup)}, url)
                if problem:
                    raise RequestFailure(problem)
                parsed = review_rating.parse_rating(soup)
                for field in ("average_rating", "rating_count"):
                    if parsed[field] is not None:
                        retained[field] = parsed[field]
                retained["rating_state"] = parsed["rating_state"]
                retained["rating_reason"] = parsed.get("rating_reason")
                if parsed["rating_state"] != "failed":
                    retained = parsed
                elif retained["average_rating"] is not None and retained["rating_count"] == 0:
                    # Match LDY's post-merge guard; partial responses remain failed.
                    retained["rating_count"] = None
                    retained["rating_reason"] = "merged_positive_average_without_ratings"
            except RequestFailure as exc:
                retained["rating_reason"] = str(exc)
            self.events.emit("rating", item=product_id, attempt=attempt, **retained)
            if retained["rating_state"] != "failed":
                break
            if attempt < self.attempts:
                time.sleep(min(attempt, 3))
        return retained


def minimal_tile(tile):
    price, sale, title = tile.get("price") or {}, tile.get("saleTags") or {}, tile.get("title") or {}
    name = text(title.get("shortened")) or text(tile.get("name"))
    brand = text(tile.get("brand"))
    if name and brand and not name.casefold().startswith(brand.casefold()):
        name = brand + " " + name
    infos = {i["label"]: i.get("value") for i in tile.get("topInfos") or []
             if isinstance(i, dict) and i.get("label") and
             (capacity_label(i["label"]) or i["label"] in (*TYPE_LABELS, *CATEGORY_LABELS))}
    energies = tile.get("energyLabels") or []
    pdf_uri = energies[0].get("datasheetUri") if energies and isinstance(energies[0], dict) else None
    euro = lambda value: (text(value) if "€" in text(value) or "EUR" in text(value) else text(value) + " €") if text(value) else None
    return dict(retailer_sku_name=name or None, top_infos=infos, pdf_uri=pdf_uri,
                final_sku_price=euro(price.get("retailPrice")),
                original_sku_price=euro(price.get("suggestedRetailPrice") or price.get("comparativePrice")),
                savings=text(sale.get("discount")) or None)


def parse_pdf(body, *, need_sku, need_capacity):
    """Read only text for missing model/capacity; no OCR, energy parsing or disk capture."""
    import pdfplumber
    result = {}
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        for page in pdf.pages[:3]:
            content = page.extract_text() or ""
            if need_sku and not result.get("sku"):
                result["sku"] = datasheet._sku({}, content)
                if result.get("sku"):
                    result["sku_raw"] = result["sku"]  # extracted model only, not the entire PDF row
            if need_capacity and not result.get("capacity"):
                for line in content.splitlines():
                    if re.search(r"wasch|gewicht|weight", line, re.I):
                        continue
                    match = re.search(r"(?:Nennkapazit[äa]t|Rated capacity|Trockenkapazit[äa]t)(?:\s*\([a-z]\))?\s*(?:\(?(?:kg)\)?)?\s*[:=]?\s*(\d+(?:[.,]\d+)?)", line, re.I)
                    if match:
                        result["capacity"] = capacity(match[1], labelled=True)
                        result["capacity_raw"] = match[1]
                        break
            if (not need_sku or result.get("sku")) and (not need_capacity or result.get("capacity")):
                break
    if (need_sku and not result.get("sku")) or (need_capacity and not result.get("capacity")):
        result["_error"] = "pdf_required_label_missing_or_no_text"
    return result
