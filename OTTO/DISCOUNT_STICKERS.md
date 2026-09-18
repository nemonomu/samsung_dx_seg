# OTTO discount_type

기존 Everglades/Crocotile API 수집을 유지한다. `deal.image.src`의 이미지 식별자로
원형 스티커를 구분하며, 할인 기간을 나타내는 `deal.highlight`는 사용하지 않는다.
이 기능을 위한 페이지 렌더링·OCR·번역 API 호출이나 추가 패키지는 없다.

## 저장 및 보고

- 확인된 스티커: listing/targets CSV에 독일어 라벨(`discount_type_raw`)과 이미지
  URL·식별자·deal ID를 보존한다. raw 라벨은 육안 확인한 이미지 매핑에서 얻는다.
  최종 CSV와 DB의 `discount_type`에는 영어값만 저장한다.
- 미등록 스티커: URL·식별자와 영향 상품을 보존하고 최종값은 빈 값(DB NULL)으로 둔다.
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
| Premium Hero | Premium Hero |
| Technik Highlights | Tech Highlights |

2026-09-18에 확인한 이미지 10종(색상·gesponsert 유무 변형 포함)을 등록했다.
OTTO Days는 영어 표기 자체는 유지할 수 있지만, 확인되지 않은 이미지 ID는
자동 추측하지 않고 미등록으로 보고한다.

## 새 이미지 등록 및 RDP 적용

보관 이미지 또는 원본 URL을 확인한 후 `common/discount_stickers.py`의
`STICKER_LABELS`에 이미지 ID와 원문을 추가한다. 새 문구라면
`common/translate.py`의 `STICKER_TRANSLATIONS`에도 영어값을 추가한다.
저장된 targets의 이미지 URL을 이용하므로 이후 full output 재실행에도 새 매핑이 반영된다.

RDP에서 처음 적용할 때:

```powershell
git fetch origin
git switch --track origin/fix/otto-discount-type-stickers
```

같은 브랜치를 이미 사용 중이면 `git pull --ff-only`로 갱신한다.
기존 실행 명령을 그대로 사용하되 최초 적용은 listing부터 다시 실행한다.
이미지 정보가 없는 이전 CSV의 할인 문구는 최종값에서 제외하고 재수집 필요를 보고한다.
기존 DB 행은 소급 수정하지 않는다.

## 검증

오프라인 테스트에서 이미지 매핑·중복 보관·다운로드 실패·API → targets → 최종 CSV →
모의 DB 삽입 및 메일 보고를 검증한다. 운영 DB 접속이나 실제 메일 발송은 하지 않는다.
앞서 페이지에서 확인한 TV/REF/LDY 각 100개 자료를 재생한 결과, 스티커가 있는
152개는 해당 영어값, 나머지 148개는 NULL로 일치했다.
