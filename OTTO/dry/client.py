"""Bounded direct HTTP. No browser, raw captures, review pagination or extra APIs."""
import io
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from common import compare, datasheet, listing, review_rating
from dry.config import CATEGORY_LABELS, COMPARE_LABELS, SEARCH_TERM, SEARCH_URL, TYPE_LABELS, DryError, PDF_PARSE_TIMEOUT, PDF_MAX_ATTEMPTS
from dry.fields import capacity, capacity_candidate, capacity_label, public_product_url, text, loading, savings, DRYER, valid_sku, sku_rejection
from dry.pdf_process import pdf_attempt


class RequestFailure(DryError):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.status = status


class Client:
    def __init__(self, events, timeout=30, attempts=3, pause=0.25, pdf_timeout=PDF_PARSE_TIMEOUT):
        self.events, self.timeout, self.attempts, self.pause = events, timeout, attempts, pause
        self.compare_cache = {}
        self.pdf_timeout = pdf_timeout
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
                names = comparison_names(soup, set(needed))
                values = {label: compare._row_values(soup, label) for label in COMPARE_LABELS}
                for vid in needed:
                    chars = {label: vals[vid] for label, vals in values.items() if vals.get(vid)}
                    if names.get(vid):
                        chars["_name"] = names[vid]
                    if not chars:
                        chars["_unavailable"] = "comparison_option_missing"
                    if not names.get(vid):
                        chars["_title_reason"] = "comparison_product_title_unavailable"
                    self.compare_cache[vid] = chars
            except RequestFailure as exc:
                for vid in needed:
                    self.compare_cache[vid] = {"_error": str(exc), "_error_kind": "request"}
        return {v: self.compare_cache[v] for v in ids}

    def pdf(self, uri, *, need_sku, need_capacity, need_loading=False):
        needed = dict(sku=need_sku, capacity=need_capacity, loading_type=need_loading)
        if not any(needed.values()):
            return {}
        if not uri:
            return {"_unavailable": "own_pdf_link_missing"}
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return {"_error": "own_pdf_link_invalid", "_error_kind": "extraction"}
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return {"_error": "own_pdf_link_invalid", "_error_kind": "extraction"}
        retained, body, last = {}, None, {}
        attempts = min(PDF_MAX_ATTEMPTS, max(1, self.attempts))
        for attempt in range(1, attempts + 1):
            remaining = {k: v and not retained.get(k) for k, v in needed.items()}
            self.events.emit("pdf_attempt", attempt=attempt, max_attempts=attempts,
                             needed=[k for k, v in remaining.items() if v], cached_document=body is not None,
                             request_timeout=self.timeout, parse_timeout=self.pdf_timeout)
            def progress(event, **values):
                self.events.emit(event, **{**values, "attempt": attempt, "max_attempts": attempts})
            with self.events.activity("pdf", attempt=attempt):
                last, body = pdf_attempt(uri, body, remaining, self.timeout, self.pdf_timeout, progress)
            for field in needed:
                if last.get(field) and not retained.get(field):
                    retained[field] = last[field]
                    if last.get(field + "_raw"):
                        retained[field + "_raw"] = last[field + "_raw"]
            self.events.emit("pdf_attempt_end", attempt=attempt, confirmed=list(k for k in needed if retained.get(k)),
                             outcome="failed" if last.get("_error") else "read",
                             reason=last.get("_error") or last.get("_unavailable"))
            if (not last.get("_error") or not last.get("_retryable", False)
                    or all(not v or retained.get(k) for k, v in needed.items())):
                break
            if attempt < attempts:
                time.sleep(min(attempt, 3))
        result = {**retained, **{k: last[k] for k in ("_error", "_error_kind", "_unavailable") if k in last}}
        missing = [k for k, v in needed.items() if v and not retained.get(k)]
        if missing:
            result["_missing"] = missing
        return result

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


def comparison_names(soup, wanted):
    result = {}
    for link in soup.select('a[href]'):
        try:
            parts = urlsplit(link.get("href", ""))
        except ValueError:
            continue
        if parts.hostname not in (None, "www.otto.de") or not parts.path.startswith("/p/"):
            continue
        owner = link if link.get("data-variation-id") else link.find_parent(attrs={"data-variation-id": True})
        owner_id = str(owner.get("data-variation-id")) if owner else None
        linked = list(dict.fromkeys(parse_qs(parts.query).get("variationId", [])
                                        + parse_qs(parts.fragment).get("variationId", [])))
        if owner_id and linked and linked != [owner_id]:
            continue
        vid = owner_id or (linked[0] if len(linked) == 1 else None)
        if vid not in wanted:
            continue
        title_nodes = link.select('[itemprop="name"], h1, h2, h3, h4, [class*="title"], [class*="product-name"]')
        # Nested title spans must not duplicate the heading text.
        title_nodes = [n for n in title_nodes if not any(p in title_nodes for p in n.parents)]
        pieces = list(dict.fromkeys(text(n.get_text(" ", strip=True)) for n in title_nodes))
        if not pieces and link.find(["p", "ul", "table"]):
            continue
        title = text(" ".join(pieces)) if pieces else text(link.get_text(" ", strip=True))
        if title and not re.fullmatch(r"(?:zum|ansehen|details|produkt|merken|entfernen)(?:\s+produkt)?", title, re.I):
            # All pieces belong to this product link, never surrounding feature paragraphs.
            if vid not in result or len(title) > len(result[vid]):
                result[vid] = title
    return result


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
                savings=savings(sale.get("discount")))


