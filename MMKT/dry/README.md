# MMKT DRY 단독 크롤러

현재 범위는 의류 건조기 전용 코드와 로컬 검증이다. 통합 실행·작업 스케줄러·기존 LDY 설정은 변경하지 않는다.
기본 실행은 Main 최대 100개와 BSR 최대 100개를 수집하여 CSV를 생성한다. DB에 연결하지 않는다.

## 실행

AWS에 이 브랜치의 코드가 반영된 뒤 원격 PowerShell에서 실행한다. 아래 명령은 작성 안내이며 개발 중 실행하지 않았다.

```powershell
Set-Location 'C:\samsung_dx_seg\MMKT'
$dryPython = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe'

# 1. 기존 테스트 테이블 구조만 조회. 수집·INSERT 없음.
& $dryPython -B -X utf8 -u -m dry.run --check-table

# 2. Main 100 / BSR 100 수집. CSV와 요약 manifest만 작성. DB 연결 없음.
& $dryPython -B -X utf8 -u -m dry.run

# 3. CSV 검수 후, 마지막 성공 결과를 기존 테스트 테이블에 INSERT.
# 새로 수집하지 않으며 실패·변경된 CSV는 거부한다.
& $dryPython -B -X utf8 -u -m dry.run --write-db
```

각 명령의 종료 코드 0이 성공이다. 실패하면 다음 단계로 진행하지 않는다.
원격의 기존 DB_CONFIG는 기존 내부 로더가 사용한다. 인증값을 명령에 붙여 넣거나 출력하지 않는다.
로컬에는 프로젝트 `.env`가 없어 실제 테이블 구조를 조회하지 않았다.

같은 Python이 PATH에 있다면 `run_dry.bat`도 사용할 수 있다. 배치는 작업 스케줄러에 등록하지 않았다.
캡차가 뜨면 열린 UC Chrome에서 직접 완료한다. 기본 대기 180초 후 미해결이면 수집 실패로 처리한다.
쿠키 동의는 기존 UC 처리 방식을 사용한다. 우편번호를 새로 설정하지 않는다.

## 수집 흐름

1. 기존 UcSession으로 Chrome 실행 → MediaMarkt 홈 준비 → 쿠키 처리.
2. Main과 BSR 목록을 각각 렌더링한다. 목록 API는 호출하지 않는다.
3. 별도 배너/캐러셀과 명백한 부적합 상품을 먼저 제외한다. 일반 목록의 Sponsored 카드는 포함한다.
4. 후보 상품의 상세 사양 API를 조회하고 제품명·제품유형으로 건조기 단품을 다시 확인한다.
5. 적격 상품의 리뷰 통계 API를 한 페이지만 조회한다. Main/BSR 중복 상품의 API 결과는 메모리에서 재사용한다.
6. 적격 고유 상품 수가 목표에 도달하거나 실제 마지막 페이지가 확인될 때까지 계속한다.
7. 제외·중복 제거 후 목록별 순위를 1부터 부여하고 상품번호 기준으로 합친다.
8. 정확히 20컬럼 CSV와 성공 여부·상태 코드·누락 수 등 요약 manifest를 작성한다.

`2-in-1`·`All-in-One` 표현만으로 세탁건조기로 판단하지 않는다. 이런 표현은 실제 제품유형으로 확인하며,
유형 근거가 없으면 분류 미확정으로 처리한다. 기능·부속품이 포함됐다는 문구는 제품 정체성과 구분하여
`mit Zubehör-Set` 같은 표현 때문에 건조기 단품이 제외되지 않도록 한다. 실제 세탁기·세탁건조기·세트·타워와
부속품 자체를 판매하는 상품은 계속 제외한다.

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
- 기존 시험은 추천 동반 응답을 거부하던 검사에서 중단됐으므로, 이번 수정 후 원격 재실행과 리뷰 통계 API·최종 필드값 확인이 필요하다. 임의로 전체 LDY 수집으로 전환하지 않는다.
- 공개 상품 사양에서 건조기용 표기는 확인했지만 실제 건조기 GraphQL 응답의 항목명과 누락률은 원격 첫 수집에서 확인해야 한다.

## 20컬럼 매핑

