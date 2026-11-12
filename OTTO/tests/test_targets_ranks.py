from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

OTTO = Path(__file__).resolve().parents[1]
if str(OTTO) not in sys.path:
    sys.path.insert(0, str(OTTO))

from common import targets
from common.io_util import read_csv, write_csv, write_json


def offer(rank, product_id, variation_id, exposure=None, name="TV"):
    row = {
        "source": "topseller_tilelist",
        "display_rank": rank,
        "product_id": product_id,
        "variation_id": variation_id,
        "retailer_sku_name": name,
        "sku_status": "Sponsored" if exposure == "sponsored" else "",
    }
    if exposure is not None:
        row["exposure_type"] = exposure
    return row


def run_targets(tmp_path, offers, *, main_limit, bsr_limit):
    cfg = SimpleNamespace(PRODUCT="TV", classify=lambda name: (name == "TV", "test"))
    write_csv(tmp_path / "otto_listing_topseller_rows.csv", offers)
    with (
        patch.object(targets, "category_output_root", return_value=tmp_path),
        patch.object(targets, "MAIN_TARGET_UNIQUE", main_limit),
        patch.object(targets, "BSR_TARGET_RANK", bsr_limit),
    ):
        manifest = targets.run(cfg)
    return read_csv(tmp_path / "otto_final_targets.csv"), manifest


def test_main_keeps_first_exposure_and_bsr_ranks_each_organic_option(tmp_path):
    rows, manifest = run_targets(
        tmp_path,
        [
            offer(6, "B", "b2", "organic"),
            offer(4, "A", "a1", "organic"),
            offer(1, "A", "a1", "sponsored"),
            offer(5, "X", "x1", "organic", name="TV wall mount"),
            offer(3, "C", "c1", "sponsored"),
            offer(2, "B", "b1", "organic"),
            offer(7, "B", "b1", "organic"),
        ],
        main_limit=4,
        bsr_limit=3,
    )

    assert [
        (r["product_id"], r["variation_id"], r["main_rank"], r["bsr_rank"], r["sku_status"])
        for r in rows
    ] == [
        ("A", "a1", "1", "2", "Sponsored"),
        ("B", "b1", "2", "1", ""),
        ("C", "c1", "3", "", "Sponsored"),
        ("B", "b2", "4", "3", ""),
    ]
    assert all(r["page_type"] == "main" for r in rows)
    assert manifest["final_target_rows"] == 4
    assert manifest["bsr_tagged_rows"] == 3


def test_bsr_rank_is_set_before_joining_with_main_limit(tmp_path):
    rows, manifest = run_targets(
        tmp_path,
        [
            offer(1, "A", "a1", "sponsored"),
            offer(2, "B", "b1", "organic"),
            offer(3, "A", "a1", "organic"),
        ],
        main_limit=1,
        bsr_limit=1,
    )

    assert [(r["product_id"], r["bsr_rank"]) for r in rows] == [("A", "")]
    assert manifest["bsr_tagged_rows"] == 0


def test_legacy_listing_can_build_main_without_inventing_bsr(tmp_path):
    offers = [dict(offer(1, "A", "a1", "organic"), source="topseller_api"), offer(2, "B", "b1")]
    rows, manifest = run_targets(tmp_path, offers, main_limit=2, bsr_limit=2)

    assert [(r["product_id"], r["main_rank"], r["bsr_rank"]) for r in rows] == [("A", "1", ""), ("B", "2", "")]
    assert manifest["bsr_tagged_rows"] == 0


def test_failed_or_mismatched_listing_warns_and_keeps_existing_behavior(tmp_path):
    manifest_path = tmp_path / "step01_listing_manifest.json"
    offers = [dict(offer(1, "A", "a1", "organic"), discount_sticker_run_id="old")]
    for manifest, reason in (
        ({"success": False}, "did not complete"),
        ({"success": True, "run_id": "new"}, "does not match"),
    ):
        write_json(manifest_path, manifest)
        rows, result = run_targets(tmp_path, offers, main_limit=1, bsr_limit=1)
        assert len(rows) == 1
        assert any(reason in warning for warning in result["warnings"])


def test_missing_option_id_keeps_listing_identity_and_later_options(tmp_path):
    rows, manifest = run_targets(
        tmp_path,
        [offer(1, "A", "", "organic"), offer(2, "A", "a1", "organic")],
        main_limit=2,
        bsr_limit=2,
    )

    assert [(r["variation_id"], r["main_rank"], r["bsr_rank"]) for r in rows] == [("", "1", "1"), ("a1", "2", "2")]
    assert manifest["excluded_rows"] == 0
