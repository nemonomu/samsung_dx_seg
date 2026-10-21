"""Exactly twenty output columns; listing provenance follows LDY."""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dry import config
from dry.detail import CollectionError


def canonical_product_url(value: str, item: str) -> str:
    parsed = urlsplit(value or "")
    if (parsed.hostname not in {"www.mediamarkt.de", "mediamarkt.de"}
            or parsed.username is not None or parsed.password is not None
            or not re.fullmatch(r"\d+", item)
            or not parsed.path.startswith("/de/product/")
            or not parsed.path.endswith("-" + item + ".html")):
        raise CollectionError("PRODUCT_URL_INVALID")
    # Keep the real product path; don't persist optional tracking/session query.
    return urlunsplit(("https", parsed.hostname, parsed.path, "", ""))


def run_metadata(now: datetime | None = None) -> dict:
    now = now or datetime.now()  # same server-local clock as LDY
    return {"calendar_week": "w" + str(now.isocalendar().week),
            "crawl_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "batch_id": "m_" + now.strftime("%Y%m%d_%H%M%S")}


def positive_savings(value) -> str | None:
    match = re.fullmatch(r"\s*[-+−]?\s*(\d+(?:[.,]\d+)?)\s*%\s*", str(value or ""))
    return match.group(1) + "%" if match else None


def present(*values):
    return next((value for value in values if value is not None and value != ""), None)


def merge_rows(main: list[dict], bsr: list[dict], details: dict[str, dict], meta: dict) -> list[dict]:
    def ranked(rows):
        # Occurrence-level banner/type filtering has ALREADY happened.
        result = {}
        for row in rows:
            key = str(row["sku_id"])
            if key not in result:
                result[key] = (row, len(result) + 1)
        return result
    m, b = ranked(main), ranked(bsr)
    result = []
    for item in [*m, *(key for key in b if key not in m)]:
        primary = (m.get(item) or b[item])[0]
        secondary = b[item][0] if item in b else {}
        detail = details[item]  # missing/failed API must not become an empty row
        star = (None if detail.get("count_of_star_ratings") == 0 else
                present(detail.get("star_rating"), primary.get("star_rating"), secondary.get("star_rating")))
        row = dict.fromkeys(config.COLUMNS)
        row.update(country=config.COUNTRY, account_name=config.ACCOUNT_NAME, item=item,
                   sku=detail.get("sku"), retailer_sku_name=primary.get("retailer_sku_name"),
                   product_url=canonical_product_url(primary.get("product_url"), item),
                   count_of_reviews=detail.get("count_of_reviews"),
                   star_rating=star,
                   count_of_star_ratings=present(detail.get("count_of_star_ratings"),
                                                primary.get("count_of_reviews"), secondary.get("count_of_reviews")),
                   final_sku_price=primary.get("final_sku_price"),
                   original_sku_price=primary.get("original_sku_price"),
                   savings=positive_savings(primary.get("savings")),
                   main_rank=m[item][1] if item in m else None,
                   bsr_rank=b[item][1] if item in b else None,
                   loading_type=detail.get("loading_type"), capacity=detail.get("capacity"), **meta)
        result.append(row)
    return result


def write_output(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=config.COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)
