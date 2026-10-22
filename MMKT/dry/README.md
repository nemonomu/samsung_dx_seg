# MMKT DRY 단독 크롤러

현재 범위는 의류 건조기 전용 코드와 로컬 검증이다. 통합 실행·작업 스케줄러·기존 LDY 설정은 변경하지 않는다.
기본 실행과 `run_dry.bat`는 Main 최대 100개와 BSR 최대 100개를 수집하고 CSV를 저장한 뒤 기존 TEST 테이블에 적재한다.
**DB에 연결하지 않고 수집만 하려면 `--collect-only`를 사용한다.** `--write-db`는 기존 검증 CSV만 적재하며 재수집하지 않는다.

## 실행

AWS에 이 브랜치의 코드가 반영된 뒤 원격 PowerShell에서 실행한다. 아래 명령은 작성 안내이며 개발 중 실행하지 않았다.

```powershell
Set-Location 'C:\samsung_dx_seg\MMKT'
$dryPython = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe'

# 1. 기존 테스트 테이블 구조만 조회. 수집·INSERT 없음.
& $dryPython -B -X utf8 -u -m dry.run --check-table

# 2. Main 100 / BSR 100 수집만 실행. DB 연결 없음.
& $dryPython -B -X utf8 -u -m dry.run --collect-only

# 3. CSV 검수 후, 마지막 성공 결과를 기존 테스트 테이블에 INSERT.
# 새로 수집하지 않으며 실패·변경된 CSV는 거부한다.
& $dryPython -B -X utf8 -u -m dry.run --write-db

# 기본 단독 실행: 수집 → CSV → 세 컬럼 결측 보완 → TEST 적재.
& $dryPython -B -X utf8 -u -m dry.run
# 같은 Python이 PATH에 있으면 다음 BAT로도 동일하게 실행한다.
.\run_dry.bat
```

위 명령들은 실행 방법 안내이며 이번 개발 중 실제 수집·DB 접속·적재는 실행하지 않았다.
각 명령의 종료 코드 0이 성공이다. 실패하면 로그에서 수집 성공과 DB 반영 여부를 따로 확인한다.
원격의 기존 DB_CONFIG는 기존 내부 로더가 사용한다. 인증값을 명령에 붙여 넣거나 출력하지 않는다.
로컬에는 프로젝트 `.env`가 없어 실제 테이블 구조를 조회하지 않았다.

같은 Python이 PATH에 있다면 `run_dry.bat`도 사용할 수 있다. 배치는 작업 스케줄러에 등록하지 않았다.
캡차가 뜨면 열린 UC Chrome에서 직접 완료한다. 기본 대기 180초 후 미해결이면 수집 실패로 처리한다.
쿠키 동의는 기존 UC 처리 방식을 사용한다. 우편번호를 새로 설정하지 않는다.

## 수집 흐름

1. 기존 UcSession으로 Chrome 실행 → MediaMarkt 홈 준비 → 쿠키 처리.
2. Main과 BSR 목록을 각각 렌더링한다. 목록 API는 호출하지 않는다.
3. 별도 배너/캐러셀과 명백한 부적합 상품을 먼저 제외한다. 일반 목록의 Sponsored 카드는 포함한다.
   제목에 `Portabel`이 포함되면 대소문자와 관계없이 제외한다. `Portable`·`Tragbar`·미니 건조기 전체로 조건을 확대하지 않는다.
4. 후보 상품의 상세 사양 API를 조회하고 제품명·제품유형으로 건조기 단품을 다시 확인한다.
5. 적격 상품의 리뷰 통계 API를 한 페이지만 조회한다. Main/BSR 중복 상품의 API 결과는 메모리에서 재사용한다.
6. 적격 고유 상품 수가 목표에 도달하거나 실제 마지막 페이지가 확인될 때까지 계속한다.
7. 제외·중복 제거 후 목록별 순위를 1부터 부여하고 상품번호 기준으로 합친다.
8. 정확히 20컬럼 CSV와 성공 여부·상태 코드·누락 수 등 요약 manifest를 작성한다.
9. 기본 실행은 같은 대상 테이블에서 `sku`, `loading_type`, `capacity` 결측을 보완한 뒤 적재한다. `--collect-only`는 8단계에서 끝난다.

