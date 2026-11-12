from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import notify


def report(tmp_path, listing, *, full_batch="current"):
    manifests = {
        "step01_listing_manifest.json": listing,
        "step02_final_targets_manifest.json": {"main_target_unique": 1, "bsr_rank_limit": 1},
        "step09_full_output_manifest.json": {"batch_id": full_batch},
        "step14_db_save_manifest.json": {"success": True, "dry_run": False, "inserted": 1},
    }
    for name, data in manifests.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    row = {field: "value" for field in notify.NULL_BASE + notify.NULL_TAIL}
    row.update(main_rank="1", bsr_rank="1", batch_id="current", star_rating="4")
    with patch.object(notify, "category_output_root", return_value=tmp_path):
        return notify.build_report(SimpleNamespace(PRODUCT="TV", SPEC_FIELDS=[]), [row])[1]


def test_partial_listing_discloses_failed_options(tmp_path):
    body = report(tmp_path, {"success": True, "crocotile_complete": False,
                             "crocotile_failed_ids": ["option-1", "option-2"]})
    assert "최신 목록 상품 정보 보강 실패 2건: option-1, option-2" in body
    assert "이상 없음" not in body


def test_latest_failure_remains_visible_if_output_is_from_an_earlier_batch(tmp_path):
    body = report(tmp_path, {"success": False, "crocotile_complete": False,
                             "crocotile_failed_ids": ["option-3"]}, full_batch="old")
    assert "최신 목록 상품 정보 보강 실패 1건: option-3" in body


def test_complete_listing_has_no_partial_warning(tmp_path):
    body = report(tmp_path, {"success": True, "crocotile_complete": True,
                             "crocotile_failed_ids": []})
    assert "상품 정보 보강 실패" not in body


def test_incomplete_listing_without_ids_does_not_report_zero_failures(tmp_path):
    body = report(tmp_path, {"crocotile_complete": False, "crocotile_failed_ids": None})
    assert "최신 목록 상품 정보 보강 실패 건수·옵션 ID 미확인" in body


def test_complete_requested_ids_still_report_unexpected_and_invalid_rows(tmp_path):
    body = report(tmp_path, {"success": True, "crocotile_complete": True,
                             "crocotile_failed_ids": [],
                             "crocotile_response_anomalies": [
                                 {"unexpected_ids": ["extra"], "invalid_rows": 1},
                                 {"unexpected_ids": ["extra"], "invalid_rows": 2},
                             ]})
    assert "요청하지 않은 옵션 1건: extra" in body
    assert "옵션 ID 없는 항목 3건" in body
    assert "이상 없음" not in body


def test_pdp_failures_are_reported_only_for_the_current_batch(tmp_path):
    report(tmp_path, {"success": True})
    full = {"batch_id": "current", "attempts": [
        {"variation_id": "v1", "pdp_supplement_failure_reason": "variation_id_mismatch"},
        {"variation_id": "v2", "pdp_supplement_failure_reason": "no_allowed_fields_recovered"},
    ]}
    path = tmp_path / "step09_full_output_manifest.json"
    path.write_text(json.dumps(full), encoding="utf-8")
    row = {field: "value" for field in notify.NULL_BASE + notify.NULL_TAIL}
    row.update(main_rank="1", bsr_rank="1", batch_id="current", star_rating="4")
    with patch.object(notify, "category_output_root", return_value=tmp_path):
        subject, body = notify.build_report(SimpleNamespace(PRODUCT="LDY", SPEC_FIELDS=[]), [row])
    assert "[확인필요]" in subject
    assert "상세 보강 실패 1건 (옵션: v1" in body
    assert "상세 보강 후 값 미확인 1건 (옵션: v2" in body
    full["batch_id"] = "old"
    path.write_text(json.dumps(full), encoding="utf-8")
    with patch.object(notify, "category_output_root", return_value=tmp_path):
        _, body = notify.build_report(SimpleNamespace(PRODUCT="LDY", SPEC_FIELDS=[]), [row])
    assert "상세 보강 실패" not in body
