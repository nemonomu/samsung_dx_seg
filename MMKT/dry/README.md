# MMKT DRY 단독 크롤러

기본 실행과 `run_dry.bat`는 Main 최대 300개와 BSR 최대 100개를 수집하고 CSV 저장·결측 보완 후 기존 본테이블 `dx_seg.dx_seg_ldy_dryer_retail`에 적재한다.
루트 `run_seg_all.bat`가 호출하는 `run_seg_all.py`의 7번째 작업이다. 앞선 여섯 작업의 명령·순서·환경은 유지한다.
이 변경의 개발 검증은 모의 브라우저·DB·메일로 수행하며 실제 운영 실행·배포·스케줄러 설정 변경은 하지 않는다.
**DB에 연결하지 않고 수집만 하려면 `--collect-only`를 사용한다.** `--write-db`는 기존 검증 CSV만 적재하며 재수집하지 않는다.

## 실행

AWS에 이 브랜치의 코드가 반영된 뒤 원격 PowerShell에서 실행한다. 아래 명령은 작성 안내이며 개발 중 실행하지 않았다.

```powershell
Set-Location 'C:\samsung_dx_seg\MMKT'
$dryPython = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe'

# 1. 기존 본테이블 구조만 조회. 수집·INSERT·메일 없음.
& $dryPython -B -X utf8 -u -m dry.run --check-table

# 2. Main 최대 300 / BSR 최대 100 수집만 실행. DB·메일 없음.
& $dryPython -B -X utf8 -u -m dry.run --collect-only

# 3. CSV 검수 후, 마지막 성공 결과만 선택한 본테이블에 INSERT.
# 새로 수집하지 않으며 실패·변경된 CSV는 거부한다.
& $dryPython -B -X utf8 -u -m dry.run --write-db --table production

# 기본 단독 실행: 수집 → CSV → 세 컬럼 결측 보완 → 본테이블 적재 → 메일.
& $dryPython -B -X utf8 -u -m dry.run
# 같은 Python이 PATH에 있으면 다음 BAT로도 동일하게 실행한다.
.\run_dry.bat

# TEST에서 시험하려면 명시적으로 선택한다. TEST를 본테이블로 자동 복사하지 않는다.
& $dryPython -B -X utf8 -u -m dry.run --table test

# 통합 실행: 여섯 기존 작업 후 DRY. 각 작업 종료를 기다리며 실패해도 다음 작업을 진행한다.
Set-Location 'C:\samsung_dx_seg'
.\run_seg_all.bat
```

위 명령들은 실행 방법 안내이며 이번 개발 중 실제 수집·DB 접속·적재는 실행하지 않았다.
각 명령의 종료 코드 0이 성공이다. 실패하면 로그에서 수집 성공·DB 반영·메일 상태를 따로 확인한다.
메일만 실패한 경우도 DRY 종료 코드는 1이며, 이미 확정된 DB 적재 성공을 실패·미반영으로 바꾸지 않는다.
원격의 기존 DB_CONFIG는 기존 내부 로더가 사용한다. 인증값을 명령에 붙여 넣거나 출력하지 않는다.
개발 중 실제 테이블 구조·권한은 조회하지 않는다. 운영 실행 시 기존 안전 로더가 설정을 사용한다.

같은 Python이 PATH에 있다면 `run_dry.bat`도 사용할 수 있다. 배치는 작업 스케줄러에 등록하지 않았다.
캡차가 뜨면 열린 UC Chrome에서 직접 완료한다. 기본 대기 180초 후 미해결이면 수집 실패로 처리한다.
쿠키 동의는 기존 UC 처리 방식을 사용한다. 우편번호를 새로 설정하지 않는다.

## 수집 흐름

1. 기존 UcSession으로 Chrome 실행 → MediaMarkt 홈 준비 → 쿠키 처리.
2. Main의 실제 마지막까지 후보 목록을 렌더링·저장한다. 이 단계에서 상세 API는 호출하지 않는다.
3. 각 묶음의 기존 카드 필드·원래 순서를 읽고 별도 배너/캐러셀과 명백한 부적합 상품을 먼저 제외한다. 일반 목록의 Sponsored 카드는 포함한다.
   제목에 `Portabel`이 포함되면 대소문자와 관계없이 제외한다. `Portable`·`Tragbar`·미니 건조기 전체로 조건을 확대하지 않는다.
4. 후보 파일의 배치·정렬·순서·상품 식별값과 저장 내용을 검증한 뒤, 저장 순서대로 Main 상세 사양 API로 건조기 단품을 다시 확인한다.
5. 적격 상품의 리뷰 통계 API를 한 페이지만 조회한다. Main 적격 고유 상품이 목표에 도달하거나 저장 후보가 소진되면 상세를 마친다.
6. BSR 전체 후보 확보 → BSR 상세 선별을 같은 방식으로 처리한다. 성공한 Main/BSR 상세 API 결과는 메모리에서 재사용한다.
7. 제외·중복 제거 후 목록별 순위를 1부터 부여하고 상품번호 기준으로 합친다.
8. 정확히 20컬럼 CSV와 성공 여부·상태 코드·누락 수 등 요약 manifest를 작성한다.
9. 기본 실행은 같은 대상 테이블에서 `sku`, `loading_type`, `capacity` 결측을 보완한 뒤 적재한다. `--collect-only`는 8단계에서 끝난다.
10. 수집과 DB 적재의 확정 성공일 때 성공 메일을 보낸다. 통합 실행기는 메일을 따로 보내지 않는다.

`2-in-1`·`All-in-One` 표현만으로 세탁건조기로 판단하지 않는다. 이런 표현은 실제 제품유형으로 확인하며,
유형 근거가 없으면 분류 미확정으로 처리한다. 기능·부속품이 포함됐다는 문구는 제품 정체성과 구분하여
`mit Zubehör-Set` 같은 표현 때문에 건조기 단품이 제외되지 않도록 한다. 실제 세탁기·세탁건조기·세트·타워와
부속품 자체를 판매하는 상품은 계속 제외한다.

