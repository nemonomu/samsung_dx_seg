"""ZenRows fetch helper for MMKT scripts.

MediaMarkt is Cloudflare-walled; a DE premium proxy (no JS render needed)
returns full server-rendered HTML. The API key is loaded from the project .env
and is never printed. See memory: mmkt-bot-defense.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from common.config import PROJECT_ROOT

ZENROWS_API_URL = "https://api.zenrows.com/v1/"
ZENROWS_BROWSER_URL = "wss://browser.zenrows.com"
DEFAULT_PROXY_COUNTRY = "de"
KEY_CANDIDATES = ("ZENROWS_API_KEY", "ZENROWS_APIKEY", "ZENROWS_KEY", "ZENROWS_TOKEN")


def load_env_file() -> None:
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


def get_zenrows_api_key() -> str:
    load_env_file()
    for name in KEY_CANDIDATES:
        if os.getenv(name):
            return os.environ[name]
    raise RuntimeError("ZenRows API key not found in environment or project .env")


def fetch_via_universal(
    target_url: str,
    *,
    timeout: int = 90,
    proxy_country: str = DEFAULT_PROXY_COUNTRY,
    premium_proxy: bool = True,
    js_render: bool = False,
    **params: Any,
) -> dict[str, Any]:
    """Fetch a target URL through the ZenRows Universal API (DE proxy)."""
    api_params: dict[str, str] = {"apikey": get_zenrows_api_key(), "url": target_url}
    base = {"proxy_country": proxy_country, "premium_proxy": premium_proxy, "js_render": js_render}
    base.update(params)
    for key, value in base.items():
        api_params[key] = "true" if value is True else "false" if value is False else str(value)
    api_url = ZENROWS_API_URL + "?" + urlencode(api_params)
    started = time.perf_counter()
    try:
        with urlopen(Request(api_url, method="GET"), timeout=timeout) as response:
            return {
                "status": response.status,
                "body": response.read(),
                "error": None,
                "elapsed": round(time.perf_counter() - started, 2),
            }
    except HTTPError as exc:
        return {"status": exc.code, "body": exc.read(), "error": repr(exc),
                "elapsed": round(time.perf_counter() - started, 2)}
    except URLError as exc:
        return {"status": None, "body": b"", "error": repr(exc),
                "elapsed": round(time.perf_counter() - started, 2)}


def build_scraping_browser_url(proxy_country: str = DEFAULT_PROXY_COUNTRY, **params: Any) -> str:
    """WebSocket CDP URL for the ZenRows scraping browser (always proxy_country=de)."""
    query: dict[str, str] = {"apikey": get_zenrows_api_key(), "proxy_country": proxy_country}
    for key, value in params.items():
        query[key] = "true" if value is True else "false" if value is False else str(value)
    return ZENROWS_BROWSER_URL + "?" + urlencode(query)
