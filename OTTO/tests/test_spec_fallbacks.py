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


if __name__ == "__main__":
    unittest.main()