추가 선별 근거로 같은 제목 안의 `9 kg / 5 kg` 같은 용량 쌍과 `1400 U/Min.` 같은 탈수 회전수를 함께 확인한다.
`8 kg / 8 kg`처럼 같은 수치도 판정 대상이며, 용량 쌍이나 회전수 하나만으로는 제외하지 않는다.
`mit`·`für` 등의 뒤에 나오는 부속품/호환 설명은 이 복합 판정 근거로 섞지 않는다.
명확한 혼합 상품은 `washer_dryer` 정상 제외로 기록하고, 실제 유형 미확정 실패와 구분한다.
상품번호·브랜드 예외는 없으며 `product_excluded.evidence`에 일체형 표기 또는 용량 쌍·회전수를 기록한다.

실제 응답에서 확인한 `Wärmepumpetrockner`(`Wärmepumpentrockner`에서 `n`이 빠진 표기)도
히트펌프 건조기로 인식한다. 선별용 비교에만 적용하며 상품명·제품유형 원본·SKU는 바꾸지 않는다.
상품번호별 예외나 임의의 유사 단어 판정은 추가하지 않고, 세탁기·세트·일체형·배너 제외를 우선한다.

목표 미달이어도 마지막 페이지가 확인되면 정상 완료한다. 0개인 실제 빈 목록도 정상이다.
목록 접속 차단, 끝으로 확인되지 않은 반복 페이지, 불완전한 광고 카드와 목록 파싱 실패는 전체 수집 실패로 처리한다.
이 경우 manifest를 실패로 바꿔 이전 CSV가 잘못 적재되지 않게 한다.
개별 상품의 상세 API·파싱·제품유형 확인 실패는 상품번호·목록·묶음 번호·원인을 남기고 다음 상품으로 진행한다.
실패 상품은 목표 수량·순위에서 제외하고 저장한 다음 후보로 보충한다. 상세 처리 중 뒤늦게 새 목록을 조회하지 않는다. 같은 목록에서 실패한 상품은 반복 호출하지 않으며,
BSR에서는 다시 시도할 수 있다. 정상 상품의 Main/BSR 상세 캐시는 유지한다.
성공한 상품은 CSV 저장·적재하되, 실패가 하나라도 있었으면 `collection_status=partial`과 실패 내역을 기록한다.
이때 종료 코드 1은 **일부 상품 확인 필요**를 뜻하며, 수집 중간에 멈췄거나 정상 상품 적재까지 실패했다는 뜻은 아니다.
`collection_completed`, `product_failure_count`, `db_written`을 함께 확인한다. 개별 상품 오류로 모두 실패한 경우 빈 진단 CSV는 보존하지만 DB는 호출하지 않는다.
동일 API 작업의 인증(401)·차단(403)·제한(429)·서버/전송·JSON/최상위 data 오류가 제한 재시도 후 서로 다른 두 상품에서 연속 확인되면
`DETAIL_API_COMMON_FAILURE`로 중단한다. 성공하면 해당 작업의 연속 실패 기록은 초기화한다. 한 상품의 403만으로 공통 장애를 확정하지 않는다.
명시적인 GraphQL 검증·persisted query 누락·미인증 오류 코드는 재시도 소진 후 바로 공통 장애로 처리한다.
공통 장애 때는 중간 자료와 이미 확보한 적격 결과만 보존하고 최종 CSV·DB·성공 메일로 진행하지 않는다.
실제 정상 끝까지 확인한 적격 0건은 `database.state=skipped_empty`, `db_written=false`로 DB에 접속하지 않는다.
수집 자체는 정상 완료할 수 있지만 DB 삽입 성공 메일로 표현하지 않으며 CHECK 보고서에 적격 0건·DB 미실행을 표시한다.

목록 후보에는 수량 상한을 두지 않는다. `--target`·`--bsr-target`은 **상세 선별의 최종 적격 목표만** 바꾼다.
따라서 `--target 1 --bsr-target 1`도 각 정렬의 목록은 끝까지 조회하며 짧은 목록 점검 명령이 아니다.
목록의 가격·원가·할인·평가와 광고 구분 등 기존 필드를 보존하며, 가격 결측만으로 후보를 제외하지 않는다.

상품 식별은 LDY와 같은 `sku_id`이며 최종 `item`으로 이동한다.
Main/BSR 중복은 한 행으로 합친다. Main의 상품명·가격·URL이 우선이며 BSR에만 있으면 BSR 값을 사용한다.
배너와 일반 카드에 동일 번호가 있어도 카드별로 판단하므로 일반 카드는 유지된다.

## 마지막 목록 판정과 중간 보존

DRY는 `dry/listing.py`의 전용 수집 루프를 사용한다. 기존 여섯 제품의 공통 목록 수집기는 변경하지 않는다.
페이지 HTML에 이미 있는 `ProductListPage.products`, 목록 건수 문구, 독일어 카테고리 제목의 상품 수(있을 때),
더보기 제어와 실제 일반 상품 카드를 확인한다. 확인되지 않은 `pageCount`·`currentPage` 필드나 목록 API는 사용하지 않는다.

- 화면의 종료 건수 표시가 있어도, 읽은 카테고리 상품 자리 수와 일치하고 총수 충돌이 없어야 종료한다.
- 빈 카테고리는 종료 건수 표시 또는 건조기 제목의 0개 표시와 빈 목록 상태·화면·비활성 더보기를 함께 확인한다.
  건수 근거 없는 빈 화면만으로 정상 종료하지 않는다.