`2-in-1`·`All-in-One` 표현만으로 세탁건조기로 판단하지 않는다. 이런 표현은 실제 제품유형으로 확인하며,
유형 근거가 없으면 분류 미확정으로 처리한다. 기능·부속품이 포함됐다는 문구는 제품 정체성과 구분하여
`mit Zubehör-Set` 같은 표현 때문에 건조기 단품이 제외되지 않도록 한다. 실제 세탁기·세탁건조기·세트·타워와
부속품 자체를 판매하는 상품은 계속 제외한다.

실제 응답에서 확인한 `Wärmepumpetrockner`(`Wärmepumpentrockner`에서 `n`이 빠진 표기)도
히트펌프 건조기로 인식한다. 선별용 비교에만 적용하며 상품명·제품유형 원본·SKU는 바꾸지 않는다.
상품번호별 예외나 임의의 유사 단어 판정은 추가하지 않고, 세탁기·세트·일체형·배너 제외를 우선한다.

목표 미달이어도 마지막 페이지가 확인되면 정상 완료한다. 0개인 실제 빈 목록도 정상이다.
접속 차단, 반복 페이지, 불완전한 광고 카드, 목록 파싱 실패, 상세 API 실패와 제품유형 판단 불가는 정상 상품 부족으로 처리하지 않는다.
이 경우 manifest를 실패로 바꿔 이전 CSV가 잘못 적재되지 않게 한다.

상품 식별은 LDY와 같은 `sku_id`이며 최종 `item`으로 이동한다.
Main/BSR 중복은 한 행으로 합친다. Main의 상품명·가격·URL이 우선이며 BSR에만 있으면 BSR 값을 사용한다.
배너와 일반 카드에 동일 번호가 있어도 카드별로 판단하므로 일반 카드는 유지된다.

## 상세 API 범위와 확인 한계

- 사양: 기존 `GetComparisonTableRecommendations` 조회를 재사용하되 `limit=0`, `cofrConfig.isEnabled=false`로 설정한다.
- 리뷰 통계: 기존 `GetProductReviews`의 첫 페이지만 사용하여 `totalResults`와 별점 분포를 읽는다.
- `GetReviewsSummary`, 추가 리뷰 페이지, 별도 상세 HTML, 배송·픽업·추천상품 수집과 AI 요약은 실행하지 않는다.
- 등록된 persisted query는 서버에 미리 정해진 조회여서 개별 응답 필드를 클라이언트에서 줄일 수 없다. 같은 응답에 부수 필드가 올 수 있으나 메모리에서 필요한 값만 추출한다. 리뷰 본문과 원문 요청·응답은 보관·출력하지 않는다.
- 2026-10-22 AWS 시험에서 위 사양 API의 HTTP 200·GraphQL 오류 없음과 대상 상품·추천상품의 동반 반환을 확인했다. `limit=0`은 추천 미반환을 보장하지 않는다. 요청 상품번호와 일치하는 대상만 읽고 동반 상품은 무시한다. 대상 상품이 없거나 대상 사양을 읽지 못한 경우에는 실패 처리한다.
- 후속 AWS Main 1개/BSR 1개 시험은 총 4회 API 호출과 CSV 2행 생성에 성공했다. 표본의 모델·가격·할인율·리뷰 통계·투입 방식·용량을 확인했으며 DB에는 적재하지 않았다.
- Main/BSR 100개 시험은 제품유형 미확정으로 중단됐다. Main 첫 페이지 11개 추가 진단에서 API가 반환한 `Wärmepumpetrockner` 표기 1건을 기존 규칙이 인식하지 못하는 원인을 확인했다. 해당 표기 인식 보완 후 원격 재실행과 전체 누락률 검수가 필요하다.

## 20컬럼 매핑

