from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


def _load_browser_module():
    fake_uc = types.ModuleType("undetected_chromedriver")
    fake_uc.Chrome = type("Chrome", (), {})
    fake_uc.ChromeOptions = type("ChromeOptions", (), {})
    with patch.dict(sys.modules, {"undetected_chromedriver": fake_uc}):
        sys.modules.pop("common.browser", None)
        return importlib.import_module("common.browser")


browser_module = _load_browser_module()


class Driver:
    def __init__(self) -> None:
        self.cache_commands: list[bool] = []

    def execute_cdp_cmd(self, command: str, params: dict[str, bool]) -> None:
        if command == "Network.setCacheDisabled":
            self.cache_commands.append(params["cacheDisabled"])


class CacheUnavailableDriver(Driver):
    def execute_cdp_cmd(self, command: str, params: dict[str, bool]) -> None:
        super().execute_cdp_cmd(command, params)
        raise browser_module.WebDriverException("CDP unavailable")


class FetchDriver:
    current_url = "https://www.amazon.de/dp/B0TEST"
    page_source = "<html>test</html>"

    def get(self, _url: str) -> None:
        pass


def _session(fetch_result=None, fetch_error: Exception | None = None):
    session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
    session.driver = Driver()
    session.open = Mock()
    session.fetch = Mock(side_effect=fetch_error, return_value=fetch_result)
    return session


class BrowserRefetchTests(unittest.TestCase):
    def test_cache_is_disabled_for_refetch_then_restored(self) -> None:
        expected = {"status": 200, "text": "retry"}
        session = _session(fetch_result=expected)

        actual = session.refetch_without_cache("https://www.amazon.de/dp/B0TEST", wait_range=(0, 0))

        self.assertEqual(actual, expected)
        self.assertEqual(session.driver.cache_commands, [True, False])
        session.fetch.assert_called_once()

    def test_cache_is_restored_when_refetch_raises(self) -> None:
        session = _session(fetch_error=RuntimeError("fetch failed"))

        with self.assertRaisesRegex(RuntimeError, "fetch failed"):
            session.refetch_without_cache("https://www.amazon.de/dp/B0TEST", wait_range=(0, 0))

        self.assertEqual(session.driver.cache_commands, [True, False])

    def test_refetch_continues_when_cache_bypass_is_unavailable(self) -> None:
        expected = {"status": 200, "text": "retry"}
        session = _session(fetch_result=expected)
        session.driver = CacheUnavailableDriver()

        with patch.object(browser_module.siel_log, "run_log"):
            actual = session.refetch_without_cache("https://www.amazon.de/dp/B0TEST", wait_range=(0, 0))

        self.assertEqual(actual, expected)
        self.assertEqual(session.driver.cache_commands, [True])
        session.fetch.assert_called_once()

    def test_fetch_status_uses_the_final_recovery_check(self) -> None:
        session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
        session.driver = FetchDriver()
        session.open = Mock()
        session.recover = Mock(side_effect=[True, False])
        session.scroll = Mock()

        with (
            patch.object(browser_module.time, "sleep"),
            patch.object(browser_module.siel_log, "run_log"),
        ):
            blocked = session.fetch("https://www.amazon.de/dp/B0TEST", post_load_sleep=0)

        self.assertEqual(blocked["status"], 429)
        self.assertEqual(blocked["error"], "amazon_interstitial")

        session.recover = Mock(side_effect=[False, True])
        with (
            patch.object(browser_module.time, "sleep"),
            patch.object(browser_module.siel_log, "run_log"),
        ):
            recovered = session.fetch("https://www.amazon.de/dp/B0TEST", post_load_sleep=0)

        self.assertEqual(recovered["status"], 200)
        self.assertIsNone(recovered["error"])


if __name__ == "__main__":
    unittest.main()
