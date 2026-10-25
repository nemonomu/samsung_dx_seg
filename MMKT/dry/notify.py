"""DRY completion report using the existing SEG mail settings and SMTP sender.

Only this module owns DRY notifications; the integrated runner never sends mail.
No raw SMTP errors, addresses or authentication settings enter logs/manifests.
"""
from __future__ import annotations

import math
from pathlib import Path

from common.notify import _send, _truthy, env_value
from dry.events import context, emit, quiet_external
from dry.output import write_manifest as write_json


def send_error_code(error) -> str:
    """Whitelist SMTP failure categories; never return any original error text."""
    if error == "missing SMTP settings":
        return "SMTP_CONFIGURATION_MISSING"
    if isinstance(error, str):
        for name, code in (("SMTPAuthenticationError", "SMTP_AUTHENTICATION_FAILED"),
                           ("SMTPRecipientsRefused", "SMTP_RECIPIENTS_REFUSED"),
                           ("SMTPServerDisconnected", "SMTP_DISCONNECTED"),
                           ("SMTPConnectError", "SMTP_CONNECTION_FAILED"),
                           ("TimeoutError", "SMTP_TIMEOUT")):
            if error.startswith(name + ":"):
                return code
    return "SMTP_SEND_FAILED"


_REASONS_KO = {"last_page": "마지막 목록 확인", "target_reached": "목표 수량 도달",
               "first_page_inconsistent": "첫 상품 묶음 불일치 (재조회 소진)",
               "washer_dryer": "세탁건조 일체형", "washing_machine": "세탁기",
               "set_or_combo": "세트·콤보", "advertisement": "별도 광고 배너",
               "portabel_title": "제목 Portabel", "accessory": "부속품",
               "other_appliance": "다른 종류의 제품", "duplicate": "중복"}
_ERRORS_KO = {"UNCONFIRMED_PRODUCT_TYPE": "상품 종류를 확인하지 못함",
              "DETAIL_API_COMMON_FAILURE": "여러 상품에 공통인 API 장애로 중단",
              "PARTIAL_PRODUCT_FAILURES": "일부 상품 처리 실패",
              "DETAIL_API_FAILED": "상세 API 호출 실패", "DETAIL_AVAILABILITY_INVALID": "구매 상태 응답 형식 오류",
              "REVIEW_DISTRIBUTION_INVALID": "별점 통계 응답 형식 오류",
              "DB_BACKFILL_FAILED": "기존 DB 값 조회 실패", "DB_CONNECT_FAILED": "DB 연결 실패",
              "DB_INSERT_FAILED": "DB 적재 실패", "DB_COMMIT_FAILED": "DB 반영 여부 확인 필요",
              "DB_COMMITTED_MANIFEST_SAVE_FAILED": "DB 반영 후 결과 기록 실패",
              "INTERMEDIATE_SAVE_FAILED": "중간 결과 저장 실패",
              "FINAL_CSV_SAVE_FAILED": "최종 CSV 저장 실패",
              "FINAL_MANIFEST_SAVE_FAILED": "수집 결과 기록 실패"}

_TAIL_REASONS_KO = {
    "fresh_total_and_verified_tail_boundary": "최신 총수와 마지막 목록 경계 일치",
    "new_following_products": "다음 묶음의 새 상품을 정규 수집으로 전달",
    "confirmation_budget_exhausted": "추가 조회 한도 소진",
    "current_total_missing_or_conflicting": "최신 총수 누락 또는 화면 내 수치 불일치",
    "current_total_below_read_slots": "최신 총수가 확인한 목록 위치보다 작음",
    "current_total_or_more_control_mismatch": "최신 총수 또는 더보기 상태 불일치",
    "current_document_totals_conflict": "재확인한 화면끼리 총수 불일치",
    "reference_unstable_during_confirmation": "재확인 전후 총수 또는 상품 변경",
    "preceding_products_changed": "직전 묶음의 상품 또는 더보기 상태 변경",
    "tail_changed_or_incomplete": "마지막 후보 상품 변경 또는 카드 누락",
    "following_not_a_contiguous_expansion": "다음 목록의 연속성 미확인",
    "following_cards_incomplete": "다음 조회 화면의 상품 카드 누락",
    "preceding_fetch_or_parse_failed": "직전 묶음 접속 또는 파싱 실패",
    "tail_fetch_or_parse_failed": "마지막 후보 접속 또는 파싱 실패",
    "following_fetch_or_parse_failed": "다음 조회 접속 또는 파싱 실패",
    "non_contiguous_history": "이전 묶음 확인 이력 누락",
    "incoherent_or_repeated_history": "이전 목록 불일치 또는 반복",
    "non_uniform_prefix": "이전 묶음의 목록 위치 불일치",
    "incomplete_tail_candidate": "마지막 후보의 카드 또는 더보기 근거 부족",
    "probe_checkpoint_failed": "재확인 중간 결과 저장 실패",
}


