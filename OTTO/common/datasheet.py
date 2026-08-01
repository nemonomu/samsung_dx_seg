"""EU energy datasheet PDF parsing (Kasada-free; URL from listing energy_datasheet_uri).

Layout varies by brand and appliance, so values are located by LABEL, not by fixed
item number. Exposes helpers the category configs compose into spec fields:
  - power_by_label(...)       TV on-mode power (HDR/SDR)
  - value_with_unit(...)      e.g. REF Gesamtrauminhalt (total volume)
  - sku / screen inches
"""
from __future__ import annotations

import io
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DATASHEET_HEADERS = {
    "Accept": "application/pdf,*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}

_NA_RE = re.compile(r"nicht zutreffend|n/a|not applicable|entfällt", re.I)
_OFF_STATE = ("aus-zustand", "bereitschaft", "standby", "off mode", "networked", "vernetzt")
_ON_MODE = ("leistungsaufnahme im ein", "on mode power", "power demand")


def fetch_datasheet_bytes(url: str, timeout: int = 45) -> tuple[bytes, int | None, str | None]:
    try:
        with urlopen(Request(url, headers=DATASHEET_HEADERS, method="GET"), timeout=timeout) as response:
            return response.read(), response.status, None
    except HTTPError as exc:
        return b"", exc.code, repr(exc)
    except URLError as exc:
        return b"", None, repr(exc)
    except Exception as exc:  # noqa: BLE001
        return b"", None, type(exc).__name__ + ": " + str(exc)


def _is_na(value: str | None) -> bool:
    return bool(value) and bool(_NA_RE.search(value))


def _num(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", value)
    return m.group(1).replace(",", ".") if m else None


_MODEL_LABEL_RE = re.compile(
    r"Modellkennung(?: des Lieferanten)?|Model identifier|Modellbezeichnung",
    re.I,
)
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.+\-]*")
_MODEL_STOP_WORDS = {
    "address", "adresse", "anschrift", "supplier", "lieferant", "lieferanten",
    "energieeffizienzklasse", "energy", "class", "bildschirmdiagonale",
    "screen", "leistungsaufnahme", "power", "name",
}


def _valid_model(value: str | None) -> str | None:
    """Return a model identifier, preserving short space-separated suffixes.

    PDF table extraction sometimes merges neighbouring rows. Only caller-selected
    text after the Modellkennung label should be passed here; then a value like
    "75E7S PRO" must stay whole, while later label text stops the candidate.
    """
    if not value:
        return None
    raw = re.sub(r"\s+", " ", str(value)).strip(" :;,.|\t\r\n")
    if not raw:
        return None
    raw = _MODEL_LABEL_RE.sub(" ", raw, count=1).strip(" :;,.|\t\r\n")
    tokens = _MODEL_TOKEN_RE.findall(raw)
    picked: list[str] = []
    for token in tokens:
        folded = token.casefold()
        if folded in _MODEL_STOP_WORDS:
            break
        if not picked:
            if len(token) < 4 or not (any(c.isdigit() for c in token) and any(c.isalpha() for c in token)):
                continue
        else:
            # Keep compact model suffixes such as "PRO" or "WPS", but stop on prose.
            if len(picked) >= 4:
                break
            if not (any(c.isdigit() for c in token) or token.isupper()):
                break
            if len(token) < 2:
                break
        picked.append(token)
    if not picked:
        return None
    model = " ".join(picked).strip()
    return model if any(c.isdigit() for c in model) and any(c.isalpha() for c in model) else None


def _model_after_label(cells: list[str]) -> str | None:
    for idx, cell in enumerate(cells):
        for match in _MODEL_LABEL_RE.finditer(cell or ""):
            candidates = [(cell or "")[match.end():], *cells[idx + 1:idx + 4]]
            for candidate in candidates:
                model = _valid_model(candidate)
                if model:
                    return model
    return None


def parse(pdf_bytes: bytes) -> dict[str, Any]:
    """Return {items: {num:[cells]}, text, sku, error}. items strips the item-number cell."""
    result: dict[str, Any] = {"items": {}, "rows": [], "text": "", "sku": None, "error": None, "bytes": len(pdf_bytes)}
    if not pdf_bytes:
        result["error"] = "empty_pdf"
        return result
    try:
        import pdfplumber

        items: dict[int, list[str]] = {}
        all_rows: list[list[str]] = []
        text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    for row in table:
                        cells = [(c or "").replace("\n", " ").strip() for c in row]
                        if not any(cells):
                            continue
                        all_rows.append(cells)
                        num = cells[0].rstrip(".").strip()
                        if num.isdigit() and int(num) not in items:
                            rest = [c for c in cells[1:] if c]
                            if rest:
                                items[int(num)] = rest
            for page in pdf.pages[:3]:
                text += (page.extract_text() or "") + "\n"
        # image-only datasheet (scanned PDF, no text layer) -> OCR fallback so the values
        # (Nennkapazität, on-mode power, Modellkennung) are still recovered.
        if len(re.sub(r"\s+", "", text)) < 10 and not items:
            from common import ocr
            ocr_text = ocr.pdf_text(pdf_bytes)
            if ocr_text:
                text = ocr_text
                result["ocr"] = True
        result["items"] = items
        result["rows"] = all_rows
        result["text"] = text
        result["sku"] = _sku(items, text)
    except ImportError:
        # missing dependency (pdfplumber) -> fail loudly rather than silently nulling
        # every datasheet field (electricity, ref_capacity, ...).
        raise
    except Exception as exc:  # noqa: BLE001
        result["error"] = type(exc).__name__ + ": " + str(exc)
    return result


def _sku(items: dict[int, list[str]], text: str) -> str | None:
    for rest in items.values():
        model = _model_after_label(rest)
        if model:
            return model
    flat = re.sub(r"\s+", " ", text)
    for match in _MODEL_LABEL_RE.finditer(flat):
        model = _valid_model(flat[match.end():match.end() + 90])
        if model:
            return model
    return None


def power_by_label(parsed: dict[str, Any], *, hdr: bool):
    """On-mode power as '<n> W', or 'NA' (no HDR/SDR), or None. hdr=True -> HDR, else SDR."""
    items = parsed.get("items") or {}
    for rest in items.values():
        joined = " ".join(rest)
        low = joined.lower()
        if not any(t in low for t in _ON_MODE) or any(t in low for t in _OFF_STATE):
            continue
        is_hdr = ("hohem" in low) or ("high dynamic range" in low)
        is_sdr = ("standard" in low) or ("(sdr)" in low) or (" sdr" in low)
        if hdr and not is_hdr:
            continue
        if not hdr and (not is_sdr or is_hdr):
            continue
        if _NA_RE.search(low):
            return "NA"
        n = _num(joined)
        if n:
            return f"{n} W"
    # text fallback: anchor on the HDR/SDR on-mode phrase and read the value in a TIGHT
    # forward window (~45 chars). Both German and English anchors are tried because some
    # sheets are bilingual and mangled, with the value only next to the English label
    # (Samsung "The Frame": value follows "High Dynamic Range 49.0 W"). The tight window
    # avoids swallowing the next field's N/A. A dash ("- W") or "Nicht zutreffend" is a real
    # "not applicable" (non-HDR TV) -> "NA". Values may be "<n> W" or a bare decimal.
    flat = re.sub(r"\s+", " ", parsed.get("text", "").replace("­", ""))
    anchors = ("bei hohem", "high dynamic range") if hdr else ("bei standard", "standard dynamic range")
    na_seen = False
    for anchor in anchors:
        for m in re.finditer(re.escape(anchor), flat, re.I):
            win = flat[m.end(): m.end() + 45]
            w = re.search(r"(\d+(?:[.,]\d+)?)\s*W\b", win) or re.search(r"(\d+[.,]\d+)", win)
            if w:
                return f"{w.group(1).replace(',', '.')} W"
            if _NA_RE.search(win) or re.search(r"[-–—]\s*W\b", win):
                na_seen = True
    if na_seen:
        return "NA"
    # last resort: non-EU spec sheets (e.g. Sharp) list a single on-mode figure with no
    # HDR/SDR split, as "Stromverbrauch (W) 53" (standby is "Stand-by-Stromverbrauch").
    m = re.search(r"(?<!by-)Stromverbrauch\s*\(W\)\s*(\d+(?:[.,]\d+)?)", flat, re.I)
    if m:
        return f"{m.group(1).replace(',', '.')} W"
    return None


def value_with_unit(parsed: dict[str, Any], label_contains: str, unit: str) -> str | None:
    """Find a table row whose label cell contains label_contains; return '<number> <unit>'.

    Used for REF Gesamtrauminhalt -> '<n> l'. The value is the next numeric cell after
    the label cell within the same row.
    """
    key = label_contains.lower()
    # scan ALL raw table rows (REF rows have no leading item number, so items misses them)
    for cells in parsed.get("rows") or []:
        for idx, cell in enumerate(cells):
            if key in cell.lower():
                for nxt in cells[idx + 1:]:
                    # value cell is a standalone number (optionally with l/liter);
                    # skip empty + unit-annotation cells like "(in dm3 oder l)".
                    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:l|liter)?", nxt.strip(), re.I)
                    if m:
                        return f"{m.group(1).replace(',', '.')} {unit}"
    # text fallback: skip the "(in dm3 oder l)" annotation, take the 2-4 digit volume
    flat = re.sub(r"\s+", " ", parsed.get("text", ""))
    m = re.search(re.escape(label_contains) + r"[^\d]*?(?:oder l\)|\bl\b|\))[^\d]{0,8}(\d{2,4})\b", flat, re.I)
    if not m:
        m = re.search(re.escape(label_contains) + r"\D{0,40}?(\d{2,4})\b", flat, re.I)
    return f"{m.group(1)} {unit}" if m else None


def screen_inches(parsed: dict[str, Any]) -> str | None:
    items = parsed.get("items") or {}
    for rest in items.values():
        joined = " ".join(rest)
        if "bildschirmdiagonale" in joined.lower() or "screen diagonal" in joined.lower():
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:Zoll|inch)", joined, re.I)
            if m:
                return m.group(1).replace(",", ".")
    flat = re.sub(r"\s+", " ", parsed.get("text", ""))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:Zoll|inches|inch)\b", flat)
    return m.group(1).replace(",", ".") if m else None
