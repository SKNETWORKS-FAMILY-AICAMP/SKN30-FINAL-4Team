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
재검할 `CplFieldCode`만 선택하는 내부 계약이다.

- FIT-4는 비교 표본이 없어 항상 `INSUFFICIENT`이므로 재검하지 않는다.
- FIT-7은 Rule이 정량값을 비교하므로 재검하지 않는다.
- FIT-5의 `NO_CONDITIONS_SPECIFIED`는 유효한 정보부족 상태이므로 재검하지 않는다.
- 재검 실패 시 최초 CPL 결과와 기존 Rule 근거를 유지하고 경고만 남긴다.

최종 `CplResult`, `FitResult`, SIM 결과와 API·DB 스키마는 기존 계약을
그대로 사용한다. 피드백은 저장용 결과 DTO가 아니다.
