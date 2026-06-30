"""Probe MediaMarkt TV listing + PDP accessibility through several fetch modes.

Goal: discover what bot defense MediaMarkt.de runs and which ZenRows mode
returns real product HTML (vs a block/challenge page). Mirrors the OTTO
everglades probe. The ZenRows API key is loaded from the project .env and is
never printed or written to artifacts.

Modes tried, cheapest first:
  1. direct          - plain urllib, local IP, no proxy (baseline / block check)
  2. zr_plain        - ZenRows universal API, DE premium proxy, no JS render
  3. zr_js           - ZenRows + js_render (headless browser)
  4. zr_js_premium   - ZenRows + js_render + premium_proxy + wait for grid

Usage:
  python MMKT/step00_probe_listing.py
  python MMKT/step00_probe_listing.py --modes direct,zr_plain
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MMKT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = MMKT_ROOT.parent
OUTPUT_DIR = MMKT_ROOT / "references" / "probe"

ZENROWS_API_URL = "https://api.zenrows.com/v1/"

# MediaMarkt TV (Fernseher) category — Best results sort (default ordering).
LISTING_URL = "https://www.mediamarkt.de/de/category/fernseher-nach-gr%C3%B6%C3%9Fen-4708.html"
# Top Sellers / BSR ordering for bsr_rank.
BSR_URL = LISTING_URL + "?sort=salescount+desc"

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

# Signals that the response is a defense/challenge page rather than real content.
BLOCK_SIGNALS = [
    ("akamai", re.compile(r"akamai|ak_bmsc|_abck|bm_sz", re.I)),
    ("cloudflare", re.compile(r"cloudflare|cf-ray|cf-chl|challenge-platform", re.I)),
    ("captcha", re.compile(r"captcha|hcaptcha|recaptcha|px-captcha", re.I)),
    ("perimeterx", re.compile(r"perimeterx|_px|px-cloud", re.I)),
    ("datadome", re.compile(r"datadome|dd_cookie", re.I)),
    ("access_denied", re.compile(r"access denied|zugriff verweigert|forbidden|blockiert", re.I)),
    ("imperva", re.compile(r"incapsula|imperva|_incap_", re.I)),
]

# Signals that real product content was returned.
CONTENT_SIGNALS = [
    ("price_euro", re.compile(r"\d[\d.\s]*,\-?\s*€|\d[\d.]*,\d{2}\s*€")),
    ("product_link", re.compile(r"/de/product/", re.I)),
    ("samsung", re.compile(r"samsung", re.I)),
    ("fernseher_word", re.compile(r"fernseher|zoll", re.I)),
    ("json_ld_product", re.compile(r'"@type"\s*:\s*"Product"', re.I)),
    ("add_to_cart", re.compile(r"in den warenkorb|warenkorb", re.I)),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe MediaMarkt listing accessibility.")
    p.add_argument("--modes", default="direct,zr_plain,zr_js,zr_js_premium")
    p.add_argument("--url", default=LISTING_URL)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--sleep", type=float, default=1.0)
    return p.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_api_key() -> str:
    load_env()
    for name in ("ZENROWS_API_KEY", "ZENROWS_APIKEY", "ZENROWS_KEY", "ZENROWS_TOKEN"):
        if os.getenv(name):
            return os.environ[name]
    raise RuntimeError("ZENROWS_API_KEY not found in environment or .env")


def _do_request(req: Request, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return {
                "status": resp.status,
                "headers": dict(resp.headers.items()),
                "body": body,
                "error": None,
                "elapsed": round(time.perf_counter() - started, 2),
            }
    except HTTPError as exc:
        return {
            "status": exc.code,
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "body": exc.read(),
            "error": repr(exc),
            "elapsed": round(time.perf_counter() - started, 2),
        }
    except URLError as exc:
        return {"status": None, "headers": {}, "body": b"", "error": repr(exc),
                "elapsed": round(time.perf_counter() - started, 2)}


def fetch_direct(url: str, timeout: int) -> dict[str, Any]:
    return _do_request(Request(url, headers=BROWSER_HEADERS, method="GET"), timeout)


def fetch_zenrows(url: str, timeout: int, **params: Any) -> dict[str, Any]:
    api_params: dict[str, str] = {"apikey": get_api_key(), "url": url}
    for k, v in params.items():
        api_params[k] = "true" if v is True else "false" if v is False else str(v)
    api_url = ZENROWS_API_URL + "?" + urlencode(api_params)
    return _do_request(Request(api_url, method="GET"), timeout)


MODE_FETCHERS = {
    "direct": lambda url, t: fetch_direct(url, t),
    "zr_plain": lambda url, t: fetch_zenrows(url, t, proxy_country="de", premium_proxy=True),
    "zr_js": lambda url, t: fetch_zenrows(url, t, proxy_country="de", premium_proxy=True, js_render=True),
    "zr_js_premium": lambda url, t: fetch_zenrows(
        url, t, proxy_country="de", premium_proxy=True, js_render=True, wait=5000,
    ),
}


def detect(signals: list[tuple[str, re.Pattern]], text: str) -> list[str]:
    return [name for name, pat in signals if pat.search(text)]


def analyze(result: dict[str, Any]) -> dict[str, Any]:
    body: bytes = result["body"]
    text = body.decode("utf-8", errors="replace")
    headers_blob = " ".join(f"{k}:{v}" for k, v in result["headers"].items())
    haystack = text + " " + headers_blob
    blocks = detect(BLOCK_SIGNALS, haystack)
    contents = detect(CONTENT_SIGNALS, text)
    # rough product count via product link occurrences
    product_hits = len(re.findall(r"/de/product/", text, re.I))
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    verdict = "BLOCKED" if blocks and not contents else (
        "CONTENT" if contents else "UNKNOWN")
    return {
        "block_signals": blocks,
        "content_signals": contents,
        "product_link_hits": product_hits,
        "title": title,
        "verdict": verdict,
    }


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()

    manifest: dict[str, Any] = {
        "run_type": "mmkt_listing_probe",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "url": args.url,
        "results": [],
    }

    for mode in modes:
        fetcher = MODE_FETCHERS.get(mode)
        if not fetcher:
            print(f"[probe] unknown mode {mode!r}, skipping")
            continue
        print(f"[probe] mode={mode} fetching ...")
        try:
            result = fetcher(args.url, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] mode={mode} EXC {exc!r}")
            manifest["results"].append({"mode": mode, "fatal_error": repr(exc)})
            continue
        body: bytes = result["body"]
        body_file = OUTPUT_DIR / f"{stamp}_{mode}.html"
        body_file.write_bytes(body)
        info = analyze(result)
        row = {
            "mode": mode,
            "http_status": result["status"],
            "elapsed_s": result["elapsed"],
            "body_bytes": len(body),
            "body_sha1": hashlib.sha1(body).hexdigest()[:12],
            "body_file": str(body_file.relative_to(MMKT_ROOT)),
            "error": result["error"],
            **info,
        }
        manifest["results"].append(row)
        print(
            f"[probe] mode={mode} status={row['http_status']} bytes={len(body):>7} "
            f"verdict={info['verdict']} blocks={info['block_signals']} "
            f"content={info['content_signals']} products={info['product_link_hits']} "
            f"title={info['title']!r}"
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    summary_path = OUTPUT_DIR / f"{stamp}_summary.json"
    summary_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[probe] summary={summary_path.relative_to(MMKT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
