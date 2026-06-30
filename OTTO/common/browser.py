"""Synchronous browser session for OTTO PDP/review collection.

OTTO product detail pages are protected by Kasada (KPSDK). A single-shot fetch is
blocked, so we drive a real browser: warm up once (home -> listing) to accumulate
Kasada cookies/fingerprint, then reuse the same context to fetch each PDP.

Two modes:
  - "zenrows": connect over CDP to a remote ZenRows scraping browser. All traffic
    egresses through the ZenRows proxy in proxy_country, so the local IP is hidden.
  - "local": launch the local Chrome/Chromium. Traffic egresses through whatever
    the machine is using, so a German VPN MUST be active for OTTO SEG. A real
    local Chrome clears Kasada PDP challenges more reliably than the remote pool.

Playwright is imported lazily so importing this module never fails when no browser
transport is in use.
"""
from __future__ import annotations

import time
from typing import Any

from common.zenrows import DEFAULT_PROXY_COUNTRY, build_scraping_browser_url

OTTO_HOME = "https://www.otto.de/"
DEFAULT_WARMUP_LISTING_URL = "https://www.otto.de/suche/fernseher/"
# Universal "PDP loaded" signal across categories: the Details characteristics table
# (TV/REF/LDY all render dv_characteristicsTable). Plus model-name tokens as backup.
DETAIL_SIGNALS = (
    "dv_characteristicsTable",
    "Modellbezeichnung",
    "Modellkennung",
)
DETAIL_TABLE_SELECTOR = ".dv_characteristicsTable"
CONSENT_SELECTORS = (
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "button:has-text('OK')",
)


def detail_present(html: str) -> bool:
    return any(signal in html for signal in DETAIL_SIGNALS)


