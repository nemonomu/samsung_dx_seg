"""Local browser transport for Amazon.de.

This mirrors the SIEL Amazon crawler's collection style: use a local
undetected_chromedriver session, set a DE locale/location, recover common Amazon
interstitials, and scroll before returning HTML for parser-based extraction.
"""
from __future__ import annotations

import os
import random
import sys
import time
from typing import Any

import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By

from common import siel_logging as siel_log


uc.Chrome.__del__ = lambda self: None


_AMAZON_TECHNICAL_ERROR_MARKERS = (
    "tut uns leid",
    "ist ein technischer fehler aufgetreten",
    "bitte schauen sie später wieder vorbei",
)


def classify_amazon_page(html: str | None, title: str | None = None) -> str | None:
    """Classify known Amazon error pages without relying on a synthetic HTTP status."""
    source = str(html or "").lower()
    page_title = str(title or "").lower()
    if (
        "robot check" in page_title
        or "sorry, we just need to make sure" in source
        or "/errors/validatecaptcha" in source
        or "bm-verify" in source
        or "_sec/verify" in source
        or "request was throttled" in source
        or "continue shopping" in source
        or "click the button below to continue shopping" in source
    ):
        return "amazon_interstitial"
    if sum(marker in source for marker in _AMAZON_TECHNICAL_ERROR_MARKERS) >= 2:
        return "amazon_technical_error"
    return None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _chrome_major() -> int | None:
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    keys = (
        r"SOFTWARE\Google\Chrome\BLBeacon",
        r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon",
    )
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "version")
        except OSError:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                    version, _ = winreg.QueryValueEx(handle, "version")
            except OSError:
                continue
        try:
            return int(str(version).split(".", 1)[0])
        except (TypeError, ValueError):
            return None
    return None


