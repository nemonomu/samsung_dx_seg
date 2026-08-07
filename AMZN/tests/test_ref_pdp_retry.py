from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import detail as detail_module
from common.detail import _browser_retry_reason, _detail_quality, _ref_retry_reason
from common.merge_insert import make_row


class Element:
    def __init__(self, text: str = "") -> None:
        self.text = text


class Driver:
    def __init__(self, *, title: str = "", containers: tuple[str, ...] = ()) -> None:
        self.title = title
        self.containers = set(containers)

    def find_elements(self, _by: str, selector: str) -> list[Element]:
        if selector == "#productTitle":
            return [Element(self.title)] if self.title else []
        return [Element()] if selector in self.containers else []


class FakeLogger:
    def info(self, *_args, **_kwargs) -> None:
        pass

    def warning(self, *_args, **_kwargs) -> None:
        pass

    def error(self, *_args, **_kwargs) -> None:
        pass


class FakeSession:
    def __init__(self, *, first_driver: Driver, retry_driver: Driver | None = None,
                 landing_url: str | None = None, retry_landing_url: str | None = None,
                 first_result: dict[str, object] | None = None,
                 normal_retry_result: dict[str, object] | None = None) -> None:
        self.driver = first_driver
        self.first_driver = first_driver
        self.retry_driver = retry_driver or first_driver
        self.landing_url = landing_url
        self.retry_landing_url = retry_landing_url
        self.first_result = first_result
        self.normal_retry_result = normal_retry_result
        self.fetch_count = 0
        self.refetch_count = 0
        self.normal_refetch_count = 0
        self.restart_count = 0

    def fetch(self, url: str, **_kwargs) -> dict[str, object]:
        self.fetch_count += 1
        self.driver = self.first_driver
        if self.first_result is not None:
            return dict(self.first_result)
        return {"url": self.landing_url or url, "status": 200, "text": "first", "error": None, "bytes": 5}

    def refetch(self, url: str, **_kwargs) -> dict[str, object]:
        self.normal_refetch_count += 1
        self.driver = self.retry_driver
        result = self.normal_retry_result or {
            "url": self.retry_landing_url or url,
            "status": 200,
            "text": "retry",
            "error": None,
            "bytes": 5,
        }
        return {**result, "retry_mode": "normal_cache", "retry_wait_seconds": 5.0}

    def refetch_without_cache(self, url: str, **_kwargs) -> dict[str, object]:
        self.refetch_count += 1
        self.driver = self.retry_driver
        return {
            "url": self.retry_landing_url or url,
            "status": 200,
            "text": "retry",
            "error": None,
            "bytes": 5,
        }

    def restart(self, _reason: str) -> None:
        self.restart_count += 1


class RecoverySession(FakeSession):
    def __init__(self, *, fetch_results: list[tuple[dict[str, object] | Exception, Driver]],
                 normal_retry: tuple[dict[str, object], Driver] | None = None,
                 cache_retry: tuple[dict[str, object], Driver] | None = None) -> None:
        super().__init__(first_driver=fetch_results[0][1])
        self.fetch_results = list(fetch_results)
        self.normal_retry = normal_retry
        self.cache_retry = cache_retry

    def fetch(self, _url: str, **_kwargs) -> dict[str, object]:
        self.fetch_count += 1
        result, driver = self.fetch_results.pop(0)
        self.driver = driver
        if isinstance(result, Exception):
            raise result
        return dict(result)

    def refetch(self, url: str, **_kwargs) -> dict[str, object]:
        if self.normal_retry is None:
            return super().refetch(url, **_kwargs)
        self.normal_refetch_count += 1
        result, driver = self.normal_retry
        self.driver = driver
        return {**result, "retry_mode": "normal_cache", "retry_wait_seconds": 5.0}

    def refetch_without_cache(self, url: str, **_kwargs) -> dict[str, object]:
        if self.cache_retry is None:
            return super().refetch_without_cache(url, **_kwargs)
        self.refetch_count += 1
        result, driver = self.cache_retry
        self.driver = driver
        return dict(result)