- 마지막 건수 표시가 사라졌다면 먼저 종료 후보만 기록한다. 이전 묶음들이 연속이고 크기가 일정하며,
  바뀌지 않은 사이트 총수에 현재 묶음까지의 카테고리 자리 수가 정확히 도달해야 한다.
  현재 목록 상태의 상품들과 실제 화면의 일반 카드도 일치하고 활성 더보기가 없어야 한다.
- 그 다음 묶음을 렌더링해 바로 직전 끝 후보와 같은 카테고리 상품들이 반환되고 같은 조건이 다시 확인되면
  `stable_total_slots_and_verified_tail_repeat` 근거로 직전 묶음을 마지막으로 확정한다. 확인 요청은 상세 API를 재호출하지 않는다.
- 자리 수는 **선별·중복 제거 전 카테고리 목록의 위치 수**다. 배너나 상태 밖 추가 Sponsored 카드는 자리 수에 더하지 않는다.
  추가 일반 Sponsored 카드는 적격 수집 대상에 계속 포함하며, Main/BSR 목표 수량과 종료 판단을 구분한다.
- 총수 변경, 불완전한 화면, 상태 불일치, 중간 반복, 종료 근거 없는 짧은 목록은 이 보완으로 정상 종료되지 않는다.
  근거가 부족하면 실패로 남기며 Main/BSR 결과와 중간 파일을 보존한다.
- `raw_last_page`, `raw_has_next`는 기존 화면 파서의 관측값이고, `last_page`·`end_basis`는 교차 확인 후 파생한 판단이다.
  `page_ready`는 빠른 관측값만 기록하며 최종 묶음 판정은 뒤의 `pagination_decision`·`page_summary`를 확인한다.
  `observed_totals`, `category_slots_read`, `observed_block_size`, `state_page_objects`, `tail_dom_verified`도 요약 로그에 남긴다.

매 묶음·재시도 뒤 배치별 JSON 중간 파일에 **후보 목록·제외 사유·완료한 20컬럼 적격 행과 안전한 진행 요약**을 구분해 저장한다.
후보에는 기존 공개 카드 필드와 `listing_sort`, `page`, `page_position`, `listing_order`, `observed_at`이 포함된다.
`listing_order`는 파싱된 카드의 원래 묶음·위치이며 카테고리 자리 수나 최종 순위와 다르다.
상세 단계에는 처리한 상품의 상태·사유·필요한 모델/용량/로딩타입/별점 값만 별도 `detail_results`에 저장한다.
원문 HTML·요청/응답·전체 상세 캐시는 저장하지 않는다. `artifact_kind=intermediate`, `success=false`, `loadable=false`이며
최종 CSV와 별개다. 자동 재개 기능은 제공하지 않고 `--write-db`도 중간 파일을 적재하지 않는다.
중간 저장 실패는 `INTERMEDIATE_SAVE_FAILED`, 최종 CSV 실패는 `FINAL_CSV_SAVE_FAILED`,
최종 검증 요약 저장 실패는 `FINAL_MANIFEST_SAVE_FAILED`로 기록하고 DB를 호출하지 않는다.
파일 교체는 임시 파일 작성 후 원자적으로 수행하며, 저장 실패 시 앞서 저장한 중간 파일은 보존한다.

실제 목록 오류의 실패 manifest·보고서에도 확보 수량·개별 실패·마지막 정상 묶음·실패 요청 묶음을 유지한다.
`candidate_count`는 1차 제외·중복 제거 후 후보, `rows`는 상세 선별을 통과한 최종 적격 수다.
`listing_state`와 `detail_state`를 따로 표시하므로 목록 실패 전에 후보를 확보했어도 상세 미실행이면 적격 수는 0이다.
`candidate_path`는 마지막으로 저장·재검증한 후보 파일이며, 저장 실패 시 그 이전의 성공 파일을 유지한다.
상세 처리는 원래 묶음 번호와 순서로 기록한다. Main이 완료된 뒤 BSR 목록에 실패해도 완료 Main의 적격 행과 후보 파일은 유지한다.
`state=not_started`, `rows=null`은 미실행이며, 실행해서 확보한 0개와 구분한다.
이번 제공 로그의 205개 상품 전체는 원래 최종 CSV가 없어 복구하지 않았다. 테스트 상품은 합성 데이터이며,
원래 로그의 수량과 페이지 신호만 사용해 Main 205/BSR 100 및 모의 DB 205행 연결을 검증한다.

## 상세 API 범위와 확인 한계

- 사양: 기존 `GetComparisonTableRecommendations` 조회를 재사용하되 `limit=0`, `cofrConfig.isEnabled=false`로 설정한다.
- 리뷰 통계: 기존 `GetProductReviews`의 첫 페이지만 사용하여 `totalResults`와 별점 분포를 읽는다.
- `GetReviewsSummary`, 추가 리뷰 페이지, 별도 상세 HTML, 배송·픽업·추천상품 수집과 AI 요약은 실행하지 않는다.
- 등록된 persisted query는 서버에 미리 정해진 조회여서 개별 응답 필드를 클라이언트에서 줄일 수 없다. 같은 응답에 부수 필드가 올 수 있으나 메모리에서 필요한 값만 추출한다. 리뷰 본문과 원문 요청·응답은 보관·출력하지 않는다.
- 2026-10-22 AWS 시험에서 위 사양 API의 HTTP 200·GraphQL 오류 없음과 대상 상품·추천상품의 동반 반환을 확인했다. `limit=0`은 추천 미반환을 보장하지 않는다. 요청 상품번호와 일치하는 대상만 읽고 동반 상품은 무시한다. 대상 상품이 없거나 사양 응답 구조가 잘못되면 해당 상품 실패로 처리한다.
- 사양 목록이 명시적으로 빈 배열/null인 정상 응답은 제목 근거를 사용하고 근거 없는 필드는 NULL로 남긴다. 사양 필드 자체 누락이나 잘못된 자료형은 정상 결측과 구분한다. 건조기 유형 근거는 여전히 필요하며, 적격 상품의 리뷰 통계 API도 정상 응답이어야 한다.
- 후속 AWS Main 1개/BSR 1개 시험은 총 4회 API 호출과 CSV 2행 생성에 성공했다. 표본의 모델·가격·할인율·리뷰 통계·투입 방식·용량을 확인했으며 DB에는 적재하지 않았다.
- `Wärmepumpetrockner` 표기 인식 보완 후 제공된 AWS 결과에서는 Main 100/BSR 100, 병합 고유 107개 수집 및 TEST 적재가 성공했다. 이후 제공된 Main 300 실행은 Main 205개 확보 후 반복 목록 오류로 DB 단계 전에 중단됐다. 이번 종료 판정 보완본은 아직 AWS에서 실행하지 않았다.