class BrowserSession:
    """Reusable warmed-up browser context for fetching OTTO PDPs and reviews.

    Use as a context manager:

        with BrowserSession(mode="local") as session:
            result = session.fetch_pdp(url)
    """

    def __init__(
        self,
        *,
        mode: str = "zenrows",
        proxy_country: str = DEFAULT_PROXY_COUNTRY,
        headless: bool = False,
        chrome_channel: str | None = "chrome",
        nav_timeout_ms: int = 90000,
        detail_wait_ms: int = 15000,
        settle_ms: int = 5000,
        warmup_wait_ms: int = 4000,
        max_attempts: int = 3,
        retry_backoff_ms: int = 6000,
        zenrows_params: dict[str, Any] | None = None,
        warmup_listing_url: str = DEFAULT_WARMUP_LISTING_URL,
    ) -> None:
        self.warmup_listing_url = warmup_listing_url
        self.mode = mode
        self.proxy_country = proxy_country
        self.headless = headless
        self.chrome_channel = chrome_channel
        self.nav_timeout_ms = nav_timeout_ms
        self.detail_wait_ms = detail_wait_ms
        self.settle_ms = settle_ms
        self.warmup_wait_ms = warmup_wait_ms
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff_ms = retry_backoff_ms
        self.zenrows_params = {} if zenrows_params is None else zenrows_params
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.warmup_status: dict[str, Any] = {}
        self.launch_info: dict[str, Any] = {}

    def __enter__(self) -> "BrowserSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._connect()

    def _launch_browser(self):
        """Create the underlying browser (remote ZenRows or local Chrome)."""
        if self.mode == "zenrows":
            self.launch_info = {"mode": "zenrows", "proxy_country": self.proxy_country}
            return self._playwright.chromium.connect_over_cdp(
                build_scraping_browser_url(proxy_country=self.proxy_country, **self.zenrows_params)
            )
        # local mode: prefer the real installed Chrome, fall back to bundled chromium
        if self.chrome_channel:
            try:
                browser = self._playwright.chromium.launch(channel=self.chrome_channel, headless=self.headless)
                self.launch_info = {"mode": "local", "channel": self.chrome_channel, "headless": self.headless}
                return browser
            except Exception as exc:
                self.launch_info = {"mode": "local", "channel_error": type(exc).__name__ + ": " + str(exc)}
        browser = self._playwright.chromium.launch(headless=self.headless)
        self.launch_info = {**self.launch_info, "mode": "local", "channel": None, "headless": self.headless}
        return browser

    def _connect(self) -> None:
        """Open a fresh browser session and warm it up.

        For zenrows mode each connection is a new proxy IP; for local mode the IP
        is fixed by the machine/VPN, but re-warming still refreshes Kasada state.
        """
        self._browser = self._launch_browser()
        self._context = self._browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1440, "height": 1400},
        )
        self._page = self._context.new_page()
        self._warmup()

    def reconnect(self) -> None:
        """Drop the current session and open a fresh one (new IP for zenrows).

        Re-running the warmup makes the next PDP look like the first PDP of a
        clean session, which is the condition under which Kasada lets it through.
        """
        self._teardown_connection()
        self._connect()

    def _teardown_connection(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
        ):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._context = self._browser = self._page = None

    def _click_consent(self) -> str | None:
        for selector in CONSENT_SELECTORS:
            try:
                locator = self._page.locator(selector).first
                if locator.count():
                    locator.click(timeout=3000)
                    self._page.wait_for_timeout(800)
                    return selector
            except Exception:
                continue
        return None

    def _warmup(self) -> None:
        # Best-effort: a blocked/closed session here must not crash the batch.
        # fetch_pdp retries with a fresh session if warmup was incomplete.
        status: dict[str, Any] = {"home_status": None, "listing_status": None, "consent_selector": None, "error": None}
        try:
            home = self._page.goto(OTTO_HOME, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            status["home_status"] = home.status if home else None
            status["consent_selector"] = self._click_consent()
            self._page.wait_for_timeout(self.warmup_wait_ms)
            listing = self._page.goto(self.warmup_listing_url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            status["listing_status"] = listing.status if listing else None
            self._page.wait_for_timeout(self.warmup_wait_ms)
        except Exception as exc:
            status["error"] = type(exc).__name__ + ": " + str(exc)
        self.warmup_status = status

    def fetch_pdp(self, url: str) -> dict[str, Any]:
        """Fetch one PDP, retrying until detail signals appear.

        On success the returned status is normalized to 200 because Kasada often
        reports 429 on the navigation response even when the resolved page body
        contains the real detail content. The raw navigation status is kept under
        nav_status for diagnostics.
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        started = time.perf_counter()
        nav_status: int | None = None
        wait_state = "not_attempted"
        html = ""
        error = None
        attempt = 0

        for attempt in range(1, self.max_attempts + 1):
            try:
                if attempt > 1:
                    # Kasada blocked this attempt: cool down, then rotate to a
                    # fresh session (new IP for zenrows; re-warm for local).
                    time.sleep(self.retry_backoff_ms / 1000 * attempt)
                    try:
                        self._teardown_connection()
                        self._connect()
                    except Exception as exc:
                        error = "reconnect_failed: " + type(exc).__name__ + ": " + str(exc)
                        continue
                # Navigate as if the PDP were clicked from the listing: a real
                # Referer + same-origin sec-fetch-site satisfies Kasada, which
                # blocks bare direct navigations to the PDP route.
                response = self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms, referer=self.warmup_listing_url
                )
                nav_status = response.status if response else None
                try:
                    self._page.wait_for_selector(DETAIL_TABLE_SELECTOR, timeout=self.detail_wait_ms)
                    wait_state = "table_ok"
                except PWTimeout:
                    # Kasada interstitial may still be resolving its JS challenge
                    # and will navigate to the real PDP on its own. Wait for the
                    # network to settle, then re-check.
                    wait_state = "table_timeout"
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=self.detail_wait_ms)
                    except PWTimeout:
                        pass
                self._page.wait_for_timeout(self.settle_ms)
                html = self._page.content()
                error = None
                if detail_present(html):
                    wait_state = wait_state if wait_state == "table_ok" else "resolved_after_challenge"
                    break
            except Exception as exc:  # navigation error, keep last html/state
                error = type(exc).__name__ + ": " + str(exc)

        body = html.encode("utf-8", errors="replace")
        present = detail_present(html)
        return {
            "status": 200 if present else nav_status,
            "nav_status": nav_status,
            "final_url": self._page.url if self._page else url,
            "content_type": "text/html",
            "body": body,
            "error": error,
            "detail_present": present,
            "wait_state": wait_state,
            "attempts": attempt,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    def fetch_html(self, url: str) -> dict[str, Any]:
        """Fetch a plain page (e.g. the review page) through the warmed context."""
        from playwright.sync_api import TimeoutError as PWTimeout

        started = time.perf_counter()
        nav_status: int | None = None
        html = ""
        error = None
        try:
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            nav_status = response.status if response else None
            try:
                self._page.wait_for_load_state("networkidle", timeout=self.detail_wait_ms)
            except PWTimeout:
                pass
            self._page.wait_for_timeout(self.settle_ms)
            html = self._page.content()
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        return {
            "status": nav_status,
            "nav_status": nav_status,
            "final_url": self._page.url if self._page else url,
            "content_type": "text/html",
            "body": html.encode("utf-8", errors="replace"),
            "error": error,
            "attempts": 1,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    def close(self) -> None:
        self._teardown_connection()
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
