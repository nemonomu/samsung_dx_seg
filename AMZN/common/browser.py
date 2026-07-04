"""Local browser transport for Amazon.de.

This mirrors the SIEL Amazon crawler's collection style: use a local
undetected_chromedriver session, set a DE locale/location, recover common Amazon
interstitials, and scroll before returning HTML for parser-based extraction.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any

import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By


uc.Chrome.__del__ = lambda self: None


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
                 headless: bool | None = None, page_load_strategy: str | None = None):
        self.postal_code = postal_code
        self.sleep = sleep
        self.headless = _truthy(os.getenv("AMZN_HEADLESS")) if headless is None else headless
        self.page_load_strategy = page_load_strategy or os.getenv("AMZN_PAGE_LOAD_STRATEGY")
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
        self.set_postal_code()

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:
            pass
        finally:
            self.driver = None

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
            print(f"[browser] postal_code setup skipped: {type(exc).__name__}: {str(exc)[:160]}")
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
            blocked = (
                "robot check" in title
                or "sorry, we just need to make sure" in source
                or "/errors/validatecaptcha" in source
                or "bm-verify" in source
                or "_sec/verify" in source
                or "request was throttled" in source
            )
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
            print(f"[browser] scroll skipped: {type(exc).__name__}: {str(exc)[:160]}")

    def fetch(self, url: str, *, scroll_ratio: float = 1.0) -> dict[str, Any]:
        self.open()
        assert self.driver is not None
        started = time.perf_counter()
        try:
            self.driver.get(url)
            time.sleep(max(self.sleep, random.uniform(2.0, 3.5)))
            recovered = self.recover(url)
            self.scroll(ratio=scroll_ratio)
            self.recover(url, cycles=1)
            html = self.driver.page_source or ""
            return {
                "url": self.driver.current_url,
                "status": 200 if recovered else 429,
                "text": html,
                "bytes": len(html.encode("utf-8", errors="replace")),
                "error": None if recovered else "amazon_interstitial",
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
        except WebDriverException as exc:
            return {
                "url": url,
                "status": None,
                "text": "",
                "bytes": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "elapsed_seconds": round(time.perf_counter() - started, 2),
            }
