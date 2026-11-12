# OTTO 검수 항목 추적

기준: 2026-11-12 작업 브랜치. 최초 발견사항은 작업 공간의 `SEG_크롤링_검증/SEG_검수_발견사항_전수대조.csv` O01–O24와 연결된 원본을 따른다. 아래 상태는 실운영 전체의 정확도나 과거 DB 데이터가 정정됐다는 뜻이 아니다.

| 번호 | 항목 | 현재 상태와 근거 |
| --- | --- | --- |
| O01 | 동일 상품명 옵션 누락 | TV·REF·LDY의 상품·옵션 키 선정과 순위 회귀 테스트 통과. DRY는 별도의 기존 상품명 중복 제거 방식을 유지. `common/targets.py`, `tests/test_targets_ranks.py` |
| O02 | Bluetooth 버전의 HDR 혼입 | 동일 옵션 PDP의 정확한 HDR W 행만 읽도록 수정·반례 검증. 기존 과거값 보충을 사용자가 유지하도록 정해 이력의 오염값 재유입 위험은 남음. `tv/config.py`, `common/db_save.py`, `tests/test_spec_fallbacks.py` |
| O03 | 모델 SKU 혼입 | 다중 모델 PDF의 선택 옵션 대조를 회귀 검증. 단일 모델 PDF와 비교 페이지가 다른 사례는 원본 자료 부족으로 미확인. `ref/config.py`, `tests/test_ref_multimodel_sku.py` |
| O04 | 광고에 BSR 순위 부여 | TV·REF·LDY는 일반 노출 옵션에 순위 부여. DRY는 기존처럼 메인 상위 100개에 동일 순위 부여. `common/targets.py`, `dry/pipeline.py`, `tests/test_targets_ranks.py` |
| O05 | 시작가 표시 소실 | 목록과 최종 CSV의 표시를 보존. DB 적재에서는 기존대로 제외하므로 DB 저장 요구와 스키마 확인 전까지 부분 해결. `common/listing.py`, `common/db_save.py`, `tests/test_db_save_policy.py` |
| O06 | HDR 전력에 해상도 혼입 | 선택 옵션의 정확한 HDR W 항목 외 값은 제외하는 합성·보존 원본 테스트 통과. O02의 이력 보충 위험과 실연결 미검증은 동일. `tv/config.py`, `tests/test_spec_fallbacks.py` |
| O07 | TV 화면 크기 누락 | 기존 우선순위를 유지하며 동일 옵션의 비교 페이지로 보강하는 반례 테스트 통과. `tv/config.py`, `tests/test_spec_fallbacks.py` |
| O08 | 세탁기 투입 방식 누락 | 비교 제목·같은 옵션 카테고리와 ZenRows 상세 보강 경로를 검증. 실제 ZenRows 연결과 동일 옵션 PDP의 원본 재대조는 미완료. `ldy/config.py`, `tests/test_variation_identity.py` |
| O09 | 본품 외 상품 포함 | 보고된 장난감·커버 등과 정상 상품 반례를 회귀 검증. 모든 비대상 유형이 제거됐다는 뜻은 아님. `tests/test_category_classification.py` |
| O10 | HDR·용량 공란 | 실제 같은 옵션의 원본 접근이 필요한 항목은 미확인. 공란 자체를 오류나 원본 미제공으로 단정하지 않음. |
| O11 | SKU·용량 차이 후보 | 출처 우선순위와 동일 옵션별 원본 대조가 필요한 사례는 미확인. |
| O12 | 리뷰 요약·추천 의사 | 평가 수와 리뷰 수를 구분하고 원본에 없는 별점의 합성 0을 제거. 리뷰 요약은 기존 비활성 정책 유지, 사이트 제공 여부 미확인. `common/review_rating.py`, `tests/test_review_ratings.py` |
| O13 | 유사상품 응답 | 과거 보존 응답의 비교 범위에서는 대체로 일치하나 접근 실패 사례와 PDP 전체는 미검증. |
| O14 | 옵션 보강 응답 성공 오판 | 누락·중복·미요청 ID를 진단하고 유효 상품은 계속 처리. `common/listing.py`, `dry/client.py`, `tests/test_listing_recovery.py`, `tests/test_dry_listing_first.py` |
| O15 | 같은 배치 재적재 중복 | TV·REF·LDY는 기존 insert-only 동작으로 중복 위험이 남음. DRY의 중복 방지와 별개이며 운영 제약·복구 절차에 맞는 수정 검증이 필요. `common/db_save.py`, `dry/storage.py` |
| O16 | 저장 실패 신호 | 빈 입력 실패 및 알림 지속 검증. 없는 테이블의 기존 성공·건너뛰기는 의도된 패스 유지 지시로 보존하므로 이 위험은 별도로 남음. `tests/test_pipeline_failure_status.py`, `tests/test_db_save_policy.py` |
| O17 | 모델번호의 HDR 혼입 | 정확한 HDR 라벨·W 값만 허용하는 반례 테스트 통과. O02의 이력 보충 위험과 실연결 미검증은 동일. `tests/test_spec_fallbacks.py` |
| O18 | 일반 전력의 HDR 보완 | SDR·PDF·비교값을 HDR PDP 값으로 혼용하지 않는 테스트 통과. O02의 이력 보충 위험은 별도. `tests/test_spec_fallbacks.py` |
| O19 | 과거값의 다른 옵션 혼입 | 사용자가 요청한 기존 이력 보충 방식 유지. 옵션 일치가 입증되지 않는 과거값 혼입 위험은 해결한 것으로 표시하지 않음. `common/last_known_db.py` |
| O20 | 수집 대상과 최종 파일 키 차이 | 배치 ID가 없는 과거 파일끼리 같은 실행인지 미확인. 새 listing manifest와 CSV의 실행 ID 대조는 과거 파일 차이의 원인 증명이 아님. `common/targets.py` |
| O21 | 냉장고 총용량 오기입 | 동일 옵션 비교표 총용량 우선과 URL 수치 반례를 회귀 검증. `tests/test_spec_fallbacks.py` |
| O22 | 음료 냉장고 용량 | 순용량과 총체적의 요구 정의가 미확정되어 판정 보류. |
| O23 | 평점 필수 제약에 따른 적재 거절 | NULL을 0으로 조작하지 않고 제약을 보고하는 모의 테스트 통과. 운영의 현재 제약과 실제 적재는 별도 확인 필요. `common/db_save.py`, `tests/test_review_ratings.py` |
| O24 | 냉장고 유형 공란 | 기존 정책상 의도된 NULL과 PDP 보강 제외를 유지. 업무 정의의 별도 확인 전까지 수정 완료로 보지 않음. `common/ref_type_policy.py`, `tests/test_spec_fallbacks.py` |

실시간 수집 실험은 DB·메일을 모의 처리했고 ZenRows 상세 보강을 끈 상태에서 네 품목 각 20개를 수집했다. 건조기의 BSR은 독립 목록이 아니라 메인 상위 순위다. 상세 원본 접근이 차단된 항목과 실제 ZenRows 연결·운영 적재는 검증하지 않았다.
