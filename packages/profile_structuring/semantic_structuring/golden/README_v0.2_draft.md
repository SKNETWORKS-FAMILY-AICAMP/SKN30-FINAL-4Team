# v0.2 Gold 초안

이 디렉터리의 `*.v0.2.draft.json`은 모델 입력이 아닌 사람 검토용 기대값이다.
`v0.1` 산출물을 정답으로 변환한 것이 아니라, Common IR v1 원문을 다시 읽고
v0.2 계약에 따라 작성한다.

## 초안 표현

- `source_anchor`는 CandidatePack block(일반 문단 또는 서버가 공통 IR 표에서 분리한 셀 문단)과
  그 안의 유일한 연속 문자열을 가리킨다. 실행 시 서버가 `value_source` offset으로 복원한다.
- `expected_fact`는 반드시 남아야 하는 최소 Raw Fact다. 이 목록에 없는 Fact가
  무조건 오류라는 뜻은 아니다. 완전성 Gold는 다음 검토에서 확장한다.
- `expected_recipient_gold_ids`는 해당 지원금·지원항목의 직접 수혜자를 가리킨다.
  신청주체와 정책 지원대상, 지급·정산 수령인, 직접 수혜자를 하나의 값으로
  축약하지 않기 위한 관계 기대값이다.
- `expected_measure`와 `expected_facets`는 아직 구현 전인 Derived Projection의
  기대 의미다. Raw Fact의 `value_raw`를 대신하거나 수정하지 않는다.
- `review_notes`는 사람의 확인이 필요한 해석 경계다. 모델 프롬프트에 주지 않는다.

## 현재 상태

- `125056`: 4개 지원 패키지, 수혜자, 금액·비율·기간 분리의 최소 Gold 초안.
- `125612`: 교육 단계와 최종 지원 단계를 구분하는 최소 Gold 초안.
- `125016`: 신청기업·참여청년·참여기업 수혜와 두 참여유형이 함께 있는 최소 Gold 초안.

이 초안을 검토·동결한 뒤에만 회귀 평가 코드의 fixture로 승격한다.