| 컬럼 | 구분 | 출처·우선순위·처리 |
|---|---|---|
| id | DB 생성 | CSV는 빈칸, INSERT에서는 제외. 실제 테이블에 identity/default가 없으면 중단 |
| country | 크롤러 생성 | LDY 공통값 `SEG` |
| account_name | 크롤러 생성 | LDY 공통값 `Mediamarkt` |
| item | 목록 | LDY와 동일한 MediaMarkt 상품번호 `sku_id` |
| sku | 상세 API → 제목 | `Modelkennung`, `Modellname / -kennzeichen` 등 명시적 모델 라벨 우선. 없으면 목록 → API 제목의 모델 토큰. 소비전력·기능 표현은 제외하고 유효한 모델은 유지 |
| retailer_sku_name | 목록 | Main 우선, BSR 전용 상품은 BSR |
| product_url | 목록 | Main 우선, BSR 전용 상품은 BSR. 상품번호·도메인 확인 후 추적 쿼리/fragment 제거 |
| count_of_reviews | 상세 API | `reviews.totalResults`. LDY와 같은 사이트 리뷰 결과 수 의미이며 별점만 있는 항목이 포함될 수 있음. 텍스트 리뷰 개수로 해석하지 않음 |
| star_rating | 상세 API → 목록 | 정상적인 1~5점 분포의 가중평균, 소수 1자리. 없으면 Main → BSR 목록 별점. API가 별점 수 0을 확인한 경우 평균은 결측 유지 |
| count_of_star_ratings | 상세 API → 목록 | 1~5점 분포의 count 합계. 없으면 Main → BSR 목록의 AggregateRating 수. 리뷰 결과 수를 대신 넣지 않음 |
| final_sku_price | 목록 | Main 우선, BSR 전용 상품은 BSR. 기존 LDY의 유로 표시 형식 유지 |
| original_sku_price | 목록 | Main 우선, BSR 전용 상품은 BSR. 원가 미표시면 결측 |
| savings | 목록 정규화 | 사이트 할인율에서 음수 기호 제거. `-10%` → `10%`, 가격으로 임의 계산하지 않음 |
| main_rank | 크롤러 파생 | Main 적격 고유 상품의 순서 1..N, Main에 없으면 결측 |
| bsr_rank | 크롤러 파생 | BSR 적격 고유 상품의 순서 1..N, BSR에 없으면 결측 |
| calendar_week | 크롤러 생성 | LDY와 동일하게 서버 현지 시각의 ISO 주차 `w43` 형식 |
| crawl_datetime | 크롤러 생성 | LDY `crawl_strdatetime` 규칙을 이 컬럼명에 적용. 서버 현지 시각 `YYYY-MM-DD HH:MM:SS` |
| batch_id | 크롤러 생성 | LDY와 동일한 `m_YYYYMMDD_HHMMSS`. Main/BSR 전체에 한 값 사용 |
| loading_type | 제목 → 상세 API | 명확한 제목 표기 우선, 없으면 건조기 `Beladung`. `Frontlader` → `Front load`, `Toplader` → `Top load`. 제품 종류로 추정하지 않음 |
| capacity | 제목 → 상세 API | 각 출처에서 kg 우선, 없으면 g. 스펙은 건조 전용 `Füllmenge Baumwolle (Trocknen)`, 같은 라벨의 `in kg`, `Nennkapazität`만 사용. 별도 unit도 보존. `4 kg` → `4kg`, `3000 g` → `3000g`. 무단 단위 추정·단위 변환·제품 무게 사용 없음 |

미제공 값은 CSV 빈칸, DB NULL이다. 0은 API가 실제 0을 반환한 경우에만 사용한다.
별점 평균은 API 분포에서 파생한 값이다. 실제 대상 SKU의 기술 사양만 선별에 사용하며, 추천상품·마케팅 설명 전체에서 단어를 찾지 않는다.
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

- 현재 선택한 적재 테이블만 참조한다. 기본 TEST, `--table production`을 명시하면 본테이블이다.
- 상품 키는 `country + account_name + item`이다. SKU는 조회 키로 사용하지 않는다.
- 신규 `sku`, `loading_type`, `capacity`가 각각 NULL/빈칸일 때만 같은 상품의 과거 비결측 값으로 보완한다.
- `crawl_datetime DESC NULLS LAST, id DESC` 순으로 읽고 각 컬럼에서 가장 최근의 유효값을 고른다. 공백 값과 잘못된 SKU는 건너뛴다.
- 유효한 신규 값과 가격·리뷰·순위 등 다른 컬럼은 보완하거나 덮어쓰지 않는다. 과거 행도 수정하지 않는다.
- raw CSV는 DB 보완 전 수집 결과다. DB 보완은 메모리 복사본에 수행하므로 CSV 내용과 manifest의 digest 검증을 유지한다.
- manifest의 `success`는 수집 성공 여부다. 적재 상태는 `database.state`, `database.db_written`, `inserted`, `backfilled`, `already_loaded`에 별도로 기록한다.
- DB 조회/적재 실패 시 성공 CSV는 유지한다. API·파싱 실패는 결측으로 바꿔 DB 값으로 숨기지 않는다.
- 이번 추출 기준은 수정 후 재수집 결과에 적용된다. 이전 CSV의 값은 자동 재해석하지 않으며, 명백히 잘못된 SKU가 있는 CSV는 적재 전에 거부한다.

