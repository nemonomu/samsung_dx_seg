from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common import detail as detail_module
from common.detail import _detail_quality, _ref_retry_reason
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


class FakeSession:
    def __init__(self, *, first_driver: Driver, retry_driver: Driver | None = None,
                 landing_url: str | None = None, retry_landing_url: str | None = None) -> None:
        self.driver = first_driver
        self.first_driver = first_driver
        self.retry_driver = retry_driver or first_driver
        self.landing_url = landing_url
        self.retry_landing_url = retry_landing_url
        self.fetch_count = 0
        self.refetch_count = 0

    def fetch(self, url: str, **_kwargs) -> dict[str, object]:
        self.fetch_count += 1
        self.driver = self.first_driver
        return {"url": self.landing_url or url, "status": 200, "text": "first", "error": None, "bytes": 5}

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


def _run_detail(session: FakeSession, *, product: str, parsed_by_driver: dict[int, dict[str, object]],
                product_url: str | None = "https://www.amazon.de/dp/B0TEST1234",
                landing_names_by_driver: dict[int, str | None] | None = None):
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
        return detail_module.run(session=session, cfg=cfg, sleep=0, review_page_fallback=False)


class RefPdpRetryTests(unittest.TestCase):
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
