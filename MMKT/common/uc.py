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

# Same, but a plain document fetch (no custom headers) → raw SSR HTML text.
_XHR_TEXT_JS = r"""
const done = arguments[arguments.length - 1];
const url = arguments[0];
fetch(url, {credentials: 'include'})
  .then(async r => { let b=null; try { b = await r.text(); } catch(e){}
                     done({status: r.status, body: b}); })
  .catch(e => done({status: null, error: String(e)}));
"""


CHROME_EXE_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)


def _version_from_registry() -> int | None:
    try:
        import winreg
    except Exception:
        return None
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
            continue
    return None


def _version_from_executable() -> int | None:
    """Read the Chrome major version straight off the installed chrome.exe —
    the registry-independent fallback. Two ways: the versioned subfolder Chrome
    keeps next to chrome.exe (e.g. Application\\150.0.7632.5\\), else the exe's
    ProductVersion via PowerShell."""
    import glob
    import subprocess

    exes = [os.getenv("MMKT_CHROME_EXE", "").strip(), *CHROME_EXE_CANDIDATES]
    for exe in exes:
        if not exe or not os.path.exists(exe):
            continue
        app_dir = os.path.dirname(exe)
        # Chrome drops a folder named after the exact version beside chrome.exe.
        majors = []
        for d in glob.glob(os.path.join(app_dir, "*.*.*.*")):
            base = os.path.basename(d).split(".")[0]
            if os.path.isdir(d) and base.isdigit():
                majors.append(int(base))
        if majors:
            return max(majors)
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=15)
            ver = (out.stdout or "").strip().split(".")[0]
            if ver.isdigit():
                return int(ver)
        except Exception:
            continue
    return None


