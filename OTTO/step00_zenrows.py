"""ZenRows helpers for OTTO scripts.

The API key is loaded from environment or .env and must never be printed.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode

from step00_config import PROJECT_ROOT

ZENROWS_API_URL = "https://api.zenrows.com/v1/"
ZENROWS_BROWSER_URL = "wss://browser.zenrows.com"
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


def build_scraping_browser_url(proxy_country: str = "de", **params: str | int | bool) -> str:
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