def _duration_ko(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "확인 불가"
    if not math.isfinite(seconds) or seconds < 0:
        return "확인 불가"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}분 {remainder:.1f}초"


def _korean_summary(manifest, table, *, error, elapsed_s):
    failures = manifest.get("product_failure_count", 0)
    status = manifest.get("collection_status")
    collection = ("실패" if status == "failed" else "일부 상품 실패" if failures or status == "partial" else
                  "완료" if status == "complete" or manifest.get("success") is True else "미완료")
    lines = ["한글 요약", f"수집 상태: {collection}", f"배치: {manifest.get('batch_id') or '미생성'}",
             f"대상 테이블: {'.'.join(table)}", f"병합 후 고유 상품: {manifest.get('output_rows', 0)}개"]
    states = {"complete": "완료", "not_started": "미실행", "running": "진행 중", "failed": "실패"}
    for sort in ("main", "bsr"):
        listing = manifest.get(sort) or {}
        state = listing.get("state") or ("complete" if listing.get("stop_reason") in {"last_page", "target_reached"} else "not_started")
        count = "미실행" if state == "not_started" else str(listing.get("rows", 0)) + "개"
        target = (manifest.get("targets") or {}).get(sort, "확인 불가")
        reason = _REASONS_KO.get(listing.get("stop_reason"), "종료 근거 미확정")
        lines.append(f"{sort.upper()}: {count} / 목표 {target}개 — {states.get(state, '확인 필요')} ({reason})")
        if "listing_state" in listing:
            phase_names = {**states, "collecting": "진행 중"}
            lines.append(f"  목록 확보: {phase_names.get(listing['listing_state'], '확인 필요')}; "
                         f"후보 {listing.get('candidate_count', 0)}개; "
                         f"필터 전 목록 위치 {listing.get('category_slots_read', 0)}; "
                         f"상세: {phase_names.get(listing.get('detail_state'), '미실행')}; "
                         f"처리 {listing.get('detail_processed', 0)}개")
            times = listing.get("timing") or {}
            lines.append(f"  목록 소요시간: {_duration_ko(times.get('listing_elapsed_s'))}; "
                         f"상세 소요시간: {_duration_ko(times.get('detail_elapsed_s'))}")
        if listing.get("last_requested_page") is not None:
            lines.append(f"  마지막 정상 묶음: {listing.get('last_successful_page')}; 마지막 요청 묶음: {listing['last_requested_page']}")
        boundary = listing.get("tail_confirmation")
        if boundary:
            states_ko = {"confirmed": "마지막 확인됨", "more_products": "다음 상품 발견",
                         "failed": "확인 실패", "checking": "확인 중 중단"}
            lines.append(f"  끝부분 재확인: {states_ko.get(boundary.get('state'), '확인 필요')}; "
                         f"추가 조회 {boundary.get('queries', 0)}/{boundary.get('query_limit', 8)}회; "
                         f"최신 총수 {boundary.get('latest_total')}; "
                         f"근거 {_TAIL_REASONS_KO.get(boundary.get('reason'), '확인 필요')} ({boundary.get('reason')})")
        excluded = listing.get("excluded") or {}
        reasons = [f"{_REASONS_KO.get(key, key)} {count}건" for key, count in excluded.items() if key != "duplicate"]
        if reasons:
            lines.append("  제외: " + ", ".join(reasons))
        lines.append(f"  중복: {listing.get('duplicates', excluded.get('duplicate', 0))}건")
    lines.append(f"개별 상품 실패: {failures}건")
    for product in manifest.get("product_failures", []):
        code = product.get("error")
        lines.append(f"실패 상품: {product.get('item')} / {str(product.get('sort', '')).upper()}"
                     f" / 묶음 {product.get('page')} / {_ERRORS_KO.get(code, '오류 상세 확인 필요')} ({code})")
    db = manifest.get("database") or {}
    db_state, written = db.get("state"), db.get("db_written")
    if db.get("table") and db["table"] != ".".join(table):
        db_result = "확인 필요 (대상 테이블 불일치)"
    elif written is True:
        db_result = "반영 확인됨" + (" (후속 기록 오류 확인 필요)" if db_state != "success" else "")
    elif db_state == "skipped_empty":
        db_result = "미실행 (확인된 적격 상품 0건)"
    elif not db or db_state == "not_started":
        db_result = "미실행"
    elif written is None:
        db_result = "반영 여부 불확실 — 확인 필요"
    else:
        db_result = "실패" if db_state == "failed" else "미적재"
    lines.extend([f"DB 적재: {db_result}", f"신규 적재: {db.get('inserted', 0)}개",
                  f"동일 배치 기존 적재: {'예' if db.get('already_loaded') else '아니오'}"])
    if db.get("backfilled"):
        lines.append("DB 결측 보완: " + ", ".join(f"{field} {count}건" for field, count in db["backfilled"].items()))
    timing = manifest.get("timing") or {}
    lines.extend([f"수집 소요시간: {_duration_ko(timing.get('collection_elapsed_s'))}",
                  f"이번 실행 소요시간(적재 단계까지): {_duration_ko(elapsed_s)}",
                  f"수집 완료 시각: {timing.get('collection_completed_at') or '미완료'}"])
    if error:
        explanation = "목록 수집 실패" if str(error).startswith("LISTING_FAILED:") else _ERRORS_KO.get(error, "오류 상세 확인 필요")
        lines.append(f"확인 사유: {explanation} ({error})")
    return lines