## 20컬럼 매핑

| 컬럼 | 구분 | 출처·우선순위·처리 |
|---|---|---|
| id | DB 생성 | CSV는 빈칸, INSERT에서는 제외. 실제 테이블에 identity/default가 없으면 중단 |
| country | 크롤러 생성 | LDY 공통값 `SEG` |
| account_name | 크롤러 생성 | LDY 공통값 `Mediamarkt` |
| item | 목록 | LDY와 동일한 MediaMarkt 상품번호 `sku_id` |
| sku | 제목 → 상세 API | 목록 → 현재 API 제목에서 명확한 모델을 우선한다. 없을 때만 명시적인 모델 라벨을 사용한다. `CSOE H8A2DE-S`, `WPT 7154`, 분리된 문자·숫자 모델과 실제 `++`/`+++`를 보존한다. 제목과 스펙 충돌 시 제목 선택. 소비전력·기능·별도 에너지등급·시리즈 숫자는 제외 |
| retailer_sku_name | 목록 | Main 우선, BSR 전용 상품은 BSR |
| product_url | 목록 | Main 우선, BSR 전용 상품은 BSR. 상품번호·도메인 확인 후 추적 쿼리/fragment 제거 |
| count_of_reviews | 상세 API | `reviews.totalResults`. LDY와 같은 사이트 리뷰 결과 수 의미이며 별점만 있는 항목이 포함될 수 있음. 텍스트 리뷰 개수로 해석하지 않음 |
| star_rating | 상세 API → 목록 → 0 | 정상적인 1~5점 분포의 가중평균, 소수 1자리. 없으면 Main → BSR 목록 별점. API가 별점 수 0을 확인하면 평균 0. 최종 결측도 0 |
| count_of_star_ratings | 상세 API → 목록 → 0 | 1~5점 분포의 count 합계. 없으면 Main → BSR 목록 AggregateRating 수, 최종 결측은 0. 리뷰 결과 수를 대신 넣지 않음 |
| final_sku_price | 목록 → API 상태 파생 문구 | Main 가격 우선, 없으면 같은 상품의 BSR 가격. 양쪽 가격이 없고 기존 상세 API에서 현재 구매 불가가 확인되면 `Artikel ist derzeit nicht verfügbar`. 그 외는 결측. 기존 유로 표시 형식 유지 |
| original_sku_price | 목록 | Main 우선, BSR 전용 상품은 BSR. 원가 미표시면 결측 |
| savings | 목록 정규화 | 사이트 할인율에서 음수 기호 제거. `-10%` → `10%`, 가격으로 임의 계산하지 않음 |
| main_rank | 크롤러 파생 | Main 적격 고유 상품의 순서 1..N, Main에 없으면 결측 |
| bsr_rank | 크롤러 파생 | BSR 적격 고유 상품의 순서 1..N, BSR에 없으면 결측 |
| calendar_week | 크롤러 생성 | LDY와 동일하게 서버 현지 시각의 ISO 주차 `w43` 형식 |
| crawl_datetime | 크롤러 생성 | LDY `crawl_strdatetime` 규칙을 이 컬럼명에 적용. 서버 현지 시각 `YYYY-MM-DD HH:MM:SS` |
| batch_id | 크롤러 생성 | LDY와 동일한 `m_YYYYMMDD_HHMMSS`. Main/BSR 전체에 한 값 사용 |
| loading_type | 제목 → 상세 API | 명확한 제목 표기 우선, 없으면 건조기 `Beladung`. `Frontlader` → `Front load`, `Toplader` → `Top load`. 제품 종류로 추정하지 않음 |
| capacity | 제목 → 상세 API | 각 출처에서 kg 우선, 없으면 g. 스펙은 건조 전용 `Füllmenge Baumwolle (Trocknen)`, 같은 라벨의 `in kg`, `Nennkapazität`만 사용. 별도 unit도 보존. `4 kg` → `4kg`, `3000 g` → `3000g`. 무단 단위 추정·단위 변환·제품 무게 사용 없음 |

미제공 값은 원칙적으로 CSV 빈칸, DB NULL이다. 예외적으로 `star_rating`, `count_of_star_ratings`는 정상 수집 후 최종 결측이면 CSV에 `0`, DB에도 숫자형이면 0/텍스트형이면 `"0"`으로 전달한다.
기존 검증 CSV 재적재 시 같은 두 컬럼의 빈칸만 메모리 복사본에서 0으로 채우며 원본 파일과 digest는 유지한다.
원문 누락과 정규화 0은 `rating_defaulted` 로그로 구분한다. 접속/API 실패·잘못된 별점 분포는 실패로 처리하며 0으로 숨기지 않는다.
별점 분포는 0건인 점수 구간이 생략된 정상 응답도 처리한다. 예를 들어 5점 9건·3점 1건·4점 1건이면 별점 수는 11, 가중평균은 4.7이다.
계산에만 생략 구간의 건수를 0으로 취급하고 원문은 바꾸거나 저장하지 않는다. 중복 점수·1~5 범위 밖 점수·음수/소수 건수·잘못된 자료형은 여전히 실패한다. `rating_distribution_parsed`에 받은 구간 수와 계산 결과를 기록한다.
별점 평균은 API 분포에서 파생한 값이다. 실제 대상 SKU의 기술 사양만 선별에 사용하며, 추천상품·마케팅 설명 전체에서 단어를 찾지 않는다.