class AmazonBrowserSession:
    def __init__(self, *, postal_code: str = "10117", sleep: float = 1.5,
                 headless: bool | None = None, page_load_strategy: str | None = None,
                 set_postal: bool | None = None):
        self.postal_code = postal_code
        self.sleep = sleep
        self.headless = _truthy(os.getenv("AMZN_HEADLESS")) if headless is None else headless
        self.page_load_strategy = page_load_strategy or os.getenv("AMZN_PAGE_LOAD_STRATEGY")
        self.set_postal = _truthy(os.getenv("AMZN_SET_POSTAL_CODE")) if set_postal is None else set_postal
        self.driver: uc.Chrome | None = None

    def open(self) -> None:
        if self.driver is not None:
            return
        opts = uc.ChromeOptions()
        if self.page_load_strategy:
            opts.set_capability("pageLoadStrategy", self.page_load_strategy)
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--start-maximized")
        opts.add_argument("--lang=de-DE")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling")
        opts.add_experimental_option("prefs", {"intl.accept_languages": "de-DE,de,en-US,en"})
        kwargs: dict[str, Any] = {"options": opts}
        major = _chrome_major()
        if major:
            kwargs["version_main"] = major
        self.driver = uc.Chrome(**kwargs)
        siel_log.run_log(
            f"new driver started headless={self.headless} "
            f"page_load_strategy={self.page_load_strategy or 'normal'} "
            f"set_postal={self.set_postal} chrome_major={major}"
        )
        try:
            self.driver.set_page_load_timeout(int(os.getenv("AMZN_PAGE_LOAD_TIMEOUT", "75")))
            self.driver.set_script_timeout(30)
            self.driver.set_window_rect(0, 0, 1920, 1080)
        except WebDriverException:
            pass
        try:
            self.driver.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
        except WebDriverException:
            pass
        if self.set_postal and self.postal_code:
            self.set_postal_code()

    def close(self) -> None:
        if self.driver is None:
            return
        siel_log.run_log("driver shutdown start")
        try:
            self.driver.quit()
            siel_log.run_log("driver shutdown done")
        except Exception as exc:
            siel_log.run_log(f"driver shutdown failed: {type(exc).__name__}: {exc}", "ERROR")
        finally:
            self.driver = None

    def restart(self, reason: str = "requested") -> None:
        """Replace the temporary WebDriver session while preserving session settings."""
        siel_log.run_log(f"driver restart reason={reason}", "WARNING")
        self.close()
        self.open()

    def _click_if_present(self, by: str, selector: str, timeout_sleep: float = 0.5) -> bool:
        if self.driver is None:
            return False
        try:
            els = self.driver.find_elements(by, selector)
            for el in els:
                if el.is_displayed():
                    el.click()
                    time.sleep(timeout_sleep)
                    return True
        except (NoSuchElementException, WebDriverException):
            return False
        return False

    def set_postal_code(self) -> bool:
        if self.driver is None:
            return False
        try:
            self.driver.get("https://www.amazon.de")
            time.sleep(random.uniform(2.0, 3.5))
            self._click_if_present(By.ID, "sp-cc-accept", 0.8)
            link = self.driver.find_elements(By.CSS_SELECTOR, "#nav-global-location-popover-link")
            if not link:
                return False
            text = (link[0].text or "").strip()
            if self.postal_code and self.postal_code in text:
                return True
            link[0].click()
            time.sleep(random.uniform(1.0, 2.0))
            boxes = self.driver.find_elements(By.CSS_SELECTOR, "#GLUXZipUpdateInput")
            if not boxes:
                return False
            boxes[0].clear()
            boxes[0].send_keys(self.postal_code)
            time.sleep(random.uniform(0.5, 1.0))
            for selector in ("#GLUXZipUpdate input[type='submit']", "#GLUXZipUpdate-announce"):
                if self._click_if_present(By.CSS_SELECTOR, selector, 1.5):
                    break
            time.sleep(random.uniform(1.5, 2.5))
            for selector in (
                "button[name='glowDoneButton']",
                "#GLUXConfirmClose",
                ".a-popover-header button[data-action='a-popover-close']",
            ):
                if self._click_if_present(By.CSS_SELECTOR, selector, 0.5):
                    break
            return True
        except WebDriverException as exc:
            msg = f"[browser] postal_code setup skipped: {type(exc).__name__}: {str(exc)[:160]}"
            print(msg, file=sys.stderr)
            siel_log.run_log(msg, "WARNING")
            return False

    def recover(self, url: str = "", cycles: int = 3) -> bool:
        if self.driver is None:
            return False
        for attempt in range(cycles):
            try:
                title = (self.driver.title or "").lower()
                source = (self.driver.page_source or "").lower()
            except WebDriverException:
                return False
            blocked = classify_amazon_page(source, title) == "amazon_interstitial"
            if not blocked:
                return True
            clicked = self._click_if_present(
                By.XPATH,
                "//*[self::button or self::a or self::input][contains("
                "translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                " 'continue shopping') or contains("
                "translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'),"
                " 'continue shopping')]",
                2.0,
            )
            if not clicked:
                time.sleep(random.uniform(4.0, 7.0) if attempt else random.uniform(2.0, 4.0))
                try:
                    self.driver.refresh()
                except WebDriverException:
                    if url:
                        self.driver.get(url)
            time.sleep(random.uniform(2.0, 4.0))
        return False

    def scroll(self, *, ratio: float = 1.0, pause: float = 0.7, max_scrolls: int = 30) -> None:
        if self.driver is None:
            return
        ratio = min(max(ratio, 0.0), 1.0)
        try:
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)
            last_y = -1
            for _ in range(max_scrolls):
                y = int(self.driver.execute_script("return window.scrollY || 0") or 0)
                height = int(self.driver.execute_script(
                    "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                ) or 0)
                inner = int(self.driver.execute_script("return window.innerHeight || 0") or 0)
                target = max(int((height - inner) * ratio), 0)
                if y >= target or y == last_y:
                    break
                last_y = y
                step = random.randint(450, 850)
                self.driver.execute_script("window.scrollTo(0, arguments[0]);", min(y + step, target))
                time.sleep(random.uniform(max(pause * 0.5, 0.25), pause + 0.35))
        except WebDriverException as exc:
            msg = f"[browser] scroll skipped: {type(exc).__name__}: {str(exc)[:160]}"
            print(msg, file=sys.stderr)
            siel_log.run_log(msg, "WARNING")

    def fetch(self, url: str, *, scroll_ratio: float = 1.0,
              scroll_pause: float | None = None, scroll_max_scrolls: int | None = None,
              post_load_sleep: float | None = None) -> dict[str, Any]:
        self.open()
        assert self.driver is not None
        started = time.perf_counter()
        siel_log.run_log(f"fetch start url={url}")
        try:
            self.driver.get(url)
            sleep_seconds = post_load_sleep if post_load_sleep is not None else max(self.sleep, 3.0)
            time.sleep(sleep_seconds)
            recovered = self.recover(url, cycles=1)
            self.scroll(
                ratio=scroll_ratio,
                pause=0.45 if scroll_pause is None else scroll_pause,
                max_scrolls=8 if scroll_max_scrolls is None else scroll_max_scrolls,
            )
            recovered = self.recover(url, cycles=1)
            html = self.driver.page_source or ""
            page_error = classify_amazon_page(html, self.driver.title or "")
            if page_error == "amazon_technical_error":
                status = 503
                error = page_error
            elif page_error == "amazon_interstitial" or not recovered:
                status = 429
                error = "amazon_interstitial"
            else:
                status = 200
                error = None
            result = {
                "url": self.driver.current_url,
                "status": status,
                "text": html,
                "bytes": len(html.encode("utf-8", errors="replace")),
                "error": error,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
            siel_log.run_log(
                f"fetch done status={result['status']} bytes={result['bytes']} "
                f"elapsed={result['elapsed_seconds']} url={result['url']} error={result['error']}"
            )
            return result
        except WebDriverException as exc:
            result = {
                "url": url,
                "status": None,
                "text": "",
                "bytes": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
            siel_log.run_log(
                f"fetch failed status=None elapsed={result['elapsed_seconds']} "
                f"url={url} error={result['error']}",
                "ERROR",
            )
            return result

    def refetch_without_cache(self, url: str, *, wait_range: tuple[float, float] = (2.0, 4.0),
                              scroll_ratio: float = 1.0, scroll_pause: float | None = None,
                              scroll_max_scrolls: int | None = None,
                              post_load_sleep: float | None = None) -> dict[str, Any]:
        """Reload one PDP in the current session while temporarily bypassing Chrome cache."""
        self.open()
        assert self.driver is not None
        low, high = wait_range
        time.sleep(random.uniform(min(low, high), max(low, high)))
        cache_disabled = False
        try:
            try:
                self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
                cache_disabled = True
            except WebDriverException as exc:
                siel_log.run_log(
                    f"cache bypass unavailable; continuing with normal reload: {type(exc).__name__}: {exc}",
                    "WARNING",
                )
            return self.fetch(
                url,
                scroll_ratio=scroll_ratio,
                scroll_pause=scroll_pause,
                scroll_max_scrolls=scroll_max_scrolls,
                post_load_sleep=post_load_sleep,
            )
        finally:
            if cache_disabled:
                try:
                    self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": False})
                except WebDriverException as exc:
                    siel_log.run_log(f"cache restore failed: {type(exc).__name__}: {exc}", "WARNING")

    def refetch(self, url: str, *, wait_range: tuple[float, float] = (5.0, 10.0),
                scroll_ratio: float = 1.0, scroll_pause: float | None = None,
                scroll_max_scrolls: int | None = None,
                post_load_sleep: float | None = None) -> dict[str, Any]:
        """Retry one failed page load once without changing Chrome's cache policy."""
        self.open()
        assert self.driver is not None
        try:
            self.driver.execute_script("window.stop();")
        except WebDriverException:
            pass
        low, high = wait_range
        wait_seconds = random.uniform(min(low, high), max(low, high))
        time.sleep(wait_seconds)
        result = self.fetch(
            url,
            scroll_ratio=scroll_ratio,
            scroll_pause=scroll_pause,
            scroll_max_scrolls=scroll_max_scrolls,
            post_load_sleep=post_load_sleep,
        )
        result["retry_mode"] = "normal_cache"
        result["retry_wait_seconds"] = round(wait_seconds, 2)
        return result
