"""Local undetected-chromedriver (UC) session for MediaMarkt — ZenRows-free.

MediaMarkt is Cloudflare-walled, but a real local Chrome driven by UC passes the
Turnstile challenge as long as the driver's version_main MATCHES the installed
Chrome (a mismatch breaks UC's stealth patch → instant bot detection). So we
auto-detect the Chrome major version. Same pattern as the Lowes UC crawler.

Once warmed (home → consent → cf_clearance cookie), the session can:
  - navigate(url)           -> page_source (listing __PRELOADED_STATE__ / PDP SSR)
  - gql(operation, vars)    -> in-browser fetch() of a persisted GraphQL query

The GraphQL recipe (persisted hashes, pwa extension block, x-mms-* headers) is
reused from step00_pdp_browser, so reviews/summary/similar work identically to
the old ZenRows path — just locally now.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from common.pdp_browser import (
    GRAPHQL_BASE_HEADERS,
    build_gql_url,
    _comparison_vars,
    _reviews_vars,
    _summary_vars,
    detail_present,
)

MMKT_HOME = "https://www.mediamarkt.de/"
CONSENT_SELECTORS = (
    "#pwa-consent-layer-accept-all",
    "button[data-test='pwa-consent-layer-accept-all']",
)

# JS that runs an authenticated in-page fetch and hands the result back.
_XHR_JS = r"""
const done = arguments[arguments.length - 1];
const url = arguments[0], headers = arguments[1];
fetch(url, {credentials: 'include', headers})
  .then(async r => { let b=null; try { b = await r.text(); } catch(e){}
                     done({status: r.status, body: b}); })
  .catch(e => done({status: null, error: String(e)}));
"""


def chrome_version_main() -> int | None:
    """Installed Chrome major version (env override > registry). Avoids the
    version_main mismatch that silently breaks UC's stealth."""
    raw = os.getenv("MMKT_UC_VERSION_MAIN", "").strip()
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            pass
    try:
        import winreg

        for hive, path in (
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
        ):
            try:
                key = winreg.OpenKey(hive, path)
                value, _ = winreg.QueryValueEx(key, "version")
                return int(str(value).split(".")[0])
            except OSError:
                continue
    except Exception:
        pass
    return None


def _gql_headers(operation: str) -> dict[str, str]:
    return {
        **GRAPHQL_BASE_HEADERS,
        "x-operation": operation,
        "x-flow-id": str(uuid.uuid4()),
        "x-cacheable": "false" if operation == "GetReviewsSummary" else "true",
    }


