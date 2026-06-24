"""Probe OTTO everglades listing API without ZenRows.

This is a direct HTTP replay check for the listing API discovered from RDP HAR.
It does not load .env, does not use ZenRows, and does not print cookies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OTTO_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = OTTO_ROOT / "references" / "xhr"
EVERGLADES_URL = "https://www.otto.de/everglades/products"
RULE = "(und.(suchbegriff.fernseher).(~.(v.1)))"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.otto.de/suche/fernseher/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Direct OTTO everglades API probe without ZenRows.")
    parser.add_argument("--offsets", default="0,109,218,327")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--cookie-env",
        default="",
        help="Optional environment variable name containing a Cookie header. The value is never printed.",
    )
    return parser.parse_args()


def parse_offsets(raw: str) -> list[int]:
    offsets: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            offsets.append(int(token))
    return offsets


def build_url(offset: int) -> str:
    params = [
        ("rule", RULE),
        ("intents", "ranked"),
        ("intents", "sponsored"),
        ("intents", "context"),
        ("ranked.offset", str(offset)),
    ]
    return EVERGLADES_URL + "?" + urlencode(params)


def fetch_direct(url: str, timeout: int, cookie_env: str) -> tuple[int | None, dict[str, str], bytes, str | None]:
    headers = dict(DEFAULT_HEADERS)
    if cookie_env:
        cookie_value = os.getenv(cookie_env)
        if cookie_value:
            headers["Cookie"] = cookie_value
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read(), None
    except HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(), repr(exc)
    except URLError as exc:
        return None, {}, b"", repr(exc)


def sha_order(values: list[str]) -> str:
    return hashlib.sha1("\n".join(values).encode("utf-8")).hexdigest()[:12]


def summarize_json(data: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "json_type": type(data).__name__,
        "top_keys": list(data.keys()) if isinstance(data, dict) else None,
        "intents": [],
    }
    if not isinstance(data, dict):
        return summary
    for intent in data.get("intents") or []:
        if not isinstance(intent, dict):
            continue
        products = [p for p in (intent.get("products") or []) if isinstance(p, dict)]
        best_ids = [str(p.get("bestVariationId") or p.get("id") or "") for p in products]
        best_ids = [value for value in best_ids if value]
        summary["intents"].append(
            {
                "intent": intent.get("intent"),
                "count": intent.get("count"),
                "products": len(products),
                "unique_best_ids": len(set(best_ids)),
                "best_id_sha1_order": sha_order(best_ids),
                "product_types": sorted({str(p.get("productType")) for p in products}),
                "first_ids": best_ids[:5],
            }
        )
    return summary


def main() -> int:
    args = parse_args()
    offsets = parse_offsets(args.offsets)
    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_ROOT / f"everglades_direct_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "run_type": "otto_everglades_direct_probe",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "endpoint": EVERGLADES_URL,
        "rule": RULE,
        "offsets": offsets,
        "uses_zenrows": False,
        "cookie_env_used": bool(args.cookie_env),
        "results": [],
    }

    ok = True
    for offset in offsets:
        url = build_url(offset)
        status, headers, body, error = fetch_direct(url, args.timeout, args.cookie_env)
        body_file = output_dir / f"everglades_offset_{offset}.body"
        body_file.write_bytes(body)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        result: dict[str, Any] = {
            "offset": offset,
            "target_url": url,
            "http_status": status,
            "content_type": content_type,
            "body_file": body_file.name,
            "body_bytes": len(body),
            "body_sha1": hashlib.sha1(body).hexdigest(),
            "error": error,
            "json_ok": False,
        }
        try:
            data = json.loads(body.decode("utf-8"))
            result["json_ok"] = True
            result["summary"] = summarize_json(data)
            body_file.rename(output_dir / f"everglades_offset_{offset}.json")
            result["body_file"] = f"everglades_offset_{offset}.json"
        except Exception as exc:
            result["json_error"] = repr(exc)
            result["body_preview"] = body[:500].decode("utf-8", errors="replace")
            ok = False
        manifest["results"].append(result)

        ranked = next((item for item in (result.get("summary") or {}).get("intents", []) if item.get("intent") == "ranked"), {})
        sponsored = next((item for item in (result.get("summary") or {}).get("intents", []) if item.get("intent") == "sponsored"), {})
        print(
            "[direct] offset={offset} status={status} json={json_ok} ranked={ranked} sponsored={sponsored} bytes={bytes}".format(
                offset=offset,
                status=status,
                json_ok=result["json_ok"],
                ranked=ranked.get("products"),
                sponsored=sponsored.get("products"),
                bytes=len(body),
            )
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[direct] output_dir={output_dir}")
    print(f"[direct] summary={summary_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())