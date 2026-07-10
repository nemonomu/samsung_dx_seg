"""SIEL-style logging helpers for the SEG Amazon crawler."""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common.io_util import AMZN_ROOT

for _stream in (sys.stdout, sys.stderr):
    enc = getattr(_stream, "encoding", "") or ""
    if enc.lower() not in {"utf-8", "utf8"} and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

REVIEW_SEP = " ||| "
SIMILAR_SEP = ", "
_RUN_LOG_PATH: Path | None = None
_RUN_JSONL_PATH: Path | None = None

_PRICE_TOKEN_RE = re.compile(r"[-+]?\d[\d.,]*")
_INT_RE = re.compile(r"\d+")


def logs_dir() -> Path:
    path = AMZN_ROOT / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: Any) -> str:
    text = str(value or "").strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def make_basename(account_name: str, product: str, stage: str) -> str:
    ts = datetime.now().strftime("%y%m%d%H%M")
    return f"seg_{_slug(account_name)}_{_slug(product)}_{_slug(stage)}_{ts}"


def setup(account_name: str, product: str, stage: str):
    base = make_basename(account_name, product, stage)
    log_path = logs_dir() / f"{base}.log"
    html_path = logs_dir() / f"{base}.html"
    logger = logging.getLogger(f"seg.{account_name}.{product}.{stage}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    logger.info("=== seg crawler start: account=%s product=%s stage=%s ===", account_name, product, stage)
    logger.info("log_file=%s", log_path)
    logger.info("html_file=%s", html_path)
    return logger, html_path


def setup_run(product: str, jsonl_path: str | Path | None = None) -> Path:
    global _RUN_LOG_PATH, _RUN_JSONL_PATH
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    _RUN_LOG_PATH = logs_dir() / f"seg_amazon_{str(product).lower()}_run_{ts}.log"
    _RUN_JSONL_PATH = Path(jsonl_path) if jsonl_path else None
    run_log(f"run log file={_RUN_LOG_PATH}")
    if _RUN_JSONL_PATH is not None:
        run_log(f"jsonl file={_RUN_JSONL_PATH}")
    return _RUN_LOG_PATH


def current_run_log_path() -> Path | None:
    return _RUN_LOG_PATH


def run_log(message: str, level: str = "INFO") -> None:
    if _RUN_LOG_PATH is None:
        return
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} [{level}] {message}\n")
    except Exception:
        pass


def log_selectors(logger, selectors: dict[str, Any]) -> None:
    logger.info("collect target schema(selectors): %d", len(selectors or {}))
    for field in sorted((selectors or {}).keys()):
        sel = selectors[field]
        xpath = sel.get("xpath") if isinstance(sel, dict) else sel
        fallback = sel.get("fallback") if isinstance(sel, dict) else None
        logger.info("  - %s: xpath=%s%s", field, xpath, f" (fallback={fallback})" if fallback else "")


def _truncate(value: Any, length: int = 50) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[:length] + "..."


def _normal_number(token: str) -> str:
    token = token.strip()
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            return token.replace(".", "").replace(",", ".")
        return token.replace(",", "")
    if "," in token:
        left, right = token.rsplit(",", 1)
        if len(right) == 2:
            return left.replace(",", "") + "." + right
        return token.replace(",", "")
    if "." in token:
        left, right = token.rsplit(".", 1)
        if len(right) == 2:
            return left.replace(",", "") + "." + right
        return token.replace(".", "")
    return token


_EURO = "\u20ac"
_AMZN_EURO_PRICE_RE = re.compile(
    rf"(?:{re.escape(_EURO)}\s*\d[\d.\s]*(?:,\d{{2}})?|\d[\d.\s]*(?:,\d{{2}})?\s*{re.escape(_EURO)})"
)
_AMZN_PRICE_SENTINELS = (
    "Currently unavailable",
    "No featured offers",
    "See price in cart",
    "Temporarily out of stock",
    "Price higher than typical",
    "Derzeit nicht verf\u00fcgbar",
    "Derzeit nicht verfuegbar",
    "Keine hervorgehobenen Angebote verf\u00fcgbar",
    "Keine hervorgehobenen Angebote verfuegbar",
)