def _run_detail(session: FakeSession, *, product: str, parsed_by_driver: dict[int, dict[str, object]],
                product_url: str | None = "https://www.amazon.de/dp/B0TEST1234",
                landing_names_by_driver: dict[int, str | None] | None = None,
                emit=None):
    cfg = SimpleNamespace(PRODUCT=product, ACCOUNT_NAME="Amazon.de")
    target = {
        "asin": "B0TEST1234",
        "item": "B0TEST1234",
        "product_url": product_url,
        "retailer_sku_name": "Test Refrigerator",
    }
    with (
        patch.object(detail_module, "category_output_root", return_value=Path("test-output")),
        patch.object(detail_module, "category_reference_root", return_value=Path("test-references")),
        patch.object(detail_module, "read_csv", return_value=[target]),
        patch.object(detail_module, "write_csv"),
        patch.object(detail_module, "write_json"),
        patch.object(
            detail_module,
            "_extract_landing_name",
            side_effect=lambda driver, _selectors: (landing_names_by_driver or {}).get(id(driver)),
        ),
        patch.object(detail_module.selector_api, "load_selectors", return_value={}),
        patch.object(
            detail_module.selector_api,
            "extract_detail",
            side_effect=lambda driver, *_args, **_kwargs: dict(parsed_by_driver[id(driver)]),
        ),
        patch.object(detail_module.siel_log, "setup", return_value=(FakeLogger(), None)),
        patch.object(detail_module.siel_log, "log_selectors"),
        patch.object(detail_module.siel_log, "warn_price_logic"),
        patch.object(detail_module.siel_log, "log_record_summary"),
        patch.object(detail_module.siel_log, "log_detail_result"),
        patch.object(
            detail_module.siel_log,
            "DetailProgress",
            return_value=SimpleNamespace(update=lambda *_args, **_kwargs: None),
        ),
    ):
        return detail_module.run(
            session=session,
            cfg=cfg,
            sleep=0,
            review_page_fallback=False,
            emit=emit,
        )