def build_report(manifest, table, *, success, error=None, elapsed_s=None):
    english_subject = "[SEG] MediaMarkt DRY crawled" if success else "[CHECK] [SEG] MediaMarkt DRY failed"
    subject = "[SEG] MediaMarkt DRY 수집·적재 완료 / crawled" if success else "[CHECK] [SEG] MediaMarkt DRY 확인 필요 / failed"
    if not success and manifest.get("product_failure_count", 0):
        english_subject = "[CHECK] [SEG] MediaMarkt DRY partial collection"
        subject = "[CHECK] [SEG] MediaMarkt DRY 일부 상품 실패 / partial collection"
    elif not success and (manifest.get("database") or {}).get("state") == "skipped_empty":
        english_subject = "[CHECK] [SEG] MediaMarkt DRY no eligible products"
        subject = "[CHECK] [SEG] MediaMarkt DRY 적격 상품 0건 / no eligible products"
    lines = [subject, "", *_korean_summary(manifest, table, error=error, elapsed_s=elapsed_s),
             "", "English details", english_subject, "", f"Batch: {manifest.get('batch_id') or 'not completed'}",
             f"Table: {'.'.join(table)}", f"Collected unique products: {manifest.get('output_rows', 0)}"]
    for sort in ("main", "bsr"):
        listing = manifest.get(sort) or {}
        state = listing.get("state") or ("complete" if listing.get("stop_reason") in {"last_page", "target_reached"}
                                         else "not_started")
        count = "not started" if state == "not_started" else listing.get("rows", 0)
        lines.append(f"{sort}: {count}/{(manifest.get('targets') or {}).get(sort, '?')}"
                     f" ({listing.get('stop_reason') or 'not completed'}) state={state}")
        if "listing_state" in listing:
            lines.append(f"{sort} listing={listing['listing_state']}; candidates={listing.get('candidate_count', 0)}; "
                         f"category slots={listing.get('category_slots_read', 0)}; "
                         f"detail={listing.get('detail_state')}; processed={listing.get('detail_processed', 0)}; "
                         f"phase timing={listing.get('timing', {})}")
        if listing.get("last_requested_page") is not None:
            lines.append(f"{sort} last successful group: {listing.get('last_successful_page')};"
                         f" last requested group: {listing['last_requested_page']}; end basis: {listing.get('end_basis')}")
        if listing.get("excluded"):
            lines.append(f"{sort} exclusions/duplicates: {listing['excluded']}")
        if listing.get("tail_confirmation"):
            boundary = listing["tail_confirmation"]
            lines.append(f"{sort} tail confirmation: state={boundary.get('state')}; "
                         f"queries={boundary.get('queries', 0)}/{boundary.get('query_limit', 8)}; "
                         f"latest total={boundary.get('latest_total')}; reason={boundary.get('reason')}")
    lines.append(f"Product failures (per listing): {manifest.get('product_failure_count', 0)}")
    for product in manifest.get("product_failures", []):
        lines.append(f"Failed product: {product['sort']} page={product['page']} item={product['item']}"
                     f" code={product['error']}")
    timing = manifest.get("timing") or {}
    lines.extend([f"Collection seconds: {timing.get('collection_elapsed_s', 'not available')}",
                  f"Collection completed: {timing.get('collection_completed_at', 'not completed')}",
                  f"Collection stopped: {timing.get('collection_stopped_at', 'not stopped')}",
                  f"Current run seconds through DB: {elapsed_s}"])
    csv = manifest.get("csv") or {}
    intermediate = manifest.get("intermediate") or {}
    lines.extend([f"Final CSV state: {csv.get('state', 'not available')}; path: {csv.get('path', 'not available')}",
                  f"Intermediate state: {intermediate.get('state', 'not created')};"
                  f" path: {intermediate.get('last_path', 'not available')}"])
    database = manifest.get("database") or {}
    lines.extend([f"DB state: {database.get('state') or 'not completed'}",
                  f"DB confirmed: {database.get('db_written')}",
                  f"Inserted: {database.get('inserted', 0)}",
                  f"Already loaded: {bool(database.get('already_loaded'))}"])
    if database.get("backfilled"):
        lines.append(f"Backfilled: {database['backfilled']}")
    if error:
        lines.append(f"Failure code: {error}")  # caller supplies a static code only
    return subject, "\n".join(lines) + "\n"


