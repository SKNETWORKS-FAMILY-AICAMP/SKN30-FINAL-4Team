# Agent coordination

분석기는 책임별 모듈형 경계를 유지하고, 실행 순서와 재시도는 코드로
결정한다. 현재 파이프라인은 다음과 같다.

```text
Parsing → CPL draft → FIT input preflight
                    └─(최대 1회, 필요한 CPL 필드만)→ CPL final
                     → FIT final → Retrieval → SIM → Report → Chat
```

`fit_engine.inspect_fit_inputs()`는 CPL occurrence를 FIT 관계의 축·구역
선택자와 대조해 `FitInputFeedback`을 만든다. 피드백은 근거를 생성하거나
CPL 결과를 수정하지 않고, `agents.orchestrator.reconcile_cpl_for_fit()`가
재검할 필드와 필요한 축을 좁히는 내부 계약이다. 재검에서도 원문에
명시되지 않은 축은 생성하지 않으며, 실제 근거가 없으면 FIT의 정보 부족
상태를 그대로 유지한다. `source_role`은 인용 범위에서 Rule이 정하므로
role만 빠진 경우에는 LLM 재검을 요청하지 않는다.

CPL semantic fragment는 명시적 라벨과 표 인접관계로 확인된 필드 소속을
보존한다. 다른 CPL 필드 소속 fragment는 근거로 사용할 수 없고,
`공란·미기재·미작성`은 occurrence가 아니다. LLM 응답에서 일부 필드가
누락되거나 잘못되어도 정상 접지된 다른 필드는 유지한다.

- FIT-4는 비교 표본이 없어 항상 `INSUFFICIENT`이므로 재검하지 않는다.
- FIT-7은 Rule이 정량값을 비교하므로 재검하지 않는다.
- FIT-5의 `NO_CONDITIONS_SPECIFIED`는 대상군 근거가 있고 CPL 추출이 정상
  완료된 뒤 조건 근거 자체가 없을 때만 사용한다. CPL이 `LLM_*`,
  `PARSE_FAILED`이거나 축 없는 조건 원문이 남아 있으면 한 번 재검하고,
  그래도 축이 서지 않으면 `COMPARISON_EVIDENCE_MISSING`으로 남긴다.
- 정상적으로 완료된 CPL에서 단순히 특정 축이 없다는 이유로 재검하지 않는다.
- FIT-7의 단측 정량값은 `INSUFFICIENT / SINGLE_SIDED_NO_CONFLICT`로 두고
  점수에서 제외한다.
- FIT semantic 응답의 누락·중복·근거 검증 실패는 해당 관계만
  `LLM_INVALID_RESPONSE`로 격리하며, 식별 가능한 정상 관계는 보존한다.
- 재검 실패 시 최초 CPL 결과와 기존 Rule 근거를 유지하고 경고만 남긴다.

최종 `CplResult`, `FitResult`, SIM 결과와 API·DB 스키마는 기존 계약을
그대로 사용한다. 피드백은 저장용 결과 DTO가 아니다.
