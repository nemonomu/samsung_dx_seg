"""Kasada-free PDP characteristics via the OTTO comparison page (/vergleich/).

The product-comparison page renders each product's Details characteristics (Bauart,
etc.) side by side and is NOT Kasada-protected. We batch variation_ids (the compare
UI allows up to 5) and read each characteristic row aligned to the column order.
"""
from __future__ import annotations

import re
import time
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from bs4 import BeautifulSoup

VERGLEICH_URL = "https://www.otto.de/vergleich/"
BATCH_SIZE = 5
_HDR = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.otto.de/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None


def _fetch(variation_ids: list[str], timeout: int, retries: int = 1) -> str | None:
    from common import raw_html
    url = VERGLEICH_URL + "?" + urlencode({"variationIds": ",".join(variation_ids)})
    for attempt in range(retries + 1):
        try:
            with urlopen_compat(url, timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                raw_html.save(f"vergleich_{variation_ids[0]}", html)
                return html
        except (HTTPError, URLError):
            if attempt < retries:
                time.sleep(2)
    return None


def urlopen_compat(url: str, timeout: int):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_HDR, method="GET"), timeout=timeout)


def _row_values(soup: BeautifulSoup, label: str) -> dict[str, str | None]:
    """Map {variation_id: value} for the characteristic row whose label == label.

    Each cell carries its own data-variation-id, so we key by the cell's own id
    rather than column position — this guarantees the value belongs to that SKU.
    """
    for node in soup.find_all(string=re.compile(rf"^\s*{re.escape(label)}\s*$")):
        label_el = node.find_parent()
        container = label_el.find_parent() if label_el else None
        if not container:
            continue
        col_list = container.select_one(".pcp_content__column-list")
        if not col_list:
            continue
        out: dict[str, str | None] = {}
        for cell in col_list.find_all(recursive=False):
            vid = cell.get("data-variation-id")
            if vid:
                out[str(vid)] = _clean(cell.get_text(" ", strip=True))
        if out:
            return out
    return {}


def _one_pass(ids: list[str], label: str, timeout: int, sleep: float, batch_size: int = BATCH_SIZE) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        html = _fetch(batch, timeout)
        # A response with no Bauart row at all (transient error / Kasada challenge /
        # rate-limit page) leaves the whole batch unresolved so the retry pass re-queries it.
        row = _row_values(BeautifulSoup(html, "lxml"), label) if html else {}
        for vid in batch:
            result[vid] = row.get(vid)
        if sleep > 0:
            time.sleep(sleep)
    return result


def _names_from_html(soup: BeautifulSoup, wanted: set[str]) -> dict[str, str]:
    """{variation_id: longest product-name text} — the long name carries the subtitle
    (e.g. '..., Frontlader, Inverter Motor, ...') used as a fallback when a structured
    characteristic is blank."""
    out: dict[str, str] = {}
    for node in soup.find_all(string=True):
        text = _clean(node)
        if not text or len(text) < 15:
            continue
        anc = node.parent
        for _ in range(8):
            if anc is None:
                break
            vid = anc.get("data-variation-id")
            if vid:
                vid = str(vid)
                if vid in wanted and len(text) > len(out.get(vid, "")):
                    out[vid] = text
                break
            anc = anc.parent
    return out


def name_map(variation_ids: list[str], *, timeout: int = 45, sleep: float = 0.6) -> dict[str, str]:
    """{variation_id: full product name (with subtitle)} via batched /vergleich/."""
    result: dict[str, str] = {}
    ids = [str(v) for v in variation_ids if v]
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i:i + BATCH_SIZE]
        html = _fetch(batch, timeout)
        if html:
            result.update(_names_from_html(BeautifulSoup(html, "lxml"), set(batch)))
        if sleep > 0:
            time.sleep(sleep)
    return result


def characteristic_map(variation_ids: list[str], label: str, *, timeout: int = 45,
                       sleep: float = 0.6, retry_rounds: int = 2, retry_sleep: float = 1.5,
                       final_individual: bool = True) -> dict[str, str | None]:
    """{variation_id: characteristic value} for `label` (e.g. 'Bauart'), via batched /vergleich/.

    Keyed by each cell's own data-variation-id so a value always belongs to that SKU.
    Unresolved ids (transient empty responses) are re-queried for a few batched rounds,
    then once per-id (batch=1, most reliable). Ids that stay empty are genuinely blank on
    OTTO (e.g. Waschtrockner / accessories).
    """
    ids = [str(v) for v in variation_ids if v]
    result = _one_pass(ids, label, timeout, sleep)
    for _ in range(retry_rounds):
        missing = [v for v in ids if not result.get(v)]
        if not missing:
            break
        for vid, val in _one_pass(missing, label, timeout, retry_sleep).items():
            if val:
                result[vid] = val
    if final_individual:
        missing = [v for v in ids if not result.get(v)]
        for vid, val in _one_pass(missing, label, timeout, retry_sleep, batch_size=1).items():
            if val:
                result[vid] = val
    return result


NAME_KEY = "_name"  # full product name (with subtitle) captured alongside characteristics


def _multi_pass(ids: list[str], labels: list[str], timeout: int, sleep: float,
                batch_size: int = BATCH_SIZE) -> dict[str, dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {v: {} for v in ids}
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        html = _fetch(batch, timeout)
        if html:
            soup = BeautifulSoup(html, "lxml")
            for label in labels:
                for vid, val in _row_values(soup, label).items():
                    if vid in result and val:
                        result[vid][label] = val
            for vid, name in _names_from_html(soup, set(batch)).items():
                if name and len(name) > len(result[vid].get(NAME_KEY, "") or ""):
                    result[vid][NAME_KEY] = name
        if sleep > 0:
            time.sleep(sleep)
    return result


def characteristics_map(variation_ids: list[str], labels: list[str], *, timeout: int = 45,
                        sleep: float = 0.6, retry_rounds: int = 2, retry_sleep: float = 1.5,
                        final_individual: bool = True, required: list[str] | None = None) -> dict[str, dict[str, str | None]]:
    """{variation_id: {label: value, _name: full product name}} for several characteristics
    in one set of /vergleich/ requests. A vid is re-queried (batched rounds, then per-id
    batch=1) if its column did not render at all (no name/labels) OR a `required` label is
    missing — batched comparison pages intermittently drop a cell, and the per-id pass is
    reliable. Genuinely absent labels stay missing after the retries."""
    ids = [str(v) for v in variation_ids if v]
    result = _multi_pass(ids, labels, timeout, sleep)

    def _incomplete(vid: str) -> bool:
        d = result.get(vid, {})
        rendered = bool(d.get(NAME_KEY)) or any(v for k, v in d.items() if k != NAME_KEY)
        if not rendered:
            return True
        if required and not all(d.get(lbl) for lbl in required):
            return True
        return False

    def _merge(passed: dict[str, dict[str, str | None]]) -> None:
        for vid, vals in passed.items():
            for key, val in vals.items():
                if val and not result[vid].get(key):
                    result[vid][key] = val

    for _ in range(retry_rounds):
        missing = [v for v in ids if _incomplete(v)]
        if not missing:
            break
        _merge(_multi_pass(missing, labels, timeout, retry_sleep))
    if final_individual:
        missing = [v for v in ids if _incomplete(v)]
        _merge(_multi_pass(missing, labels, timeout, retry_sleep, batch_size=1))
    return result