def notify_run(output_dir: Path, manifest: dict, table, *, collection_success: bool,
               db_written: bool | None, error=None, elapsed_s=None, status_path: Path | None = None) -> dict:
    """Confirmed success gets a success subject; all other outcomes get CHECK.

    A persisted sent marker avoids duplicate mail on a load-only retry of the
    same batch/table. A crash during sending requires review instead of an
    automatic resend. CSV integrity and the database result remain independent.
    """
    table_name = ".".join(table)
    context("notification", batch_id=manifest.get("batch_id"), table=table_name)
    database = manifest.get("database") or {}
    success = (collection_success and manifest.get("success") is True and db_written is True
               and manifest.get("collection_status") in {None, "complete"}
               and not manifest.get("product_failure_count", 0)
               and database.get("state") == "success" and database.get("db_written") is True
               and database.get("table") == table_name and error is None)
    previous = (manifest.get("notifications") or {}).get(table_name) or manifest.get("notification") or {}
    same = (previous.get("batch_id") == manifest.get("batch_id")
            and previous.get("table") == table_name and previous.get("success_report") == success)
    if same and (previous.get("sent") is True or previous.get("state") == "sending"):
        state = "already_sent" if previous.get("sent") is True else "previous_send_unknown"
        result = {**previous, "state": state, "failed": state == "previous_send_unknown"}
        emit("mail_complete", **result)
        return result

    subject, report = build_report(manifest, table, success=success, error=error, elapsed_s=elapsed_s)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "mmkt_dry_email_report.txt").write_text(report, encoding="utf-8")
    with quiet_external():
        enabled = _truthy(env_value("SEG_EMAIL_NOTIFY", "0"))
        dry_run = _truthy(env_value("SEG_EMAIL_DRY_RUN", "0"))
    result = {"batch_id": manifest.get("batch_id"), "table": table_name, "success_report": success,
              "enabled": enabled, "dry_run": dry_run, "sent": False, "failed": False,
              "state": "disabled" if not enabled else "dry_run" if dry_run else "sending"}

    def save():
        manifest["notification"] = dict(result)
        manifest.setdefault("notifications", {})[table_name] = dict(result)
        write_json(status_path or output_dir / "mmkt_dry_manifest.json", manifest)

    save()  # fail before SMTP if the status cannot be persisted
    if enabled and not dry_run:
        emit("mail_start", success_report=success)
        try:
            with quiet_external():
                sent, recipients, send_error = _send(subject, report)
            # The shared sender returns raw errors: deliberately never retain them.
            result.update(sent=bool(sent), failed=not sent,
                          state="sent" if sent else "failed", recipients_count=recipients,
                          error=None if sent else send_error_code(send_error))
        except Exception as exc:
            result.update(failed=True, state="failed", error="SMTP_SEND_FAILED", error_type=type(exc).__name__)
        try:
            save()
        except Exception as exc:
            # Sent may be True. Don't turn a persistence error into another send.
            result.update(failed=True, error="MAIL_STATUS_SAVE_FAILED", error_type=type(exc).__name__)
    emit("mail_complete", **result)
    return result
