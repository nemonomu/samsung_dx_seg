"""DRY report from final rows/state; reuse only OTTO's existing SMTP transport."""
from common.io_util import env_value
from dry.config import BSR_LIMIT


def build_report(state, rows):
    counts, db = state.get("counts", {}), state.get("database", {})
    history = state.get("history_backfill", {})
    code = state.get("collection_exit_code", state.get("exit_code", 0))
    outcome = "실패" if code in (3, 4, 5) else "확인 필요" if code else "완료"
    mode = "CSV 재적재" if state.get("mode") == "load_only" else "수집"
    subject = f"[SEG] OTTO DRY {mode} {outcome}"
    main_count = sum(row.get("main_rank") not in (None, "") for row in rows)
    bsr_count = sum(row.get("bsr_rank") not in (None, "") for row in rows)
    reason = {"target_reached": "목표 도달", "actual_search_end": "실제 검색 끝까지 확인"}.get(
        state.get("end_reason"), "완료 전 중단" if code in (3, 4, 5) else "기존 CSV")
    lines = [subject, f"실행: {state.get('started_at', '')}", f"배치: {state.get('batch_id', '')}",
             f"적재 대상: {state.get('table', '')}", f"결과: {len(rows)}행 / 목표 최대 {state.get('target', '')}행",
             f"순위: main {main_count}개 / BSR {bsr_count}개 (상한 {BSR_LIMIT})", f"종료 사유: {reason}",
             f"제외 {counts.get('excluded', 0)} / 중복 {counts.get('duplicates', 0)}",
             f"상품 실패 {counts.get('item_failures', 0)} / 요청 오류 상품 {counts.get('request_error_items', 0)}"
             f" / 추출 오류 상품 {counts.get('extraction_error_items', 0)} / 가격 경고 {counts.get('price_warning_items', 0)}",
             f"DB: {state.get('db_status', 'not_started')} / 삽입 {db.get('inserted', 0)} / 중복 {db.get('duplicates', 0)}",
             f"과거 DB 보충: {history.get('status', 'not_started')} / {history.get('recovered_rows', 0)}개 상품",
             *[f"  {field}: 보충 {history.get('recovered_fields', {}).get(field, 0)} / 최종 NULL "
               f"{sum(row.get(field) in (None, '') for row in rows)}" for field in ("sku", "capacity", "loading_type")],
             "NULL 자체는 수집 실패가 아님. 요청·추출 오류는 위 항목에서 별도 확인."]
    if state.get("mode") == "load_only":
        lines.append("DB 보충 건수는 원수집 기준이며 이번 재적재에서 재조회하지 않음.")
    if state.get("failure"):
        lines.append("실행 오류: " + state["failure"])
    if history.get("error"):
        lines.append("DB 보충 오류: " + history["error"])
    lines += [f"소요시간 (메일 처리 전): {state.get('total_seconds', 0):.2f}초"]
    lines += [f"  {stage}: {seconds:.2f}초" for stage, seconds in state.get("durations", {}).items()]
    lines += [f"CSV: {state.get('csv', '')}", f"로그: {state.get('log', '')}",
              f"진단: {state.get('diagnostics', '')}"]
    return subject, "\n".join(lines) + "\n"


def send(state, rows, *, sender=None):
    result = dict(status="disabled", sent=False, recipients_count=0, error=None)
    try:
        enabled = str(env_value("SEG_EMAIL_NOTIFY", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
        if not enabled:
            return result
        subject, body = build_report(state, rows)
        result["subject"] = subject
        dry_run = str(env_value("SEG_EMAIL_DRY_RUN", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
        if dry_run:
            result["status"] = "dry_run"
            return result
        if sender is None:
            from common.notify import _send
            sender = _send
        sent, recipients, error = sender(subject, body)  # Existing SMTP timeout: 30 seconds; one attempt.
        result.update(status="sent" if sent else "failed", sent=bool(sent), recipients_count=recipients,
                      error=None if sent else "smtp_settings_missing" if error == "missing SMTP settings" else "smtp_send_failed")
    except Exception as exc:
        result.update(status="failed", error="email_failed:" + type(exc).__name__)
    return result