## 진행도와 실패 로그

화면과 실행별 UTF-8 파일에 같은 JSON 이벤트를 기록한다.

`C:\samsung_dx_seg\MMKT\dry\data\logs\mmkt_dry_YYYYMMDD_HHMMSS_ffffff.log`

- 시작 시 batch_id·목표 수량·대상 테이블·실행 모드와 로그 경로를 표시한다.
- Main/BSR·페이지·상세 처리 상품번호·적격 수/목표·제외 이유·중복 수·API 호출 수·경과시간을 기록한다.
- 30초마다 `waiting` 이벤트로 현재 단계와 마지막 진행 위치를 표시한다. 응답이 없는 외부 호출도 대기 단계가 보인다.
- `field_selected`는 선택한 SKU·용량·로딩타입의 제목/스펙/DB 출처와 관련 값·단위를 남긴다.
- API의 작업명·HTTP 상태·GraphQL 오류 여부·오류 분류·재시도와, 실패 단계·오류 코드·소스 위치를 기록한다.
- DB 보완 건수·INSERT 시작·COMMIT 대기·확정 건수·이미 적재됨 여부를 구분한다.
- `run_complete`의 `collection_success`, `db_success`, `db_written`을 따로 확인한다. COMMIT 오류의 반영 여부는 null(확인 필요)로 표시한다.
- 로그 생성에 실패하면 수집/DB 작업을 시작하지 않는다. UC의 원문 출력과 원문 오류는 버리고 안전한 요약만 남긴다.
- 쿠키·토큰·인증정보·접속 문자열·요청/응답 원문·HAR는 기록하지 않는다.

출력 폴더: `C:\samsung_dx_seg\MMKT\dry\data\output`

- CSV: `mmkt_dry_output.csv`
- 수집·적재 요약: `mmkt_dry_manifest.json`
- `--output-dir`, `--log-dir`로 각각 경로를 변경할 수 있다.

## 설정 전환

- 기본 Main 수량: `dry/config.py`의 `MAIN_TARGET_UNIQUE=100`. 나중에 300으로 수정하거나 실행 시 `--target 300`.
- BSR 기본 수량: 같은 파일의 `BSR_TARGET_UNIQUE=100` 또는 `--bsr-target 100`.
- 기본 적재 테이블: 같은 파일의 `DB_TABLE`. 현재는 반드시 테스트 테이블이다.
- 본테이블: 검수 후 명시적으로 `--write-db --table production` 사용. `PRODUCTION_TABLE`에 주소가 있다.
- 산출물: `dry/data/output/mmkt_dry_output.csv`, `mmkt_dry_manifest.json`.
- 출력 폴더는 `--output-dir` 또는 기존 방식의 `MMKT_DRY_OUTPUT_ROOT`로 변경 가능하다.

```powershell
# 검수 후 Main 300 수집만 실행.
& $dryPython -B -X utf8 -u -m dry.run --collect-only --target 300

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
```

실제 AWS UC 접속·건조기 API·테이블 구조·INSERT는 이 로컬 테스트의 검증 범위가 아니다.

DB·UC·API는 로컬 테스트에서 모의 객체로 대체한다. 실제 AWS 테이블 자료형·권한·트랜잭션과 사이트 응답은 원격에서 확인해야 한다.

2026-10-22 검증: DRY 55개 포함, 기존 알림 테스트 1개를 제외한 MMKT 177개 통과.
제외한 `test_email_report_includes_unresolved_review_partial`은 변경하지 않은 알림 테스트에서 발생하는 `StopIteration`이다.
전체 검사를 모두 통과한 것으로 간주하지 않으며 이번 범위에서 알림 코드는 수정하지 않았다.
