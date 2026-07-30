from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ref import config as ref_config  # noqa: E402
from common import eprel  # noqa: E402
from tv import config as tv_config  # noqa: E402


class OttoRefSpecFallbackTests(unittest.TestCase):
    def test_top_infos_cooling_freezer_are_summed(self) -> None:
        target = {
            "product_id": "C2022152006",
            "retailer_sku_name": "LG Kuehl-/Gefrierkombination InstaView GBG5160CPY",
            "top_infos": '{"Kapazit\\u00e4t K\\u00fchlen": "239 l", "Kapazit\\u00e4t Frieren": "110 l"}',
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku="GBG5160CPY")
        self.assertEqual(spec["ref_capacity"], "349 l")

    def test_top_infos_explicit_total_wins(self) -> None:
        target = {
            "product_id": "C1121663679",
            "retailer_sku_name": "SIEMENS Kuehl-/Gefrierkombination iQ500 KG36EALCA",
            "top_infos": '{"Kapazit\\u00e4t K\\u00fchlen": "214 l", "Kapazit\\u00e4t Frieren": "94 l", "Gesamtrauminhalt": "308 l"}',
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku="KG36EALCA")
        self.assertEqual(spec["ref_capacity"], "308 l")

    def test_title_cooling_freezer_sum_does_not_store_cooling_alone(self) -> None:
        target = {
            "product_id": "example",
            "retailer_sku_name": "LG Kuehl-Gefrierkombination 239 l Kuehlen 110 l Frieren",
            "top_infos": "",
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_capacity"], "349 l")

    def test_refrigerator_cooling_only_is_total_capacity(self) -> None:
        target = {
            "product_id": "CS05AV06C",
            "retailer_sku_name": "PKM K\u00fchlschrank KS93EB",
            "top_infos": '{"Rauminhalte der K\u00fchlf\u00e4cher": "94 l"}',
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_refrigerator_type"], "Refrigerator")
        self.assertEqual(spec["ref_capacity"], "94 l")

    def test_cooling_only_is_not_total_for_multi_compartment_ref(self) -> None:
        target = {
            "product_id": "example",
            "retailer_sku_name": "PKM Side-by-Side SBS480NFWDBJ",
            "top_infos": '{"Rauminhalte der K\u00fchlf\u00e4cher": "276 l"}',
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_refrigerator_type"], "Side by Side")
        self.assertIsNone(spec["ref_capacity"])

    def test_freezer_label_is_not_reused_as_cooling_capacity(self) -> None:
        target = {
            "product_id": "example",
            "retailer_sku_name": "Royal Catering Getraenkekuehlschrank RC-BC004",
            "top_infos": {"Rauminhalte der Tiefkuehlfaecher": "458 l"},
        }
        with patch.object(ref_config.eprel, "fridge_total_volume", return_value=None):
            spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertIsNone(spec["ref_capacity"])

    def test_product_url_liter_capacity_wins_over_ambiguous_compartment_label(self) -> None:
        target = {
            "product_id": "S03EZ0KJ",
            "retailer_sku_name": "Royal Catering Getraenkekuehlschrank RC-BC004",
            "product_url": (
                "https://www.otto.de/p/royal-catering-getraenkekuehlschrank-rc-bc004-"
                "180-cm-hoch-90-5-cm-breit-458-l-kuehlschrank-fuer-getraenke-"
                "mit-glastuer-led-beleuchtung-schwarz-S03EZ0KJ/"
            ),
            "top_infos": {"Rauminhalte der Tiefkuehlfaecher": "458 l"},
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_capacity"], "458 l")

    def test_freezer_only_capacity_is_total_for_freezer(self) -> None:
        target = {
            "product_id": "freezer-example",
            "retailer_sku_name": "Hanseatic Gefrierschrank HGS17060CNFI",
            "top_infos": {"Rauminhalte der Tiefkuehlfaecher": "168 l"},
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_refrigerator_type"], "Freezer")
        self.assertEqual(spec["ref_capacity"], "168 l")

    def test_standalone_nutzinhalt_is_total_capacity(self) -> None:
        target = {
            "product_id": "S09230AI",
            "retailer_sku_name": "Stillstern Table Top K\u00fchlschrank KB-46-2",
            "top_infos": '{"Nutzinhalt": "45 L"}',
        }
        spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertEqual(spec["ref_capacity"], "45 l")

    def test_standalone_nutzinhalt_requires_numeric_liters(self) -> None:
        target = {
            "product_id": "example",
            "retailer_sku_name": "Stillstern Table Top K\u00fchlschrank KB-46-2",
            "top_infos": '{"Nutzinhalt": "nicht zutreffend"}',
        }
        with patch.object(ref_config.eprel, "fridge_total_volume", return_value=None):
            spec = ref_config.extract_spec(target, {}, {"model": {}}, sku=None)
        self.assertIsNone(spec["ref_capacity"])
    def test_chest_freezer_type_is_preserved(self) -> None:
        self.assertEqual(ref_config.translate_ref_type("Hanseatic Gefriertruhe"), "Chest Freezer")


class EprelMatchConfidenceTests(unittest.TestCase):
    def test_fuzzy_first_hit_is_not_trusted(self) -> None:
        hit = {"modelIdentifier": "600202425", "totalVolume": 88}
        self.assertIsNone(eprel._best_hit([hit], "202425"))

    def test_clear_variant_prefix_is_allowed(self) -> None:
        hit = {"modelIdentifier": "50UV1563DDW", "powerOnModeHDR": 91}
        self.assertIs(eprel._best_hit([hit], "50UV1563DD"), hit)


class OttoTvSpecFallbackTests(unittest.TestCase):
    def test_eprel_hdr_fallback_used_when_pdf_and_compare_missing(self) -> None:
        target = {"retailer_sku_name": "Coocaa 55 Zoll 55R5G LCD-LED Fernseher"}
        with patch.object(tv_config.eprel, "display_on_mode_power", return_value="110 W"):
            spec = tv_config.extract_spec(target, {}, {"model": {}}, sku="55R5G")
        self.assertEqual(spec["estimated_annual_electricity_use"], "110 W")
        self.assertEqual(spec["screen_size"], "55")

    def test_url_cm_fallback_used_when_no_zoll_sources(self) -> None:
        cases = [
            (
                "HISENSE 136MXQ Mini-LED-Fernseher",
                "https://www.otto.de/p/hisense-hisense-136mxq-mini-led-fernseher-345-cm-S00FF0SA/",
                "345 cm",
            ),
            (
                "Coocaa 40CRTG30Z DLED-Fernseher",
                "https://www.otto.de/p/coocaa-40crtg30z-dled-fernseher-100-cm-full-hd-smart-tv-hdr-S01GL0HJ/",
                "100 cm",
            ),
        ]
        with patch.object(tv_config.eprel, "display_on_mode_power", return_value=None):
            for name, url, expected in cases:
                with self.subTest(url=url):
                    target = {"retailer_sku_name": name, "product_url": url}
                    spec = tv_config.extract_spec(target, {}, {"model": {}}, sku=None)
                    self.assertEqual(spec["screen_size"], expected)

    def test_url_cm_fallback_ignores_variant_urls_with_zoll(self) -> None:
        target = {
            "retailer_sku_name": "Sony K-55S3 DLED-Fernseher",
            "product_url": "https://www.otto.de/p/sony-k-85s3-85-bravia-3-dled-fernseher-215-cm-85-zoll-C1970862261/",
        }
        with patch.object(tv_config.eprel, "display_on_mode_power", return_value=None):
            spec = tv_config.extract_spec(target, {}, {"model": {}}, sku="K-55S3")
        self.assertIsNone(spec["screen_size"])


if __name__ == "__main__":
    unittest.main()
