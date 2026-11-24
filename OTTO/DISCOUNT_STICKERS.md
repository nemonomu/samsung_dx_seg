# OTTO discount_type

TV·냉장고·세탁기의 인기순 목록에서 상품 옵션을 확인하고 Crocotile API에서 스티커 이미지와 행사 ID를 읽는다.
해당 옵션의 행사 안내 제목을 우선 읽는다. 제목을 확인할 수 없으면 동일 옵션·행사의 상세 스티커 버튼 이름 또는 대체 텍스트를 확인하고, 기존에 검증된 이미지 매핑으로 보완한다.
상품권·회원 혜택·할인율·할인 기간은 행사 스티커명에 포함하지 않는다. 건조기는 별도 수집 범위를 따른다.

## 저장 및 보고

- 확인된 스티커는 listing/targets CSV에 원문(`discount_type_raw`), 이미지 URL·식별자와 행사 ID를 보존한다. 행사 안내나 상세에서 읽은 제목에는 출처도 기록한다.
  최종 CSV와 DB의 `discount_type`에는 이미 정해진 영어 표기를 적용하고, 대응하는 표기가 없는 확인된 행사명은 원문을 유지한다.
- 행사명과 기존 이미지 매핑을 모두 확인할 수 없으면 URL·식별자와 영향 상품을 보존하고 최종값은 빈 값(DB NULL)으로 둔다.
  이미지를 `OTTO/references/discount_stickers/`에 다운로드한다. 같은 이미지가 이미
  보관되어 있으면 다시 받지 않으며, URL의 크기/포맷 쿼리는 동일 이미지로 취급한다.
  다운로드 실패는 로그·manifest·메일 보고에 표시하고 상품 수집은 계속한다.
  실패한 이미지는 다음 listing 실행에서 다시 시도한다.
- 이미지가 없는 상품: 정상적인 NULL이며 미등록 스티커 경고 대상이 아니다.
- 미등록 이미지별 SKU·상품 URL·이미지 URL·보관 경로/오류는 기존 카테고리별
  이메일 보고서에 합쳐진다. 기존 `SEG_EMAIL_NOTIFY` 및 `SEG_EMAIL_DRY_RUN` 설정을 따른다.
  실행 배치와 listing 실행 ID를 대조하여 이전 실행의 경고가 섞이지 않게 한다.

| 스티커 원문 | 최종 영어값 |
| --- | --- |
| Deal des Monats | Deal of the month |
| Deal der Woche | Deal of the week |
| Deal & Gewinne | Deal & Win |
| Unser Hero | Our Hero |
| Heroes | Our Hero |
| Premium Hero | Premium Hero |
| Technik Highlights | Tech Highlights |
| Pre-Sale | Pre-Sale |

2026-09-18에 확인한 이미지 10종(색상·gesponsert 유무 변형 포함)을 등록했다.
2026-11-12에 확인한 Pre-Sale 이미지 2종도 등록했다.
새 행사도 같은 옵션의 행사 안내나 상세 스티커에서 확인되면 이미지 등록 없이 수집할 수 있다.

## 이미지 보완 및 기존 CSV 재처리

텍스트 출처를 확인할 수 없는 스티커는 보관 이미지를 검토해 기존 이미지 매핑에 추가할 수 있다.
최종 출력 재실행에서는 targets에 저장된 확인 출처와 원문을 우선 사용하고, 이전 이미지 기반 targets에는 갱신된 매핑을 적용한다.
행사 안내 조회를 적용하려면 listing부터 다시 실행한다.
이미지 정보가 없는 이전 CSV의 할인 문구는 최종값에서 제외하고 재수집 필요를 보고한다.
기존 DB 행은 소급 수정하지 않는다.

## 검증

오프라인 테스트에서 동일 이미지의 서로 다른 행사, 상세 스티커의 옵션 일치 여부, 기존 이미지 매핑, 실패 진단과 targets → 최종 CSV → 모의 DB 삽입을 검증한다.