| 컬럼 | 구분 | 출처·우선순위·처리 |
|---|---|---|
| id | DB 생성 | CSV는 빈칸, INSERT에서는 제외. 실제 테이블에 identity/default가 없으면 중단 |
| country | 크롤러 생성 | LDY 공통값 `SEG` |
| account_name | 크롤러 생성 | LDY 공통값 `Mediamarkt` |
| item | 목록 | LDY와 동일한 MediaMarkt 상품번호 `sku_id` |
| sku | 상세 API | LDY와 같은 대상 상품의 `Modelkennung` 우선. 없으면 API 상품명 → 목록 상품명의 명시적 모델 토큰. 유효값 자동 교체 없음 |
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
| loading_type | 상세 API 정규화 | 건조기 사양 `Beladung`: `Frontlader` → `Front load`, `Toplader` → `Top load`. 미확인 표현은 결측 |
| capacity | 상세 API 정규화 | 건조 전용 `Füllmenge Baumwolle (Trocknen)` → 같은 표기의 `in kg` → `Nennkapazität` 순. 단위 공백 제거 `8 kg` → `8kg`. 세탁 용량은 사용하지 않음 |

미제공 값은 CSV 빈칸, DB NULL이다. 0은 API가 실제 0을 반환한 경우에만 사용한다.
별점 평균은 API 분포에서 파생한 값이다. 실제 대상 SKU의 기술 사양만 선별에 사용하며, 추천상품·마케팅 설명 전체에서 단어를 찾지 않는다.
건조기 사양 표기의 공개 근거: [SIEMENS WT43N202](https://www.mediamarkt.de/de/product/_siemens-wt43n202-trockner-80-kg-b-2580915.html), [건조기 카테고리](https://www.mediamarkt.de/de/category/trockner-4.html).

DB 적재 직전에 information_schema로 컬럼·자료형·NULL·기본값 존재·id 생성을 확인한다.
텍스트 컬럼은 표시값을 유지한다. 숫자 컬럼이면 유로/퍼센트/kg 표시를 제거해 숫자로 전달한다.
필수값 누락, 미지원 자료형, 잘못된 숫자, 길이 초과는 INSERT 전에 거부한다. 테이블을 새로 만들거나 변경하지 않는다.
INSERT는 트랜잭션으로 처리하며 DELETE/UPDATE는 하지 않는다. 같은 성공 결과로 `--write-db`를 반복하면 중복 행이 생길 수 있으므로 한 번만 실행한다.
쓰기 중 오류로 `db_written: null`이 나오면 결과가 불확실할 수 있으므로 해당 batch_id의 적재 여부를 확인한 뒤 재시도한다.

## 설정 전환

- 기본 Main 수량: `dry/config.py`의 `MAIN_TARGET_UNIQUE=100`. 나중에 300으로 수정하거나 실행 시 `--target 300`.
- BSR 기본 수량: 같은 파일의 `BSR_TARGET_UNIQUE=100` 또는 `--bsr-target 100`.
- 기본 적재 테이블: 같은 파일의 `DB_TABLE`. 현재는 반드시 테스트 테이블이다.
- 본테이블: 검수 후 명시적으로 `--write-db --table production` 사용. `PRODUCTION_TABLE`에 주소가 있다.
- 산출물: `dry/data/output/mmkt_dry_output.csv`, `mmkt_dry_manifest.json`.
- 출력 폴더는 `--output-dir` 또는 기존 방식의 `MMKT_DRY_OUTPUT_ROOT`로 변경 가능하다.

```powershell
# 검수 후 Main 300 수집. 여전히 DB 연결/적재 없음.
& $dryPython -B -X utf8 -u -m dry.run --target 300

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
```

실제 AWS UC 접속·건조기 API·테이블 구조·INSERT는 이 로컬 테스트의 검증 범위가 아니다.

2026-10-22 추천 동반 응답 보완 후 검증 결과: DRY 전용 29개 통과. MMKT 전체 발견 테스트 152개 중 기존 알림 실패 1개는 제외했다.
실패한 테스트는 `test_email_report_includes_unresolved_review_partial`이며, 테스트와 알림 코드 모두 main과 동일하고 독립 실행에서도 재현됐다.
해당 기존 실패를 명시적으로 제외한 151개는 모두 통과했다. 기존 알림 코드는 이번 범위에서 수정하지 않았다.
구문 검사와 `git diff --check`도 통과했다. commit/push·AWS 배포·실제 DB 접속/적재는 수행하지 않았다.