class UcSession:
    """Warmed local UC Chrome for fetching MediaMarkt pages + GraphQL, ZenRows-free."""

    def __init__(
        self,
        *,
        headless: bool = False,
        nav_timeout_s: int = 70,
        script_timeout_s: int = 40,
        settle_s: float = 3.0,
        warmup_s: float = 4.0,
        review_pages: int = 2,
    ) -> None:
        self.headless = headless
        self.nav_timeout_s = nav_timeout_s
        self.script_timeout_s = script_timeout_s
        self.settle_s = settle_s
        self.warmup_s = warmup_s
        self.review_pages = review_pages
        self.driver = None
        self.version_main = chrome_version_main()
        self.warmup_status: dict[str, Any] = {}

    def __enter__(self) -> "UcSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        import undetected_chromedriver as uc

        options = uc.ChromeOptions()
        options.add_argument("--lang=de-DE")
        options.add_argument("--disable-blink-features=AutomationControlled")
        kwargs: dict[str, Any] = {"options": options, "headless": self.headless, "use_subprocess": True}
        if self.version_main:
            kwargs["version_main"] = self.version_main
        exe = os.getenv("MMKT_CHROME_EXE", "").strip()
        if exe:
            kwargs["browser_executable_path"] = exe
        self.driver = uc.Chrome(**kwargs)
        # remember the Chrome PID we launched so close() can kill its whole tree
        # (UC's quit() leaks chrome.exe children on Windows).
        self._browser_pid = getattr(self.driver, "browser_pid", None)
        self.driver.set_page_load_timeout(self.nav_timeout_s)
        self.driver.set_script_timeout(self.script_timeout_s)
        self._warmup()

    def _click_consent(self) -> str | None:
        from selenium.webdriver.common.by import By

        for sel in CONSENT_SELECTORS:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    els[0].click()
                    time.sleep(0.8)
                    return sel
            except Exception:
                continue
        return None

    def _blocked(self, src: str) -> bool:
        head = (src or "")[:4000]
        return "Nur einen Moment" in head or "Just a moment" in head or "ein Mensch sind" in head

    def _warmup(self) -> None:
        status: dict[str, Any] = {"home_blocked": None, "consent": None, "error": None}
        try:
            self.driver.get(MMKT_HOME)
            time.sleep(self.warmup_s)
            status["consent"] = self._click_consent()
            status["home_blocked"] = self._blocked(self.driver.page_source)
        except Exception as exc:
            status["error"] = type(exc).__name__ + ": " + str(exc)
        self.warmup_status = status

    def navigate(self, url: str, *, settle_s: float | None = None) -> dict[str, Any]:
        """Load a page; return {html, blocked, error}."""
        wait = self.settle_s if settle_s is None else settle_s
        html = ""
        error = None
        try:
            self.driver.get(url)
            time.sleep(wait)
            html = self.driver.page_source or ""
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        return {"html": html, "blocked": self._blocked(html), "error": error, "url": url}

    def gql(self, operation: str, variables: dict[str, Any]) -> dict[str, Any]:
        """In-browser fetch() of a persisted GraphQL query (uses page cookies)."""
        url = build_gql_url(operation, variables)
        try:
            res = self.driver.execute_async_script(_XHR_JS, url, _gql_headers(operation))
        except Exception as exc:
            return {"status": None, "error": type(exc).__name__ + ": " + str(exc), "body": None}
        body = res.get("body") if isinstance(res, dict) else None
        data = None
        if body:
            try:
                data = json.loads(body)
            except Exception:
                data = None
        return {"status": (res or {}).get("status"), "data": data, "raw": body}

    def reconnect(self) -> None:
        self.close()
        self.open()

    def fetch_pdp_detail(self, url: str, sku_id: str, *, review_pages: int | None = None) -> dict[str, Any]:
        """Same shape as PdpBrowserSession.fetch_pdp_detail, but local UC: navigate
        the PDP (SSR html) then in-page GraphQL for the 3 lazy fields."""
        review_pages = self.review_pages if review_pages is None else review_pages
        started = time.perf_counter()
        nav = self.navigate(url)
        html = nav["html"]
        summary = self.gql("GetReviewsSummary", _summary_vars(sku_id))
        reviews = [self.gql("GetProductReviews", _reviews_vars(sku_id, p))
                   for p in range(1, review_pages + 1)]
        comparison = self.gql("GetComparisonTableRecommendations", _comparison_vars(sku_id))
        return {
            "sku_id": sku_id,
            "url": url,
            "nav_status": 403 if nav["blocked"] else (200 if html else None),
            "detail_present": detail_present(html),
            "html": html,
            "summary_resp": summary.get("data"),
            "review_resps": [r.get("data") for r in reviews],
            "comparison_resp": comparison.get("data"),
            "gql_status": {
                "summary": summary.get("status"),
                "reviews": [r.get("status") for r in reviews],
                "comparison": comparison.get("status"),
            },
            "error": nav["error"],
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        }

    def close(self) -> None:
        pid = getattr(self, "_browser_pid", None)
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
        # Force-kill the Chrome process tree we launched (by PID — never touches
        # the user's own Chrome), since UC's quit() leaves orphans on Windows.
        if pid:
            try:
                import subprocess
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True)
            except Exception:
                pass
        self.driver = None
        self._browser_pid = None