def chrome_version_main() -> int | None:
    """Installed Chrome major version (env override > newest on disk). Runs on
    every session, so it auto-tracks Chrome auto-updates: bump Chrome and the
    next run downloads the matching driver.

    Right after an auto-update BLBeacon (registry) still names the last-RUN
    version while a newer one already sits on disk. UC launches a FRESH
    chrome.exe, which runs the NEWEST installed version — so we take the max of
    what the registry and the on-disk version folders report. Trusting the
    stale registry value here is exactly what causes the driver/Chrome mismatch
    that silently breaks UC's stealth."""
    raw = os.getenv("MMKT_UC_VERSION_MAIN", "").strip()
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            pass
    reg = _version_from_registry()
    exe = _version_from_executable()
    candidates = [v for v in (reg, exe) if v]
    if not candidates:
        return None
    if reg and exe and reg != exe:
        print(f"[uc] Chrome version: registry={reg}, on-disk={exe} — a fresh launch "
              f"runs the newest ({max(candidates)}), using that for the driver.", flush=True)
    return max(candidates)


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
        settle_s: float = 1.2,
        warmup_s: float = 3.0,
        review_pages: int = 4,
        performance_logging: bool = False,
        block_images: bool = True,
    ) -> None:
        self.headless = headless
        self.nav_timeout_s = nav_timeout_s
        self.script_timeout_s = script_timeout_s
        self.settle_s = settle_s
        self.warmup_s = warmup_s
        self.review_pages = review_pages
        self.performance_logging = performance_logging
        self.block_images = block_images
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

        opened_at = time.perf_counter()
        options = uc.ChromeOptions()
        options.add_argument("--lang=de-DE")
        options.add_argument("--disable-blink-features=AutomationControlled")
        # Skip images — big bandwidth/time win; SSR HTML + apollo + GraphQL are
        # unaffected (we never read pixels). Cloudflare's JS challenge still runs.
        if self.block_images:
            options.add_argument("--blink-settings=imagesEnabled=false")
        if self.performance_logging:
            # Diagnostic-only CDP events. Disabled for normal batches to avoid
            # accumulating a large in-memory performance log.
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        kwargs: dict[str, Any] = {"options": options, "headless": self.headless, "use_subprocess": True}
        if self.version_main:
            kwargs["version_main"] = self.version_main
            print(f"[uc] Chrome major {self.version_main} detected — matching driver auto-selected", flush=True)
        else:
            print("[uc] WARNING: could not detect installed Chrome version — UC will guess a "
                  "driver, which may mismatch and trigger a Cloudflare block. Set MMKT_UC_VERSION_MAIN "
                  "to the installed Chrome major version if collection fails.", flush=True)
        exe = os.getenv("MMKT_CHROME_EXE", "").strip()
        if exe:
            kwargs["browser_executable_path"] = exe
        print(f"[uc] launching Chrome headless={self.headless} timeout={self.nav_timeout_s}s ...", flush=True)
        self.driver = uc.Chrome(**kwargs)
        print(f"[uc] Chrome launched ({time.perf_counter() - opened_at:.1f}s); configuring session ...", flush=True)
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
        started = time.perf_counter()
        try:
            print(f"[uc] warmup: opening MediaMarkt home (timeout={self.nav_timeout_s}s) ...", flush=True)
            self.driver.get(MMKT_HOME)
            print(f"[uc] warmup: home loaded; waiting {self.warmup_s:.1f}s for Cloudflare/consent ...", flush=True)
            time.sleep(self.warmup_s)
            status["consent"] = self._click_consent()
            status["home_blocked"] = self._blocked(self.driver.page_source)
        except Exception as exc:
            status["error"] = type(exc).__name__ + ": " + str(exc)
        self.warmup_status = status
        print(f"[uc] warmup: finished in {time.perf_counter() - started:.1f}s "
              f"blocked={status['home_blocked']} consent={status['consent'] or 'none'}", flush=True)
        if status.get("home_blocked"):
            print(f"[uc] WARNING: home page BLOCKED by Cloudflare after warmup "
                  f"(Chrome major={self.version_main}). Most likely a driver/Chrome "
                  f"version mismatch or a flagged IP. Collection will churn until fixed.", flush=True)
        elif status.get("error"):
            print(f"[uc] WARNING: warmup error: {status['error']}", flush=True)

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

    def fetch_page_text(self, url: str) -> str:
        """In-browser fetch() of a page URL → raw SSR HTML text (uses page
        cookies, no navigation/render). Used to recover fields that live only in
        the PDP description body (e.g. REF ref_capacity)."""
        try:
            res = self.driver.execute_async_script(_XHR_TEXT_JS, url)
        except Exception:
            return ""
        if isinstance(res, dict):
            return res.get("body") or ""
        return ""

    def _gql_many(self, specs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Fire several persisted GraphQL fetches CONCURRENTLY (Promise.all) in one
        round trip — much faster than sequential per-PDP."""
        calls = [{"url": build_gql_url(op, v), "headers": _gql_headers(op)} for op, v in specs]
        js = (
            "const done = arguments[arguments.length-1];"
            "Promise.all(arguments[0].map(async (c)=>{"
            " try { const r = await fetch(c.url,{credentials:'include',headers:c.headers});"
            "       let b=null; try{ b = await r.text(); }catch(e){}"
            "       return {status:r.status, body:b, contentType:r.headers.get('content-type')||''}; }"
            " catch(e){ return {status:null, error:String(e)}; }"
            "})).then(done);"
        )
        try:
            results = self.driver.execute_async_script(js, calls)
        except Exception as exc:
            results = [{"status": None, "error": type(exc).__name__ + ": " + str(exc)}] * len(calls)
        out: list[dict[str, Any]] = []
        for res in (results or []):
            body = res.get("body") if isinstance(res, dict) else None
            data = None
            if body:
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
            status = (res or {}).get("status")
            # Keep only a short response preview for diagnostics. Request
            # headers/cookies are deliberately not returned or logged.
            preview = None
            if status != 200 and body:
                preview = " ".join(body.split())[:300]
            out.append({
                "status": status,
                "data": data,
                "content_type": (res or {}).get("contentType"),
                "body_preview": preview,
                "transport_error": (res or {}).get("error"),
            })
        return out

    def reconnect(self) -> None:
        self.close()
        self.open()

    def fetch_pdp_detail(self, url: str, sku_id: str, *, review_pages: int | None = None) -> dict[str, Any]:
        """Same shape as PdpBrowserSession.fetch_pdp_detail, but local UC: navigate
        the PDP (SSR html) then in-page GraphQL for the 3 lazy fields."""
        review_pages = self.review_pages if review_pages is None else review_pages
        started = time.perf_counter()
        # Open the PDP first. MediaMarkt's API rejects GraphQL calls made
        # directly from the warmed home page with an HTML 403 response; running
        # fetch() from the PDP supplies the expected Referer/browser state.
        nav = self.navigate(url)
        html = nav["html"]
        # GetComparisonTableRecommendations carries the main product's
        # specs/delivery/pickup/ratings + similar; summary + reviews come
        # alongside. All GraphQL calls still run in one concurrent round trip.
        specs = [
            ("GetComparisonTableRecommendations", _comparison_vars(sku_id)),
            ("GetReviewsSummary", _summary_vars(sku_id)),
        ]
        specs += [("GetProductReviews", _reviews_vars(sku_id, p)) for p in range(1, review_pages + 1)]
        results = self._gql_many(specs)
        comparison = results[0]
        summary = results[1]
        reviews = results[2:2 + review_pages]
        failed_statuses: dict[str, list[str]] = {}
        first_failure: dict[str, Any] | None = None
        for (operation, _), result in zip(specs, results):
            if result.get("status") == 200:
                continue
            failed_statuses.setdefault(operation, []).append(str(result.get("status")))
            if first_failure is None:
                first_failure = result
        error = None
        if failed_statuses:
            statuses = " ".join(
                f"{operation}={','.join(values)}"
                for operation, values in failed_statuses.items()
            )
            detail = first_failure or {}
            extras = []
            if detail.get("content_type"):
                extras.append(f"content_type={detail['content_type']}")
            if detail.get("transport_error"):
                extras.append(f"transport={detail['transport_error']}")
            if detail.get("body_preview"):
                extras.append(f"body={detail['body_preview']}")
            error = "gql_failed " + statuses
            if extras:
                error += " " + " ".join(extras)
        nav_errors = []
        if nav.get("blocked"):
            nav_errors.append("pdp_blocked")
        if nav.get("error"):
            nav_errors.append(f"pdp_navigation={nav['error']}")
        if nav_errors:
            error = " ".join(nav_errors + ([error] if error else []))
        return {
            "sku_id": sku_id,
            "url": url,
            "nav_status": comparison.get("status"),
            "detail_present": comparison.get("status") == 200 or detail_present(html),
            "html": html,
            "comparison_resp": comparison.get("data"),
            "summary_resp": summary.get("data"),
            "review_resps": [r.get("data") for r in reviews],
            "gql_status": {
                "comparison": comparison.get("status"),
                "summary": summary.get("status"),
                "reviews": [r.get("status") for r in reviews],
            },
            "error": error,
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