class RefPdpRetryTests(unittest.TestCase):
    @staticmethod
    def _result(*, status: int | None, text: str, error: str | None) -> dict[str, object]:
        return {
            "url": "https://www.amazon.de/dp/B0TEST1234",
            "status": status,
            "text": text,
            "error": error,
            "bytes": len(text),
        }

    def test_initial_fetch_tab_crash_restarts_chrome_and_recovers_tv(self) -> None:
        dead_driver = Driver()
        recovered_driver = Driver(title="Test TV", containers=("#dp",))
        session = RecoverySession(fetch_results=[
            (self._result(status=None, text="", error="WebDriverException: Message: tab crashed"), dead_driver),
            (self._result(status=200, text="recovered", error=None), recovered_driver),
        ])

        manifest = _run_detail(
            session,
            product="TV",
            parsed_by_driver={id(recovered_driver): {"sku": "TV-RECOVERED"}},
        )

        self.assertTrue(manifest["success"])
        self.assertEqual(session.restart_count, 1)
        self.assertEqual(session.fetch_count, 2)
        self.assertEqual(manifest["rows_data"][0]["sku"], "TV-RECOVERED")
        restart = manifest["attempts"][0]["browser_restart_attempts"][0]
        self.assertEqual(restart["phase"], "initial_fetch")
        self.assertTrue(restart["success"])

    def test_thrown_dead_session_error_also_restarts_and_recovers(self) -> None:
        dead_driver = Driver()
        recovered_driver = Driver(title="Test TV", containers=("#dp",))
        session = RecoverySession(fetch_results=[
            (RuntimeError("invalid session id: browser connection refused"), dead_driver),
            (self._result(status=200, text="recovered", error=None), recovered_driver),
        ])

        manifest = _run_detail(
            session,
            product="TV",
            parsed_by_driver={id(recovered_driver): {"sku": "TV-RECOVERED"}},
        )

        self.assertTrue(manifest["success"])
        self.assertEqual(session.restart_count, 1)
        self.assertEqual(manifest["rows_data"][0]["sku"], "TV-RECOVERED")

    def test_normal_refetch_tab_crash_restarts_chrome_and_recovers_tv(self) -> None:
        first_driver = Driver()
        dead_driver = Driver()
        recovered_driver = Driver(title="Test TV", containers=("#dp",))
        timeout = self._result(status=None, text="", error="TimeoutException: renderer timeout")
        crash = self._result(status=None, text="", error="invalid session id: browser disconnected")
        session = RecoverySession(
            fetch_results=[
                (timeout, first_driver),
                (self._result(status=200, text="recovered", error=None), recovered_driver),
            ],
            normal_retry=(crash, dead_driver),
        )

        manifest = _run_detail(
            session,
            product="TV",
            parsed_by_driver={id(recovered_driver): {"sku": "TV-RECOVERED"}},
        )

        self.assertEqual(session.restart_count, 1)
        self.assertEqual(session.normal_refetch_count, 1)
        self.assertEqual(manifest["attempts"][0]["selected_attempt"], "retry")
        self.assertEqual(
            manifest["attempts"][0]["browser_restart_attempts"][0]["phase"],
            "normal_refetch",
        )

    def test_ref_cache_bypass_tab_crash_restarts_chrome_and_recovers(self) -> None:
        first_driver = Driver()
        dead_driver = Driver()
        recovered_driver = Driver(title="Test Refrigerator", containers=("#dp",))
        success = self._result(status=200, text="pdp", error=None)
        crash = self._result(status=None, text="", error="WebDriverException: Message: tab crashed")
        session = RecoverySession(
            fetch_results=[(success, first_driver), (success, recovered_driver)],
            cache_retry=(crash, dead_driver),
        )
        recovered = {
            "sku": "RF-RECOVERED",
            "ref_capacity": "199 L",
            "ref_refrigerator_type": "Refrigerator",
        }

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(recovered_driver): recovered},
        )

        self.assertEqual(session.restart_count, 1)
        self.assertEqual(session.refetch_count, 1)
        self.assertEqual(manifest["rows_data"][0]["sku"], "RF-RECOVERED")
        self.assertEqual(
            manifest["attempts"][0]["browser_restart_attempts"][0]["phase"],
            "cache_bypass_refetch",
        )

    def test_two_failed_new_chrome_sessions_stop_detail_and_emit_fatal_record(self) -> None:
        dead_driver = Driver()
        crash = self._result(status=None, text="", error="WebDriverException: Message: tab crashed")
        session = RecoverySession(fetch_results=[
            (crash, dead_driver),
            (crash, dead_driver),
            (crash, dead_driver),
        ])
        emitted: list[dict[str, object]] = []

        with self.assertRaises(detail_module.DetailBrowserUnavailableError):
            _run_detail(session, product="REF", parsed_by_driver={}, emit=emitted.append)

        self.assertEqual(session.restart_count, 2)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["stage"], "detail_error")
        self.assertTrue(emitted[0]["_fatal"])
        self.assertEqual(len(emitted[0]["browser_restart_attempts"]), 2)

    def test_browser_retry_reason_excludes_amazon_interstitial(self) -> None:
        self.assertIsNone(_browser_retry_reason({
            "status": 429, "text": "blocked", "error": "amazon_interstitial",
        }))

    def test_browser_retry_reason_classifies_timeout_and_empty_html(self) -> None:
        self.assertEqual(_browser_retry_reason({
            "status": None, "text": "", "error": "TimeoutException: renderer timeout",
        }), "timeout")
        self.assertEqual(_browser_retry_reason({
            "status": 200, "text": "", "error": None,
        }), "empty_html")

    def test_tv_timeout_retries_once_with_cache_and_selects_retry(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Test TV", containers=("#dp",))
        session = FakeSession(
            first_driver=first_driver,
            retry_driver=retry_driver,
            first_result={
                "url": "https://www.amazon.de/dp/B0TEST1234",
                "status": None,
                "text": "",
                "error": "TimeoutException: renderer timeout",
                "bytes": 0,
            },
        )

        manifest = _run_detail(
            session,
            product="TV",
            parsed_by_driver={id(retry_driver): {"sku": "TV-100", "final_sku_price": "999,00€"}},
        )

        attempt = manifest["attempts"][0]
        self.assertEqual(session.fetch_count, 1)
        self.assertEqual(session.normal_refetch_count, 1)
        self.assertEqual(session.refetch_count, 0)
        self.assertEqual(manifest["rows_data"][0]["final_sku_price"], "999,00€")
        self.assertNotIn("_transport_warning", manifest["rows_data"][0])
        self.assertEqual(attempt["retry_reason"], "timeout")
        self.assertEqual(attempt["retry_mode"], "normal_cache")
        self.assertEqual(attempt["selected_attempt"], "retry")
        self.assertIsNone(attempt["retry_final_reason"])

    def test_tv_timeout_retry_failure_is_recorded_and_collection_continues(self) -> None:
        driver = Driver()
        failed = {
            "url": "https://www.amazon.de/dp/B0TEST1234",
            "status": None,
            "text": "",
            "error": "TimeoutException: renderer timeout",
            "bytes": 0,
        }
        session = FakeSession(
            first_driver=driver,
            first_result=failed,
            normal_retry_result=failed,
        )

        manifest = _run_detail(session, product="TV", parsed_by_driver={})

        attempt = manifest["attempts"][0]
        self.assertTrue(manifest["success"])
        self.assertEqual(manifest["rows"], 1)
        self.assertEqual(session.normal_refetch_count, 1)
        self.assertEqual(attempt["selected_attempt"], "first")
        self.assertEqual(attempt["retry_final_reason"], "timeout")
        self.assertEqual(manifest["rows_data"][0]["_transport_warning"], "timeout")

    def test_latest_failed_retry_distinguishes_429_from_timeout(self) -> None:
        cases = (
            (
                self._result(status=None, text="", error="TimeoutException: renderer timeout"),
                self._result(status=429, text="blocked", error="amazon_interstitial"),
                "amazon_429",
            ),
            (
                self._result(status=503, text="technical error", error="amazon_technical_error"),
                self._result(status=None, text="", error="TimeoutException: renderer timeout"),
                "timeout",
            ),
        )
        for first_result, retry_result, expected in cases:
            with self.subTest(expected=expected):
                driver = Driver()
                session = FakeSession(
                    first_driver=driver,
                    first_result=first_result,
                    normal_retry_result=retry_result,
                )

                manifest = _run_detail(
                    session,
                    product="TV",
                    parsed_by_driver={id(driver): {}},
                )

                self.assertTrue(manifest["success"])
                self.assertEqual(manifest["rows_data"][0]["_transport_warning"], expected)

    def test_ref_incomplete_pdp_with_timeout_retry_is_warned_and_continues(self) -> None:
        first_driver = Driver()
        retry_driver = Driver()
        session = RecoverySession(
            fetch_results=[(
                self._result(status=200, text="incomplete pdp", error=None),
                first_driver,
            )],
            cache_retry=(
                self._result(status=None, text="", error="TimeoutException: renderer timeout"),
                retry_driver,
            ),
        )

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): {}},
        )

        self.assertTrue(manifest["success"])
        self.assertEqual(manifest["rows_data"][0]["_transport_warning"], "timeout")

    def test_ref_timeout_uses_only_the_common_retry(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Test Refrigerator", containers=("#dp",))
        session = FakeSession(
            first_driver=first_driver,
            retry_driver=retry_driver,
            first_result={
                "url": "https://www.amazon.de/dp/B0TEST1234",
                "status": None,
                "text": "",
                "error": "TimeoutException: renderer timeout",
                "bytes": 0,
            },
        )

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={
                id(retry_driver): {
                    "sku": "RF-100",
                    "ref_capacity": "199 L",
                    "ref_refrigerator_type": "Refrigerator",
                },
            },
        )

        self.assertEqual(session.normal_refetch_count, 1)
        self.assertEqual(session.refetch_count, 0)
        self.assertEqual(manifest["attempts"][0]["selected_attempt"], "retry")

    def test_interstitial_does_not_retry_immediately(self) -> None:
        driver = Driver()
        session = FakeSession(
            first_driver=driver,
            first_result={
                "url": "https://www.amazon.de/dp/B0TEST1234",
                "status": 429,
                "text": "blocked",
                "error": "amazon_interstitial",
                "bytes": 7,
            },
        )

        manifest = _run_detail(session, product="TV", parsed_by_driver={id(driver): {}})

        self.assertTrue(manifest["success"])
        self.assertEqual(session.normal_refetch_count, 0)
        self.assertEqual(session.restart_count, 0)
        self.assertFalse(manifest["attempts"][0]["retry_attempted"])
        self.assertEqual(manifest["rows_data"][0]["_transport_warning"], "amazon_429")

    def test_ref_interstitial_does_not_use_product_specific_retry(self) -> None:
        driver = Driver()
        session = FakeSession(
            first_driver=driver,
            first_result={
                "url": "https://www.amazon.de/dp/B0TEST1234",
                "status": 429,
                "text": "blocked",
                "error": "amazon_interstitial",
                "bytes": 7,
            },
        )

        manifest = _run_detail(session, product="REF", parsed_by_driver={id(driver): {}})

        self.assertEqual(session.normal_refetch_count, 0)
        self.assertEqual(session.refetch_count, 0)
        self.assertEqual(session.restart_count, 0)
        self.assertFalse(manifest["attempts"][0]["retry_attempted"])

    def test_normal_ref_pdp_does_not_retry(self) -> None:
        driver = Driver(title="Bosch Refrigerator", containers=("#dp",))
        detail = {"sku": "KIR41", "ref_capacity": "199 L", "ref_refrigerator_type": "Built-in Refrigerator"}

        self.assertIsNone(_ref_retry_reason(driver, detail))

    def test_missing_title_requests_one_retry(self) -> None:
        driver = Driver(containers=("#dp",))
        detail = {"sku": "KIR41"}

        self.assertEqual(_ref_retry_reason(driver, detail), "missing_title")

    def test_missing_container_requests_one_retry(self) -> None:
        driver = Driver(title="Bosch Refrigerator")
        detail = {"sku": "KIR41"}

        self.assertEqual(_ref_retry_reason(driver, detail), "missing_container")

    def test_all_core_fields_empty_requests_one_retry(self) -> None:
        driver = Driver(title="Bosch Refrigerator", containers=("#dp",))

        self.assertEqual(_ref_retry_reason(driver, {}), "core_fields_empty")

    def test_one_core_field_present_does_not_retry_for_fields_only(self) -> None:
        driver = Driver(title="Bosch Refrigerator", containers=("#dp",))

        self.assertIsNone(_ref_retry_reason(driver, {"sku": "KIR41"}))

    def test_quality_prefers_normal_pdp_then_more_core_fields(self) -> None:
        normal = Driver(title="Bosch Refrigerator", containers=("#dp",))
        incomplete = Driver()

        self.assertGreater(_detail_quality(normal, {"sku": "KIR41"}), _detail_quality(incomplete, {
            "sku": "KIR41", "ref_capacity": "199 L", "ref_refrigerator_type": "Built-in Refrigerator",
        }))

    def test_run_refetches_once_and_selects_recovered_ref_detail(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Test Refrigerator", containers=("#dp",))
        session = FakeSession(first_driver=first_driver, retry_driver=retry_driver)
        recovered = {
            "sku": "RF-100",
            "ref_capacity": "199 L",
            "ref_refrigerator_type": "Built-in Refrigerator",
        }

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): recovered},
        )

        self.assertEqual(session.fetch_count, 1)
        self.assertEqual(session.refetch_count, 1)
        self.assertEqual(manifest["rows_data"][0]["sku"], "RF-100")
        self.assertEqual(manifest["attempts"][0]["selected_attempt"], "retry")
        self.assertEqual(manifest["attempts"][0]["final_core_field_count"], 3)

    def test_run_does_not_apply_ref_retry_to_tv(self) -> None:
        driver = Driver()
        session = FakeSession(first_driver=driver)

        manifest = _run_detail(session, product="TV", parsed_by_driver={id(driver): {}})

        self.assertEqual(session.fetch_count, 1)
        self.assertEqual(session.refetch_count, 0)
        self.assertFalse(manifest["attempts"][0]["retry_attempted"])

    def test_redirect_listing_only_remains_true_and_does_not_retry(self) -> None:
        driver = Driver()
        session = FakeSession(
            first_driver=driver,
            landing_url="https://www.amazon.de/dp/B0OTHER999",
        )

        manifest = _run_detail(session, product="REF", parsed_by_driver={id(driver): {}})

        row = manifest["rows_data"][0]
        self.assertTrue(row["redirect"])
        self.assertEqual(row["redirect_decision"], "name_mismatch_listing_only")
        self.assertEqual(session.refetch_count, 0)

    def test_retry_redirect_to_another_product_is_rejected(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Other Product", containers=("#dp",))
        session = FakeSession(
            first_driver=first_driver,
            retry_driver=retry_driver,
            retry_landing_url="https://www.amazon.de/dp/B0OTHER999",
        )

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): {"sku": "OTHER-SKU"}},
        )

        row = manifest["rows_data"][0]
        attempt = manifest["attempts"][0]
        self.assertTrue(row["redirect"])
        self.assertNotIn("sku", row)
        self.assertEqual(row["item"], "B0TEST1234")
        self.assertEqual(row["loaded_url"], "https://www.amazon.de/dp/B0OTHER999")
        self.assertEqual(row["redirect_decision"], "name_mismatch_listing_only")
        self.assertEqual(attempt["loaded_asin"], "B0OTHER999")
        self.assertEqual(attempt["retry_final_reason"], "redirect_name_mismatch")

    def test_retry_redirect_with_same_name_is_selected_and_marked(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Test Refrigerator", containers=("#dp",))
        session = FakeSession(
            first_driver=first_driver,
            retry_driver=retry_driver,
            retry_landing_url="https://www.amazon.de/dp/B0SAME9999",
        )
        recovered = {
            "sku": "RF-REDIRECT",
            "ref_capacity": "199 L",
            "ref_refrigerator_type": "Built-in Refrigerator",
        }

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): recovered},
            landing_names_by_driver={id(retry_driver): "Test Refrigerator"},
        )

        row = manifest["rows_data"][0]
        attempt = manifest["attempts"][0]
        self.assertTrue(row["redirect"])
        self.assertTrue(row["_redirect_use_landing"])
        self.assertEqual(row["item"], "B0SAME9999")
        self.assertEqual(row["sku"], "RF-REDIRECT")
        self.assertEqual(row["redirect_decision"], "same_name_collect_landing")
        self.assertEqual(attempt["selected_attempt"], "retry")
        self.assertEqual(attempt["loaded_asin"], "B0SAME9999")
        merged = make_row(
            SimpleNamespace(PRODUCT="REF"),
            {
                "stage": "main",
                "asin": "B0TEST1234",
                "item": "B0TEST1234",
                "product_url": "https://www.amazon.de/dp/B0TEST1234",
                "retailer_sku_name": "Test Refrigerator",
            },
            None,
            row,
        )
        self.assertIsNotNone(merged)
        self.assertTrue(merged["redirect"])
        self.assertEqual(merged["item"], "B0SAME9999")
        self.assertEqual(merged["sku"], "RF-REDIRECT")

    def test_retry_manifest_records_the_actual_remaining_reason(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Test Refrigerator", containers=("#dp",))
        session = FakeSession(first_driver=first_driver, retry_driver=retry_driver)

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): {}},
        )

        attempt = manifest["attempts"][0]
        self.assertEqual(attempt["retry_reason"], "missing_title,missing_container,core_fields_empty")
        self.assertEqual(attempt["retry_final_reason"], "core_fields_empty")

    def test_missing_product_url_does_not_attempt_refetch(self) -> None:
        driver = Driver()
        session = FakeSession(first_driver=driver)

        manifest = _run_detail(session, product="REF", parsed_by_driver={}, product_url=None)

        self.assertEqual(session.fetch_count, 0)
        self.assertEqual(session.refetch_count, 0)
        self.assertFalse(manifest["attempts"][0]["retry_attempted"])

    def test_redirect_true_survives_the_db_merge_row(self) -> None:
        cfg = SimpleNamespace(PRODUCT="REF")
        listing = {
            "stage": "main",
            "asin": "B0TEST1234",
            "item": "B0TEST1234",
            "product_url": "https://www.amazon.de/dp/B0TEST1234",
            "retailer_sku_name": "Test Refrigerator",
        }
        detail = {
            "stage": "detail",
            "asin": "B0TEST1234",
            "item": "B0TEST1234",
            "redirect": True,
            "_detail_skip": "url_mismatch_name_mismatch",
        }

        row = make_row(cfg, listing, None, detail)

        self.assertIsNotNone(row)
        self.assertTrue(row["redirect"])
        self.assertEqual(row["item"], "B0TEST1234")

    def test_two_different_redirects_leave_one_consistent_listing_only_row(self) -> None:
        first_driver = Driver()
        retry_driver = Driver(title="Other Product", containers=("#dp",))
        session = FakeSession(
            first_driver=first_driver,
            retry_driver=retry_driver,
            landing_url="https://www.amazon.de/dp/B0FIRST999",
            retry_landing_url="https://www.amazon.de/dp/B0OTHER999",
        )

        manifest = _run_detail(
            session,
            product="REF",
            parsed_by_driver={id(first_driver): {}, id(retry_driver): {"sku": "OTHER-SKU"}},
            landing_names_by_driver={id(first_driver): "Test Refrigerator", id(retry_driver): "Other Product"},
        )

        row = manifest["rows_data"][0]
        attempt = manifest["attempts"][0]
        self.assertEqual(row["item"], "B0TEST1234")
        self.assertEqual(row["loaded_url"], "https://www.amazon.de/dp/B0OTHER999")
        self.assertEqual(row["landing_asin"], "B0OTHER999")
        self.assertNotIn("_redirect_use_landing", row)
        self.assertNotIn("sku", row)
        self.assertEqual(attempt["loaded_asin"], "B0OTHER999")
        self.assertEqual(attempt["detail_skip"], "url_mismatch_name_mismatch")


if __name__ == "__main__":
    unittest.main()