### 제목 모델과 구매 불가 문구

- 제목 모델이 명확하면 API의 다른 모델값이나 숫자 식별값보다 우선한다. API 모델은 제목 안에 실제 존재하는 경우 분리된 모델의 경계 확인에만 도움을 주며, 제목의 첫 모델을 뒤의 다른 모델로 바꾸지 않는다.
- `field_selected`의 `source=title`, `spec_value`, `spec_label`, `conflict`로 선택값과 스펙 충돌을 확인한다. 제목·스펙에 모델이 없으면 NULL이며 기존 DB 보완 규칙만 적용한다.
- 사용자 원격 진단에서 현재 `GetComparisonTableRecommendations` 응답의 대상 상품 `cofrProductAggregate` 안 상태 필드가 확인됐다. 요청 횟수·변수는 늘리지 않는다.
- `cofrOnlineStatusFeature.isAvailableAndBuyable=false`, `cofrDeliveryFeature.delivery.displayStatus=NOT_AVAILABLE`, `cofrPickupFeature.displayStatus=NOT_AVAILABLE`가 모두 확인되고 목록 가격도 없을 때만 구매 불가 문구를 선택한다.
- 배송 불가만 있거나 픽업 가능·구매 가능·필드 누락·알 수 없는 상태이면 이 문구로 바꾸지 않는다. 선택 필드가 명시적으로 잘못된 자료형이면 상품 파싱 실패로 기록한다.
- 이 문구는 화면 문장의 원문 복사가 아니라 API 상태의 표준 표시값이다. `purchase_status`와 최종 `field_selected`의 `source=API_status`, `derived=true`로 구분한다. 중간 저장마다 동일 문구 선택 로그를 반복하지 않는다.
- `onlineStatus` 원문과 진단용 `UNRECOGNIZED_STATUS`는 판정에 사용하지 않는다. 상태 부재나 API 오류를 품절로 숨기지 않으며, 추천상품의 상태도 사용하지 않는다.
- 상태는 상세 메모리 캐시에만 좁게 보관하며 최종 CSV·중간 상품 행은 계속 정확히 20컬럼이다. 이전 CSV 재적재에서는 API를 부르거나 가격을 재해석하지 않는다.
건조기 사양 표기의 공개 근거: [SIEMENS WT43N202](https://www.mediamarkt.de/de/product/_siemens-wt43n202-trockner-80-kg-b-2580915.html), [건조기 카테고리](https://www.mediamarkt.de/de/category/trockner-4.html).

DB 적재 직전에 information_schema로 컬럼·자료형·NULL·기본값 존재·id 생성을 확인한다.
텍스트 컬럼은 표시값을 유지한다. 숫자 가격/할인율 컬럼이면 유로/퍼센트 표시를 제거해 숫자로 전달한다.
capacity는 단위 보존이 가능한 텍스트 자료형이어야 한다. 실제 테이블이 숫자형이면 단위를 버리거나 테이블을 변경하지 않고 명확한 오류로 중단한다.
필수값 누락, 미지원 자료형, 잘못된 숫자, 길이 초과는 INSERT 전에 거부한다. 테이블을 새로 만들거나 변경하지 않는다.
INSERT는 트랜잭션으로 처리하며 DELETE/UPDATE는 하지 않는다.
동일 대상 테이블·배치는 트랜잭션 advisory lock으로 직렬화한다. 같은 batch_id의 상품 구성이 모두 적재되어 있으면 재입력을 생략한다.
기존 배치에 일부 상품만 있거나 상품 구성이 다르면 `DB_BATCH_PARTIAL_OR_CONFLICT`로 중단해 수동 확인할 수 있게 한다.
쓰기 중 오류로 `db_written: null`이 나오면 결과가 불확실할 수 있으므로 해당 batch_id의 적재 여부를 확인한 뒤 재시도한다.

## DB 결측 보완과 결과 구분

- 현재 선택한 적재 테이블만 참조한다. 기본은 본테이블이며 `--table test`일 때만 TEST를 참조·적재한다.
- 상품 키는 `country + account_name + item`이다. SKU는 조회 키로 사용하지 않는다.
- 신규 `sku`, `loading_type`, `capacity`가 각각 NULL/빈칸일 때만 같은 상품의 과거 비결측 값으로 보완한다.
- `crawl_datetime DESC NULLS LAST, id DESC` 순으로 읽고 각 컬럼에서 가장 최근의 유효값을 고른다. 공백 값과 잘못된 SKU는 건너뛴다.
- 유효한 신규 값과 가격·리뷰·순위 등 다른 컬럼은 보완하거나 덮어쓰지 않는다. 과거 행도 수정하지 않는다.
- raw CSV는 DB 보완 전 수집 결과다. DB 보완은 메모리 복사본에 수행하므로 CSV 내용과 manifest의 digest 검증을 유지한다.
- manifest의 `success`는 수집 산출물 작성 완료·검증 가능 여부다. 오류 없는 수집 여부는 `collection_status=complete`와 `product_failure_count=0`으로 구분한다. 이전 manifest에 이 두 필드가 없으면 기존 정상 결과로 처리한다. 적재 상태는 `database.state`, `database.db_written`, `inserted`, `backfilled`, `already_loaded`에 별도로 기록한다.
- DB 조회/적재 실패 시 성공 CSV는 유지한다. API·파싱 실패는 결측으로 바꿔 DB 값으로 숨기지 않는다.
- CSV 검증 직후부터 원래 manifest·배치·검증 정보를 함께 보존한다. 적재 상태 저장이나 알림 기록이 실패해도 새 실패 배치로 바꾸지 않으므로 `--write-db`로 재수집 없이 재시도할 수 있다. 실제 DB 반영 여부는 기존 동일 배치 검사로 다시 확인한다.
- 적재 전 상태 저장 실패는 `DB_STATE_SAVE_FAILED`이며 DB를 호출하지 않는다. COMMIT 성공 후 상태 저장 실패는 `DB_COMMITTED_MANIFEST_SAVE_FAILED`로 구분하고 로그에는 `db_written=true`를 유지한다. 진행 중 상태만 파일에 남으면 반영 여부는 null(확인 필요)이다.
- `--write-db`의 CSV 검증 자체가 실패한 경우에도 원래 CSV·manifest는 덮어쓰지 않는다. 해당 실행의 실패 알림 상태는 `mmkt_dry_load_failure.json`에 분리해 기록한다.
- 이번 추출 기준은 수정 후 재수집 결과에 적용된다. 이전 CSV의 값은 자동 재해석하지 않으며, 명백히 잘못된 SKU가 있는 CSV는 적재 전에 거부한다.

## 진행도와 실패 로그

화면과 실행별 UTF-8 파일에 같은 JSON 이벤트를 기록한다.

`C:\samsung_dx_seg\MMKT\dry\data\logs\mmkt_dry_YYYYMMDD_HHMMSS_ffffff.log`

- 시작 시 batch_id·목표 수량·대상 테이블·실행 모드와 로그 경로를 표시한다.
- Main/BSR·페이지·상세 처리 상품번호·적격 수/목표·제외 이유·중복 수·API 호출 수·경과시간을 기록한다.
- 30초마다 `waiting` 이벤트로 현재 단계와 마지막 진행 위치를 표시한다. 응답이 없는 외부 호출도 대기 단계가 보인다.
- `field_selected`는 선택한 SKU·용량·로딩타입의 제목/스펙/DB 출처와 관련 값·단위를 남긴다.
- 상세 `page`는 저장한 후보의 원래 목록 묶음 번호다. 첫 주소 1, 다음 주소 2이며 DB 컬럼은 추가하지 않는다.
- `collection_complete`는 수집 완료 시각·소요시간, `run_complete`는 적재·메일을 포함한 `total_elapsed_s`를 남긴다. 실패 시에도 `collection_elapsed_s`와 중단 시각이 유지된다.
- `page_summary`의 `count_kind=candidate`·`candidate_count`는 목록 후보 수이며 이 단계의 `accepted`는 0이다.
  `detail_phase_start`·`detail_phase_complete`와 상세의 `accepted`로 실제 적격 수를 확인한다.
  `pagination_decision`에 종료 근거·총수 충돌·상태/화면 일치를 기록한다.
- 정렬별 `timing`에 목록·상세 시작/종료·소요시간을 따로 기록한다. 한 실행의 상세 캐시는 유지하지만 프로세스 종료 후 자동 재개는 지원하지 않는다.
- `run_complete`·메일 보고서는 Main/BSR의 완료·미실행·실패, 최종 CSV 저장 상태, 중간 파일 경로와 DB 실행 상태를 구분한다.
- `product_failed`는 개별 상품 실패와 계속 진행 여부를 기록한다. 최종 `product_failure_count`는 Main/BSR별 실패 상품 건수 합계이므로 같은 상품이 양쪽에서 실패하면 각각 센다. 실패 상품의 별점이나 사양을 0/NULL로 바꿔 적재하지 않는다.
- `mail_complete`는 발송·실패·비활성·모의 전송·이미 발송됨을 구분하며 SMTP 원문 오류나 수신 주소를 기록하지 않는다.
- API의 작업명·HTTP 상태·GraphQL 오류 여부·오류 분류·재시도와, 실패 단계·오류 코드·소스 위치를 기록한다.
- DB 보완 건수·INSERT 시작·COMMIT 대기·확정 건수·이미 적재됨 여부를 구분한다.
- `run_complete`의 `collection_success`, `db_success`, `db_written`을 따로 확인한다. COMMIT 오류의 반영 여부는 null(확인 필요)로 표시한다.
- 로그 생성에 실패하면 수집/DB 작업을 시작하지 않는다. UC의 원문 출력과 원문 오류는 버리고 안전한 요약만 남긴다.
- 쿠키·토큰·인증정보·접속 문자열·요청/응답 원문·HAR는 기록하지 않는다.

출력 폴더: `C:\samsung_dx_seg\MMKT\dry\data\output`

- CSV: `mmkt_dry_output.csv`
- 수집·적재 요약: `mmkt_dry_manifest.json`
- 메일 보고서: `mmkt_dry_email_report.txt` (수집 전용·구조 확인 모드에서는 만들지 않음)
- 기존 CSV 검증 실패 시 별도 알림 기록: `mmkt_dry_load_failure.json` (원래 manifest 보존)
- 배치별 후보: `intermediate\<batch_id>\main_candidates_0001_attempt_01.json`, `bsr_candidates_0001_attempt_01.json` 등
- 상세 진행 결과: 같은 폴더의 `main_detail_0001.json`, `bsr_detail_0001.json` 등 (번호는 상세 처리 수)
- 기본 중간 폴더 전체 경로: `C:\samsung_dx_seg\MMKT\dry\data\output\intermediate\<batch_id>`
- `--output-dir`, `--log-dir`로 각각 경로를 변경할 수 있다.

## 설정 전환

- 기본 Main 수량: `dry/config.py`의 `MAIN_TARGET_UNIQUE=300` 또는 실행 시 `--target`.
- BSR 기본 수량: 같은 파일의 `BSR_TARGET_UNIQUE=100` 또는 `--bsr-target 100`.
- 기본 적재/결측 참조: `DB_TABLE=PRODUCTION_TABLE`, CLI 기본 `--table production`.
- 명시적 시험: `--table test`를 사용하며 주소는 `TEST_TABLE`에 있다. 본테이블은 `PRODUCTION_TABLE`이다.
- 산출물: `dry/data/output/mmkt_dry_output.csv`, `mmkt_dry_manifest.json`.
- 출력 폴더는 `--output-dir` 또는 기존 방식의 `MMKT_DRY_OUTPUT_ROOT`로 변경 가능하다.

```powershell
# 기본 Main 300/BSR 100 수집만 실행.
& $dryPython -B -X utf8 -u -m dry.run --collect-only

# 본테이블 구조를 읽기 전용으로 확인한 다음, 검수된 결과만 본테이블에 적재.
& $dryPython -B -X utf8 -u -m dry.run --check-table --table production
& $dryPython -B -X utf8 -u -m dry.run --write-db --table production
```

## 로컬 검증

실제 API/DB 호출 없이 테스트용 응답으로 선별, 페이지 계속 읽기, 목표 미달, 광고 카드, 순위/병합,
API 오류, 20컬럼/양수 할인율, DB 자료형 변환과 INSERT 범위를 검사한다.

```powershell
Set-Location 'C:\samsung_dx_seg\MMKT'
python -B -X utf8 -m unittest discover -s tests -p test_dry_pipeline.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_updates.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_production.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_resilience.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_listing_end.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_review_fixes.py -v
python -B -X utf8 -m unittest discover -s tests -p test_dry_listing_phases.py -v
```

실제 AWS UC 접속·건조기 API·테이블 구조·INSERT는 이 로컬 테스트의 검증 범위가 아니다.

DB·UC·API는 로컬 테스트에서 모의 객체로 대체한다. 실제 AWS 테이블 자료형·권한·트랜잭션과 사이트 응답은 원격에서 확인해야 한다.

기존 알림 테스트의 `StopIteration`은 모의 파일 응답의 개수·순서 불일치였다. 파일 이름별 응답으로 보완했으며 기존 공통 알림 운영 코드는 수정하지 않는다.

2026-10-22 본테이블·통합 전환 검증: MMKT 200개(DRY 77개 포함), OTTO 79개, 루트 실행·공통 테스트 27개 통과. 건너뛴 테스트는 없다.
OTTO 1개는 저장소 안 테스트 폴더의 생성 권한 오류가 있어 테스트의 출력 루트만 임시 폴더로 바꿔 동일한 검증을 수행했다. OTTO 소스나 검증 조건은 바꾸지 않았다.
브라우저·HTTP API·DB·SMTP 연결은 모의 객체와 외부 소켓 연결 차단 상태로 검증했다. BAT 종료 코드 시험은 가짜 Python 명령만 실행했다.
후속 별점 분포 수정 검증에서는 제공된 3개 구간 응답으로 오류를 재현한 뒤, MMKT 205개(DRY 82개 포함)를 다시 실행해 모두 통과했다. 실제 API·DB·메일은 실행하지 않았다.
개별 상품 실패 격리·정상 빈 사양·검증 CSV 복구 보완 후에는 MMKT 216개(DRY 93개 포함)가 모두 통과했다. 외부 연결은 차단하고, 실제 브라우저·DB·SMTP 대신 모의 객체를 사용했다.
종료 판정·중간 보존 보완에서는 제공 로그와 같은 신호로 기존 반복 오류를 먼저 재현했다. 새 로직에서 같은 수량의 합성 상품으로
Main 205/BSR 100·모의 본테이블 205행 연결을 확인했으며, 원격의 실제 상품 결과를 복구하거나 실제 적재한 것은 아니다.
실제 마지막 HTML이 제공되지 않아 목록 상태·화면 카드·더보기 일치 여부는 AWS에서 추가 확인해야 한다.
이번 보완 후 로컬 MMKT 237개(DRY 114개 포함)와 변경 Python 8개 구문 검사가 통과했다.
제공 로그의 23개 묶음별 확보 수량·제외 사유가 테스트 입력과 일치하는지도 별도로 확인했다.

2026-10-22 후속 검수 반영: MMKT 260개(DRY 137개 포함), OTTO 79개, 루트 실행기·공통 테스트 27개가 통과했다.
OTTO의 기존 1개 테스트는 저장소 내부 임시 폴더 생성 권한 문제로 처음 실패했으며, 해당 테스트의 출력 루트만
시스템 임시 폴더로 바꿔 같은 검증을 통과했다. OTTO 소스·검증 조건은 수정하지 않았다.
제공된 207개 제목의 구버전/신버전 판정을 비교했을 때 확인된 BOSCH 혼합 상품 1개만 추가 제외되고 나머지 206개 판정은 동일했다.
이는 제목 비교이며 보존되지 않은 전체 API 스펙을 재현한 결과는 아니다. 검수된 SKU 12개 결측도 기존 로그의 모델 근거와 제목을 대조해 유지됨을 확인했다.
혼합 2개 정상 제외·목표 미달 종료·SKU 충돌·구매 불가 문구·CSV 검증·모의 DB·한글/영문 성공 메일을 한 흐름으로 검증했다.
구매 상태 자료형 오류 상품은 실패로 남기되 다른 정상 상품의 적재를 이어가며 CHECK를 유지하는 경우도 검증했다.
외부 소켓·실제 UC/DB/SMTP는 차단하거나 모의 객체로 대체했으며 실제 수집·적재·메일은 실행하지 않았다.
이 검수 반영본의 AWS 최신 제목·상태 응답 변형, 실제 본테이블 INSERT, 한글 메일 수신 화면은 아직 확인하지 않았다.

## 메일과 재실행

- DRY 제목은 한글 상태와 영문 상태를 함께 표시한다. 본문 첫 부분은 한글 요약, 아래 `English details`는 기존 영문 상세다. 다른 제품의 공통 발송기·설정은 변경하지 않는다.
- 한글 요약은 Main/BSR 실제 수량·목표·종료 사유·제외/중복·실패 상품과 이유, DB 반영 여부·신규 적재·결측 보완 건수, 수집 소요시간·이번 실행의 적재 단계까지 시간을 구분한다. 미실행과 0개, 상품 일부 실패와 DB 반영 성공, COMMIT 반영 불확실을 구분한다.
- 기존 `SEG_EMAIL_NOTIFY` 활성화 및 `SEG_EMAIL_DRY_RUN` 비활성 설정을 그대로 따른다. 설정 비활성·모의 전송이면 실제 발송하지 않고 로그에 표시한다.
- 기존 `common.notify._send`와 내부 설정 로더를 재사용하며 수신자·계정·전송 방식은 새로 만들지 않는다.
- 정상 마지막 페이지 때문에 300개 미만이어도 실제 수량/목표와 `last_page`를 표시한 성공 보고서다.
- 수집·적재 실패 또는 COMMIT 불확실은 기존 방식의 `[CHECK]` 실패 보고서이며 성공 메일을 보내지 않는다.
- 개별 상품 실패가 있으면 정상 상품 적재가 성공해도 `[CHECK]` 일부 수집 보고서로 보내고 실패 상품번호·목록·묶음 번호·원인을 표시한다. 같은 배치의 재적재에서도 실패 이력을 지우거나 성공 메일로 바꾸지 않는다.
- `--collect-only`, `--check-table`은 메일을 보내지 않는다. `--write-db`는 검증 파일 적재 확정 후 보고한다.
- 동일 batch_id·대상 테이블의 성공 메일 발송 기록이 있으면 재적재 시 중복 발송하지 않는다. 다른 테이블 발송 기록을 재사용하지 않는다.
- SMTP 발송 후 상태 파일 저장이 실패하면 발송 여부와 오류를 로그에 남기고, 다음 실행에서 `previous_send_unknown`으로 자동 재발송을 보류한다. 운영자가 메일 수신 여부와 해당 manifest를 확인해야 한다.
- 실제 운영 SMTP 권한·접속과 본테이블 자료형·권한·사이트 최신 응답은 원격 실행 전후에 확인해야 한다.

## 총수 변동 시 끝부분 재확인

- 기존 `?page=N` 렌더링 조회와 첫 묶음 검증은 유지한다. 과거 총수 변동 이력을 지우거나 최솟값을 종료 기준으로 택하지 않는다.
- 정규 묶음의 상품·중간 결과를 먼저 보존한 뒤, 총수가 변했고 더보기가 없는 마지막 후보를 별도 단계로 확인한다.
- `직전 묶음 → 마지막 후보 → 다음 묶음 → 직전 묶음` 순서로 최신 총수·상품번호·카드 완전성·버튼 상태를 교차 확인한다.
- 최신 총수는 적격 건조기 수가 아닌 필터 전 목록 위치와 비교한다. 일반 목록 Sponsored는 수집하고 별도 배너는 위치를 늘리지 않는다.
- 정상 확인은 4회이며 Main·BSR마다 추가 조회 예산 8회를 공유한다. 재시도나 새 후보 때문에 이 예산을 초기화하지 않는다.
- 다음에 새 상품이 확인되면 정규 수집으로 넘긴다. 확인용 조회는 상품·순위·상세 API 캐시를 확정하지 않는다.
- 정상 끝이 확인되면 목표 미달도 완료하고 BSR·병합·CSV·적재·기존 알림으로 이어진다. 불명확·접속·파싱 실패는 성공 처리하지 않는다.
- 로그/manifest의 `tail_confirmation`에 조회 수·예산·최신 총수·판정 근거가 남는다. `latest_total`은 마지막으로 읽은 총수이며, `confirmed_total`은 끝이 증명됐을 때만 채운다.
- 중간 파일은 `intermediate/<batch_id>/<main|bsr>_tail_read_번호_page_묶음.json`, `..._tail_result_번호_page_묶음.json`로 구분한다. 기존 정규 묶음 파일을 덮지 않으며 모두 자동 적재 불가다.
- 이 확인은 끝부분 경계에 대한 증거다. 재방문하지 않은 중간 묶음의 상품 교체나 전체 최신 목록의 누락 0을 보장하지 않는다.

외부 연결 없이 실제 DRY 실행 흐름과 모의 DB·메일까지 확인:

```powershell
Set-Location 'C:\samsung_dx_seg\MMKT'
python -B -X utf8 -m unittest discover -s tests -p test_dry_tail_confirmation.py -v
```

합성 상품·화면으로 검증하며 원격의 실제 HTML·API·DB·SMTP 결과를 검증한 것은 아니다.

## 정렬별 목록 선확보의 확인 범위

2026-10-25 사용자 AWS 진단의 8개 관측은 모두 파싱·내부 목록 정합성이 확인됐다.
첫 화면의 `dom_complete=false`·`state_coherent=true`, Main 총수 286→288, 끝부분 반복 신호를 합성 연결 검사에 반영한다.
진단에 없는 중간 상품·HTML은 복구하지 않는다. `heading_totals`는 빈 목록일 수 있고 정렬 선택지 전체는 선택된 정렬의 증거가 아니다.
일반 목록 Sponsored의 실제 표본과 전체 중간 목록, 새 단계 분리 코드의 실제 AWS 성공은 이 진단으로 확인되지 않았다.
8회 목록 조회의 관측 `elapsed_s` 합계는 173.4초다. 목록 중 상세 API 대기는 없어지지만 BSR도 끝까지 읽어 조회량이 늘 수 있다.
기존 렌더링·광고 대기시간을 줄이지 않으며, 전체 실행시간 단축·단일 시점 순위·상품 누락 0을 보장하지 않는다.