def explicit_pdf_loading_line(line):
    token = r"(?:Frontlader|Toplader|Frontbeladung|Topbeladung|front[- ]loading|top[- ]loading)"
    if not re.search(r"\b" + token + r"\b", line, re.I):
        return False
    if re.search(r"\b(?:nicht|kein\w*|waschmaschine\w*|waschvollautomat\w*)\b|geeignet für|kompatibel", line, re.I):
        return False
    return bool(DRYER.search(line)
                or re.match(r"\s*(?:Bauart|Beladung|Beladungsart|Gerätetyp|Produkttyp|Typ)\s*[:=]?\s*" + token, line, re.I)
                or re.fullmatch(r"\s*" + token + r"(?:\s+(?:und|oder)\s*" + token + r")?\s*", line, re.I))


def pdf_model_candidates(content):
    """Yield distinct labelled candidates in page order, including rejected ones."""
    labels = list(datasheet._MODEL_LABEL_RE.finditer(content))
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(content)
        tail = content[label.end():end].lstrip(" :\t\r\n")
        raw = tail.splitlines()[0] if tail else ""
        raw = datasheet._MODEL_NEXT_FIELD_RE.split(raw, maxsplit=1)[0].strip(" :;,.|\t")
        raw = re.split(r"\b(?:Gewicht|Weight|Beladung|Trockenkapazit[äa]t)\b", raw,
                       maxsplit=1, flags=re.I)[0].strip()
        if raw:
            model = valid_sku(datasheet._valid_model(raw))
            yield raw, model, None if model else sku_rejection(raw)


def parse_pdf(body, *, need_sku, need_capacity, need_loading=False, progress=None):
    """Read only text for missing model/capacity/loading; no OCR, energy parsing or disk capture."""
    result = {}
    had_text = False
    needed = dict(sku=need_sku, capacity=need_capacity, loading_type=need_loading)
    if not any(needed.values()):
        return result
    started, stage = time.monotonic(), "open"
    def emit(event, **values):
        if progress:
            progress(event, seconds=round(time.monotonic()-started, 2), **values)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            emit("pdf_opened")
            stage = "extract"
            for number, page in enumerate(pdf.pages, 1):
                emit("pdf_page", page=number, needed=[k for k, v in needed.items() if v and not result.get(k)])
                content = page.extract_text() or ""
                had_text = had_text or bool(content.strip())
                if need_sku and not result.get("sku"):
                    for raw, model, reason in pdf_model_candidates(content):
                        emit("pdf_sku_candidate", page=number, raw=raw[:500], normalized=model,
                             reason=reason, selected=model)
                        if model:
                            result.update(sku=model, sku_raw=raw[:500])
                            break
                if need_capacity and not result.get("capacity"):
                    for line in content.splitlines():
                        if re.search(r"wasch|gewicht|weight", line, re.I):
                            continue
                        for match in re.finditer(r"(?:Nennkapazit[äa]t|Rated capacity|Trockenkapazit[äa]t)(?:\s*\([a-z]\))?\s*(?:\(?(?:kg)\)?)?\s*[:=]?\s*(\d[\d.,]*(?:[ \t]+[\d.,]+)*)", line, re.I):
                            raw = match[1].rstrip(".,")
                            normalized, reason = capacity_candidate(raw, labelled=True)
                            emit("pdf_capacity_candidate", page=number, raw=match[0][:500],
                                 normalized=normalized, reason=reason)
                            if normalized:
                                result.update(capacity=normalized, capacity_raw=raw)
                                break
                        if result.get("capacity"):
                            break
                if need_loading and not result.get("loading_type"):
                    # Explicit labels only, never door instructions or generic front/top words.
                    lines = [line for line in content.splitlines()
                             if explicit_pdf_loading_line(line)]
                    raw = text(" ".join(lines))
                    if raw:
                        result["loading_type"] = loading(raw)
                        result["loading_type_raw"] = raw[:500]
                emit("pdf_progress", page=number, result=dict(result), confirmed=[k for k in needed if result.get(k)])
                if all(not needed or result.get(field) for field, needed in (
                        ("sku", need_sku), ("capacity", need_capacity), ("loading_type", need_loading))):
                    break
    except Exception as exc:
        # Never expose exception text or discard values from earlier pages.
        result.update(_error="pdf_" + stage + ":" + type(exc).__name__, _error_kind="extraction",
                      _retryable=not isinstance(exc, ImportError))
    missing = [field for field, needed in (("sku", need_sku), ("capacity", need_capacity),
                                          ("loading_type", need_loading)) if needed and not result.get(field)]
    if missing:
        result["_missing"] = missing
        if not result.get("_error"):
            result["_unavailable"] = "pdf_label_unconfirmed" if had_text else "pdf_no_text"
    return result