def parse_amzn_apex_price(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    split_match = re.search(r"(\d{1,3}(?:\.\d{3})*)\s*,\s*(\d{2})\s*€", text)
    if split_match:
        return f"{split_match.group(1)},{split_match.group(2)}€"
    match = _AMZN_EURO_PRICE_RE.search(text)
    if match:
        price = re.sub(r"\s+", "", match.group(0))
        if price.startswith(_EURO):
            price = price[1:] + _EURO
        return price
    if any(sentinel.casefold() in text.casefold() for sentinel in _AMZN_PRICE_SENTINELS):
        return text
    return None


def parse_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    match = _PRICE_TOKEN_RE.search(str(value))
    if not match:
        return None
    try:
        return float(_normal_number(match.group(0)))
    except ValueError:
        return None


def parse_int_field(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = _INT_RE.search(str(value).replace(",", "").replace(".", ""))
    return int(match.group(0)) if match else None


_STAR_RE = re.compile(r"\b(\d+(?:[,.]\d+)?)\b")
_NUM_CHUNK_RE = re.compile(r"\d[\d,.]*")
_MODEL_YEAR_4DIGIT_RE = re.compile(r"\b(\d{4})\b")
_RATINGS_RE = re.compile(
    r"(\d[\d,.]*)\s*(?:global\s+)?(?:ratings?|bewertungen|sternebewertungen|kundenbewertungen)\b",
    re.I,
)
_DETAILS_TAIL_RE = re.compile(r"\s*(?:Details|Einzelheiten)\.?\s*$", re.I)
_NUM_ONLY_RE = re.compile(r"\s*\d[\d,.]*\s*")
_REF_PRICE_NOISE_RE = re.compile(
    rf"^\s*{re.escape(_EURO)}|^\d+\s*(?:offers?|angebote?)\s+(?:from|ab)\s+{re.escape(_EURO)}",
    re.I,
)
_REF_REQUIRED_KEYWORD_RE = re.compile(
    r"refrigerator|freezer|kuehl|k\u00fchl|gefrier|kombi|side\s*by\s*side|\b\d+\s*l\b|\blitre|\bliter",
    re.I,
)


def _count_token(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    return f"{int(digits):,}"


def westernize_commas(value: Any) -> str | None:
    if value in (None, ""):
        return None

    def repl(match: re.Match[str]) -> str:
        return _count_token(match.group(0)) or match.group(0)

    return _NUM_CHUNK_RE.sub(repl, str(value))


def parse_star_rating(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.casefold() == "no customer reviews":
        return "No customer reviews"
    for match in _STAR_RE.finditer(text):
        token = match.group(1).replace(",", ".")
        try:
            numeric = float(token)
        except ValueError:
            continue
        if 0 <= numeric <= 5:
            return token
    return None


def parse_model_year(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if "/" in text:
        years = [int(part.strip()) for part in text.split("/") if part.strip().isdigit() and len(part.strip()) == 4]
        if years:
            return str(max(years))
    match = _MODEL_YEAR_4DIGIT_RE.search(text)
    return match.group(1) if match else None


def parse_count_of_ratings(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"^[\(\[\|]+|[\)\]\|]+$", "", str(value).strip()).strip()
    match = _RATINGS_RE.search(text)
    if match:
        return _count_token(match.group(1))
    if re.search(r"\b(?:out of 5|von 5)\b", text, flags=re.I):
        chunks = _NUM_CHUNK_RE.findall(text)
        if len(chunks) >= 3:
            return _count_token(chunks[-1])
        return None
    match = _NUM_CHUNK_RE.search(text)
    return _count_token(match.group(0)) if match else None



def parse_delivery(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def parse_delivery_availability(value: Any) -> str | None:
    text = parse_delivery(value)
    if not text:
        return None
    text = _DETAILS_TAIL_RE.sub("", text).strip()
    return text or None


def parse_fastest_delivery(value: Any) -> str | None:
    text = parse_delivery(value)
    if not text:
        return None
    text = _DETAILS_TAIL_RE.sub("", text).strip()
    return text or None


def parse_sku_assurance(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    if text.casefold().startswith("amazon"):
        return text
    return f"Amazon {text}"


def _clean_review_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def format_review_content(parts: list[Any]) -> str | None:
    cleaned = [_clean_review_text(value) for value in (parts or [])]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return None
    return REVIEW_SEP.join(f"review{idx} - {text}" for idx, text in enumerate(cleaned, start=1))


def format_similar_names(parts: list[Any]) -> str | None:
    cleaned = [_clean_review_text(value) for value in (parts or [])]
    cleaned = [value for value in cleaned if value]
    return SIMILAR_SEP.join(cleaned) if cleaned else None


def filter_similar_noise(parts: list[Any]) -> list[str]:
    if not parts:
        return []
    return [str(part).strip() for part in parts if part and not _NUM_ONLY_RE.fullmatch(str(part))]


def filter_similar_noise_ref(parts: list[Any]) -> list[str]:
    out: list[str] = []
    for part in filter_similar_noise(parts):
        if _REF_PRICE_NOISE_RE.search(part):
            continue
        if not _REF_REQUIRED_KEYWORD_RE.search(part):
            continue
        out.append(part)
    return out


def count_review_cards(value: Any) -> int:
    if value in (None, ""):
        return 0
    return len(re.findall(r"\breview\d+\s-\s", str(value)))


def count_similar_names(value: Any) -> int:
    if value in (None, ""):
        return 0
    text = str(value)
    sep = SIMILAR_SEP if SIMILAR_SEP in text else ", "
    return text.count(sep) + 1


def warn_price_logic(logger, rec: dict[str, Any]) -> None:
    final_price = rec.get("final_sku_price")
    original_price = rec.get("original_sku_price")
    final_num = parse_price(final_price)
    original_num = parse_price(original_price)
    if final_num is not None and original_num is not None and final_num > original_num:
        logger.warning(
            "price logic violation: final=%s (%.2f) > original=%s (%.2f) | url=%s",
            final_price,
            final_num,
            original_price,
            original_num,
            rec.get("source_url") or rec.get("product_url"),
        )


_DEFAULT_EXCLUDE = {
    "account_name",
    "product",
    "stage",
    "company",
    "division",
    "source_url",
    "batch_id",
    "crawl_datetime",
    "page_no",
    "main_rank",
    "bsr_rank",
}


def log_record_summary(logger, rec: dict[str, Any], exclude=None) -> None:
    skip = set(exclude) if exclude is not None else _DEFAULT_EXCLUDE
    rank_parts = []
    for key in ("main_rank", "bsr_rank"):
        if rec.get(key) not in (None, ""):
            rank_parts.append(f"{key} : {rec.get(key)}")
    parts = []
    for key, value in rec.items():
        if key in skip or value in (None, ""):
            continue
        if key == "detailed_review_content":
            count = count_review_cards(value)
            if count:
                parts.append(f"detailed_review_content_card : {count}")
        elif key == "retailer_sku_name_similar":
            count = count_similar_names(value)
            if count:
                parts.append(f"retailer_sku_name_similar_count : {count}")
        else:
            parts.append(f"{key} : {_truncate(value)}")
    head = " ".join(rank_parts) + " | " if rank_parts else ""
    logger.info("record: %s%s", head, " | ".join(parts) if parts else "(no fields)")


def log_detail_result(logger, rec: dict[str, Any], product: str | None = None) -> None:
    def show(key: str) -> str:
        value = rec.get(key)
        return "-" if value in (None, "", []) else _truncate(value, 90)

    fields = [
        "item", "sku", "final_sku_price", "original_sku_price", "star_rating",
        "count_of_star_ratings", "sku_popularity", "discount_type",
        "delivery_availability", "fastest_delivery", "inventory_status",
    ]
    product_key = str(product or rec.get("product") or "").lower()
    if product_key == "tv":
        fields += ["screen_size", "model_year", "estimated_annual_electricity_use"]
    elif product_key == "ref":
        fields += ["ref_refrigerator_type", "ref_capacity"]
    fields += ["detailed_review_content"]
    parts = []
    for field in fields:
        if field == "detailed_review_content":
            value = rec.get(field)
            parts.append(f"{field}_card={count_review_cards(value) if value not in (None, '') else 0}")
        else:
            parts.append(f"{field}={show(field)}")
    logger.info("detail summary: %s", " | ".join(parts))


class DetailProgress:
    def __init__(self, total: int, interval: int = 20):
        self.total = total
        self.interval = interval
        self.started = datetime.now().timestamp()
        self.done = 0
        self.errors = 0
        self.fills = {
            "detailed_review_content": 0,
            "sku": 0,
            "inventory_status": 0,
            "star_rating": 0,
        }

    @staticmethod
    def _elapsed(seconds: float) -> str:
        total = int(seconds)
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}h{minutes}m{secs}s"
        if minutes:
            return f"{minutes}m{secs}s"
        return f"{secs}s"

    def update(self, logger, rec: dict[str, Any]) -> None:
        self.done += 1
        if rec.get("_error"):
            self.errors += 1
        else:
            for field in self.fills:
                if rec.get(field) not in (None, "", []):
                    self.fills[field] += 1
        is_final = self.total > 0 and self.done == self.total
        if self.done % self.interval != 0 and not is_final:
            return
        elapsed = self._elapsed(datetime.now().timestamp() - self.started)
        fills = self.fills
        if is_final:
            logger.info(
                "[done] %d records in %s | rev=%d sku=%d inv=%d star=%d errors=%d",
                self.done,
                elapsed,
                fills["detailed_review_content"],
                fills["sku"],
                fills["inventory_status"],
                fills["star_rating"],
                self.errors,
            )
        else:
            pct = self.done * 100 // self.total if self.total else 0
            logger.info(
                "[progress] %d/%d (%d%%) %s | rev=%d sku=%d inv=%d star=%d",
                self.done,
                self.total,
                pct,
                elapsed,
                fills["detailed_review_content"],
                fills["sku"],
                fills["inventory_status"],
                fills["star_rating"],
            )


def log_record_event(rec: dict[str, Any]) -> None:
    if not isinstance(rec, dict):
        return
    stage = rec.get("stage") or rec.get("error_stage") or rec.get("summary_stage") or "unknown"
    product = rec.get("product") or ""
    page = rec.get("page_no")
    url = rec.get("source_url") or rec.get("product_url") or ""
    page_part = f" page={page}" if page not in (None, "") else ""
    product_part = f" product={product}" if product else ""
    url_part = f" url={url}" if url else ""
    if rec.get("_error"):
        reason = rec.get("message") or rec.get("_error")
        run_log(
            f"stage={stage}{page_part}{product_part}{url_part} error={rec.get('_error')} reason={reason}",
            "ERROR",
        )
    elif rec.get("_warn"):
        reason = rec.get("message") or rec.get("_warn")
        run_log(
            f"stage={stage}{page_part}{product_part}{url_part} warning={rec.get('_warn')} reason={reason}",
            "WARNING",
        )
    elif rec.get("_detail_skip"):
        run_log(
            f"stage={stage}{product_part}{url_part} detail_skip={rec.get('_detail_skip')}",
            "WARNING",
        )
    elif rec.get("_summary") or str(stage).endswith("summary") or stage == "db_insert_summary":
        fields = []
        for key in (
            "summary_stage",
            "target_records",
            "captured_urls",
            "unique_records",
            "emitted_records",
            "target_met",
            "stage_error",
            "returncode",
            "rows_full",
            "rows_listing",
            "inserted_total",
            "success",
        ):
            if key in rec:
                fields.append(f"{key}={rec.get(key)}")
        run_log(f"stage={stage} summary={rec.get('_summary', rec.get('run_type', ''))} " + " ".join(fields))
