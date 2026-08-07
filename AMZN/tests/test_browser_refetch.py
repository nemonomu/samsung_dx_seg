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
        self.scripts: list[str] = []

    def execute_cdp_cmd(self, command: str, params: dict[str, bool]) -> None:
        if command == "Network.setCacheDisabled":
            self.cache_commands.append(params["cacheDisabled"])

    def execute_script(self, script: str) -> None:
        self.scripts.append(script)


class CacheUnavailableDriver(Driver):
    def execute_cdp_cmd(self, command: str, params: dict[str, bool]) -> None:
        super().execute_cdp_cmd(command, params)
        raise browser_module.WebDriverException("CDP unavailable")


class FetchDriver:
    current_url = "https://www.amazon.de/dp/B0TEST"
    page_source = "<html>test</html>"
    title = "Amazon.de"

    def get(self, _url: str) -> None:
        pass


def _session(fetch_result=None, fetch_error: Exception | None = None):
    session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
    session.driver = Driver()
    session.open = Mock()
    session.fetch = Mock(side_effect=fetch_error, return_value=fetch_result)
    return session


class BrowserRefetchTests(unittest.TestCase):
    def test_fatal_browser_error_matches_dead_session_messages_only(self) -> None:
        for message in (
            "Message: tab crashed",
            "invalid session id",
            "disconnected: not connected to DevTools",
            "no such window",
            "chrome not reachable",
            "connection refused by local driver service",
            "WinError 10061",
        ):
            with self.subTest(message=message):
                self.assertIsNotNone(browser_module.fatal_browser_error({"error": message}))

        self.assertIsNone(browser_module.fatal_browser_error({
            "error": "TimeoutException: timed out receiving message from renderer",
        }))
        self.assertIsNone(browser_module.fatal_browser_error({"error": "amazon_interstitial"}))

    def test_classifies_captured_amazon_de_technical_error_page(self) -> None:
        html = """
        <h1>Tut uns Leid!</h1>
        <p>Während wir Ihre Eingabe ausführen wollten, ist ein technischer Fehler aufgetreten.</p>
        <p>Bitte schauen Sie später wieder vorbei.</p>
        """

        self.assertEqual(
            browser_module.classify_amazon_page(html),
            "amazon_technical_error",
        )

    def test_technical_error_requires_multiple_markers(self) -> None:
        self.assertIsNone(browser_module.classify_amazon_page("Tut uns Leid!"))

    def test_classifies_existing_amazon_interstitial(self) -> None:
        self.assertEqual(
            browser_module.classify_amazon_page(
                "Sorry, we just need to make sure you're not a robot",
                "Robot Check",
            ),
            "amazon_interstitial",
        )

    def test_normal_amazon_page_is_not_classified_as_error(self) -> None:
        self.assertIsNone(
            browser_module.classify_amazon_page(
                '<div data-component-type="s-search-result" data-asin="B0TEST"></div>',
                "Amazon.de : Kühlschränke",
            )
        )

    def test_normal_refetch_keeps_cache_enabled_and_stops_previous_load(self) -> None:
        expected = {"status": 200, "text": "retry"}
        session = _session(fetch_result=expected)

        actual = session.refetch("https://www.amazon.de/dp/B0TEST", wait_range=(0, 0))

        self.assertEqual(actual["status"], 200)
        self.assertEqual(actual["retry_mode"], "normal_cache")
        self.assertEqual(actual["retry_wait_seconds"], 0)
        self.assertEqual(session.driver.cache_commands, [])
        self.assertEqual(session.driver.scripts, ["window.stop();"])
        session.fetch.assert_called_once()

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

    def test_fetch_classifies_technical_error_as_synthetic_503(self) -> None:
        session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
        session.driver = FetchDriver()
        session.driver.page_source = """
        <h1>Tut uns Leid!</h1>
        <p>Es ist ein technischer Fehler aufgetreten.</p>
        <p>Bitte schauen Sie später wieder vorbei.</p>
        """
        session.open = Mock()
        session.recover = Mock(return_value=True)
        session.scroll = Mock()

        with (
            patch.object(browser_module.time, "sleep"),
            patch.object(browser_module.siel_log, "run_log"),
        ):
            result = session.fetch("https://www.amazon.de/s?k=test", post_load_sleep=0)

        self.assertEqual(result["status"], 503)
        self.assertEqual(result["error"], "amazon_technical_error")

    def test_restart_replaces_driver_using_same_session_object(self) -> None:
        session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
        first_driver = object()
        second_driver = object()
        session.driver = first_driver

        def close() -> None:
            session.driver = None

        def open_session() -> None:
            session.driver = second_driver

        session.close = Mock(side_effect=close)
        session.open = Mock(side_effect=open_session)

        with patch.object(browser_module.siel_log, "run_log"):
            session.restart("main_listing_recovery")

        self.assertIs(session.driver, second_driver)
        session.close.assert_called_once()
        session.open.assert_called_once()

    def test_warmup_uses_homepage_without_cookie_or_cache_commands(self) -> None:
        expected = {
            "url": "https://www.amazon.de/",
            "status": 200,
            "text": "homepage",
            "bytes": 1_500_000,
            "error": None,
        }
        session = browser_module.AmazonBrowserSession.__new__(browser_module.AmazonBrowserSession)
        session.sleep = 1.5
        session.fetch = Mock(return_value=expected)

        with patch.object(browser_module.siel_log, "run_log"):
            actual = session.warm_up()

        self.assertEqual(actual, expected)
        session.fetch.assert_called_once_with(
            "https://www.amazon.de/",
            scroll_ratio=0.0,
            scroll_max_scrolls=0,
            post_load_sleep=3.0,
        )


if __name__ == "__main__":
    unittest.main()
