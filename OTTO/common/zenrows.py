"""ZenRows helpers for OTTO scripts.

The API key is loaded from environment or .env and must never be printed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from common.io_util import PROJECT_ROOT

ZENROWS_API_URL = "https://api.zenrows.com/v1/"
ZENROWS_BROWSER_URL = "wss://browser.zenrows.com"
DEFAULT_PROXY_COUNTRY = "de"
KEY_CANDIDATES = (
    "ZENROWS_API_KEY",
    "ZENROWS_APIKEY",
    "ZENROWS_KEY",
    "ZENROWS_TOKEN",
)


def load_env_file(path: Path | None = None) -> None:
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_zenrows_api_key() -> str:
    load_env_file()
    for name in KEY_CANDIDATES:
        value = os.getenv(name)
        if value:
            return value
    for name, value in os.environ.items():
        upper = name.upper()
        if "ZENROWS" in upper and "KEY" in upper and value:
            return value
    raise RuntimeError("ZenRows API key was not found in environment or project .env")


def universal_api_params(target_url: str, **params: str | int | bool) -> dict[str, str]:
    api_key = get_zenrows_api_key()
    result: dict[str, str] = {"apikey": api_key, "url": target_url}
    for key, value in params.items():
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        else:
            result[key] = str(value)
    return result


def fetch_via_universal(
    target_url: str,
    *,
    timeout: int = 60,
    proxy_country: str = DEFAULT_PROXY_COUNTRY,
    premium_proxy: bool = True,
    js_render: bool = False,
    extra_headers: dict[str, str] | None = None,
    **params: str | int | bool,
) -> dict[str, Any]:
    """Fetch a target URL through the ZenRows Universal API (synchronous urllib).

    Returns a dict shaped like step09's fetch_html result: status/body/error/etc.
    All traffic egresses through the ZenRows proxy in proxy_country, so the local
    machine IP is never exposed to the target.
    """
    api_params: dict[str, str | int | bool] = {
        "proxy_country": proxy_country,
        "premium_proxy": premium_proxy,
        "js_render": js_render,
    }
    api_params.update(params)
    api_url = ZENROWS_API_URL + "?" + urlencode(universal_api_params(target_url, **api_params))
    request = Request(api_url, headers=extra_headers or {}, method="GET")
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            return {
                "status": response.status,
                "final_url": target_url,
                "content_type": response.headers.get("Content-Type"),
                "body": body,
                "error": None,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
    except HTTPError as exc:
        return {
            "status": exc.code,
            "final_url": target_url,
            "content_type": exc.headers.get("Content-Type") if exc.headers else None,
            "body": exc.read(),
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except URLError as exc:
        return {
            "status": None,
            "final_url": target_url,
            "content_type": None,
            "body": b"",
            "error": repr(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def build_scraping_browser_url(proxy_country: str = DEFAULT_PROXY_COUNTRY, **params: str | int | bool) -> str:
    api_key = get_zenrows_api_key()
    query: dict[str, str] = {"apikey": api_key, "proxy_country": proxy_country}
    for key, value in params.items():
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        else:
            query[key] = str(value)
    return ZENROWS_BROWSER_URL + "?" + urlencode(query)


def redacted_scraping_browser_options(proxy_country: str = "de", **params: str | int | bool) -> dict[str, str]:
    result: dict[str, str] = {"proxy_country": proxy_country}
    for key, value in params.items():
        result[key] = str(value)
    return result
