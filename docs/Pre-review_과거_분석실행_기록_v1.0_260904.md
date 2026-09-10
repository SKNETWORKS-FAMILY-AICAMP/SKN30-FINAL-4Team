# Pre-review 과거 분석 실행 기록

2026-09-04 추출 · 출처: 팀 Supabase `sims` 스키마 실측
대상 기간: 2026-08-31 06:11 ~ 2026-09-04 00:41

**이 문서는 DB에 기록된 값만 적는다.** 원인은 시스템이 스스로 남긴 `failure_code`·`reason_code`·`status` 까지만 쓰고, 그 너머의 해석은 하지 않는다. 데이터가 말해주지 않는 것은 "기록 없음"으로 적었다.

---

## 1. 전체 실행 결과

분석 요청 **54건**.

| 상태 | 실패 코드 | 건수 |
|---|---|---|
| `COMPLETED` | — | **47** |
| `FAILED` | `ANALYSIS_INTERRUPTED` | 4 |
| `FAILED` | `RETRIEVAL_NOT_READY` | 3 |

단계별 잔존:

```
54 업로드
 └ 51 파싱 실행        (3건은 파싱 기록 없음)
    └ 51 항목 추출
       └ 47 보고서 확정  (4건은 보고서 없음)
```

완료된 47건은 `inspection_embedding` · `retrieval_run` · `inspection_report` · `output_artifact` 가 **모두 47로 일치**한다.

**소요 시간** — 최소 15초, 최대 2분 22초, 평균 46초.

## 2. 실패 7건

| case_id | 실패 코드 | 기록된 메시지 | 입력 파일 | 날짜 |
|---|---|---|---|---|
| 2 | `ANALYSIS_INTERRUPTED` | The analysis did not survive a server restart | `사전협의요청서_미흡사례.hwpx` | 08-31 |
| 3 | `ANALYSIS_INTERRUPTED` | The analysis did not survive a server restart | `사전협의서_예시.hwp` | 08-31 |
| 1735 | `ANALYSIS_INTERRUPTED` | The analysis did not survive a server restart | 파일 기록 없음 | 09-02 |
| 1736 | `ANALYSIS_INTERRUPTED` | The analysis did not survive a server restart | 파일 기록 없음 | 09-02 |
| 2119 | `RETRIEVAL_NOT_READY` | Similar-program retrieval could not be completed | `사전협의서_예시.hwp` | 09-03 |
| 2120 | `RETRIEVAL_NOT_READY` | Similar-program retrieval could not be completed | `사전협의서_예시.hwp` | 09-03 |
| 2183 | `RETRIEVAL_NOT_READY` | Similar-program retrieval could not be completed | `사전협의서_예시.hwp` | 09-03 |

1735·1736 은 `retrieval-c8d7acc8e0@example.com` 계정 소유이며 `file_asset` 행이 없다.

## 3. 실행 조건

47건 전부 **동일한 버전**으로 돌았다. 버전이 섞인 실행은 없다.

| 항목 | 값 | 건수 |
|---|---|---|
| 파서 | `rhwp-python` `0.8.1` | 51 |
| 추출기 | `cpl-rule-llm` `cpl-alpha-v0.3+cpl-semantic-v0.9` | 51 |
| CPL 룰셋 | `cpl-alpha-v0.3` | 51 |
| 임베딩 프로파일 | `id=1` | 47 |
| 챗봇 모델 | `gpt-4o-mini` | 4 세션 |
| 검색 `top_k` | `5` | 47 |
| 검색 필터 | `search_status IN (OPEN, UNKNOWN)`, `is_current`, `source_code = BIZINFO_OPEN_API` | 47 |
| 비교 대상 공고 | **6건** | — |

`retrieval_run` 47건은 **전부 `SUCCESS`**, `error_code` 기록 없음.

## 4. 파싱 결과

| 상태 | 건수 | 추출 텍스트 길이 |
|---|---|---|
| `PARTIAL_SUCCESS` | **39** | 667 ~ 4,040자 |
| `SUCCESS` | 12 | 777 ~ 2,133자 |

51건 중 39건(76%)이 `PARTIAL_SUCCESS` 다. `FAILED` 는 0건.

파싱 상태별 CPL 확인 개수:

| 파싱 상태 | 보고서 수 | CPL 확인 최소 | 최대 | 평균 |
|---|---|---|---|---|
| `SUCCESS` | 12 | 4 | 12 | 9.1 |
| `PARTIAL_SUCCESS` | 35 | 3 | 11 | 8.3 |

> 두 집단의 건수가 12 대 35로 불균형하고 입력 문서도 다르다. 이 표는 **관측된 값**이며 인과관계를 뜻하지 않는다.

---

## 5. 입력 파일별 실행

같은 파일을 반복해서 돌린 기록이다. `sha256` 기준이라 이름이 같아도 내용이 다르면 다른 줄이다.

| 파일 | 실행 | 완료 | 실패 | CPL 확인 개수 |
|---|---|---|---|---|
| `사전협의서_예시.hwp` | **30** | 26 | 4 | **6 ~ 9** |
| `사전협의요청서_우수사례.hwpx` | 6 | 6 | 0 | **4 ~ 11** |
| `mockup_02_우수사례_뿌리산업스마트제조.hwpx` | 4 | 4 | 0 | 11 ~ 11 |
| `mockup_05_저급사례_AI바우처_모순충돌.hwpx` | 3 | 3 | 0 | **3 ~ 10** |
| `mockup_08_CPL전항목_스마트기술사업화.hwpx` | 3 | 3 | 0 | **8 ~ 10** |
| `mockup_03_보통사례_청년로컬크리에이터.hwpx` | 1 | 1 | 0 | 11 |
| `mockup_04_보통사례_친환경그린에너지.hwpx` | 1 | 1 | 0 | 11 |
| `[서식1]사전협의요청서_샘플_AI바이오실증.hwpx` | 1 | 1 | 0 | 11 |
| `mockup_08_CPL전항목_스마트기술사업화.hwpx` (다른 sha) | 1 | 1 | 0 | 12 |
| `사전협의요청서_미흡사례.hwpx` (sha `064c24…`) | 1 | 1 | 0 | 5 |
| `사전협의요청서_미흡사례.hwpx` (sha `f29278…`) | 1 | 0 | 1 | — |

`mockup_08` 과 `사전협의요청서_미흡사례` 는 이름이 같은 `sha256` 이 두 개씩 있다. 파일 내용이 도중에 바뀌었다는 뜻이다.

### 같은 입력에서 판정이 갈린 항목

`sha256` 이 같은 파일 안에서 항목별 판정이 두 종류 이상 나온 경우를 셌다. 대상은 반복 실행이 있는 파일이다.

| `field_code` | 갈린 파일 수 / 전체 |
|---|---|
| `BUSINESS_NEED` | 4 / 10 |
| `NEW_OR_CHANGED_CONTENT` | 4 / 10 |
| `PURPOSE_GOAL` | 4 / 10 |
| `TARGET_AND_CONDITIONS` | 4 / 10 |
| `DELIVERY_SYSTEM` | 3 / 10 |
| `LINKED_POLICY` | 3 / 10 |
| `SUPPORT_CONTENT_AND_SCALE` | 3 / 10 |
| `EXPECTED_EFFECTS_AND_PERFORMANCE` | 2 / 10 |
| `BUDGET` | **0 / 10** |
| `BUSINESS_PERIOD` | **0 / 10** |
| `IMPLEMENTATION_PLAN` | **0 / 10** |
| `LEGAL_BASIS` | **0 / 10** |
| `REQUEST_TYPE` | **0 / 10** |

아래 5개 항목은 같은 입력에 대해 **항상 같은 판정**이 나왔다: `BUDGET` · `BUSINESS_PERIOD` · `IMPLEMENTATION_PLAN` · `LEGAL_BASIS` · `REQUEST_TYPE`

---

## 6. CPL 결과 (663건 전수)

51번의 점검 실행 × 13항목.

| `field_code` | PRESENT | NEEDS_CONF | MISSING | N/A | PARSE_FAILED |
|---|---|---|---|---|---|
| `REQUEST_TYPE` | 20 | 30 | 1 | 0 | 0 |
| `PURPOSE_GOAL` | 43 | 8 | 0 | 0 | 0 |
| `IMPLEMENTATION_PLAN` | **0** | **51** | 0 | 0 | 0 |
| `BUSINESS_PERIOD` | 21 | 30 | 0 | 0 | 0 |
| `NEW_OR_CHANGED_CONTENT` | 33 | 14 | 4 | 0 | 0 |
| `BUSINESS_NEED` | 18 | 33 | 0 | 0 | 0 |
| `LEGAL_BASIS` | 47 | 0 | 4 | 0 | 0 |
| `LINKED_POLICY` | 35 | 16 | 0 | 0 | 0 |
| `BUDGET` | 50 | 0 | 1 | 0 | 0 |
| `TARGET_AND_CONDITIONS` | 38 | 12 | 1 | 0 | 0 |
| `SUPPORT_CONTENT_AND_SCALE` | 44 | 6 | 1 | 0 | 0 |
| `DELIVERY_SYSTEM` | 35 | 15 | 1 | 0 | 0 |
| `EXPECTED_EFFECTS_AND_PERFORMANCE` | 49 | 2 | 0 | 0 | 0 |
| **합계** | **433** | **217** | **13** | **0** | **0** |

- `IMPLEMENTATION_PLAN` 은 51건 중 `PRESENT` 가 **한 번도 없다.**
- `NOT_APPLICABLE` 과 `PARSE_FAILED` 는 663건 중 **0건.** 한 번도 나오지 않은 상태값이다.

### 기록된 사유 코드 (전수)

| 판정 | `reason_code` | 건수 |
|---|---|---|
| `PRESENT` | (없음) | 433 |
| `NEEDS_CONFIRMATION` | `LLM_INVALID_RESPONSE` | **54** |
| `NEEDS_CONFIRMATION` | `LLM_TIMEOUT` | **41** |
| `NEEDS_CONFIRMATION` | `VALUE_NOT_SPECIFIC` | 30 |
| `NEEDS_CONFIRMATION` | `REQUEST_TYPE_AMBIGUOUS` | 30 |
| `NEEDS_CONFIRMATION` | `EVIDENCE_OWNERSHIP_UNRESOLVED` | 28 |
| `NEEDS_CONFIRMATION` | `EVIDENCE_CONFLICT` | 10 |
| `NEEDS_CONFIRMATION` | `LLM_REASON_CODE_MISSING` | 6 |
| `NEEDS_CONFIRMATION` | `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` | 6 |
| `NEEDS_CONFIRMATION` | `REQUIRED_AXIS_MISSING` | 6 |
| `NEEDS_CONFIRMATION` | `EVIDENCE_AMBIGUOUS` | 6 |
| `MISSING` | (없음) | 7 |
| `MISSING` | `EXPLICIT_VALUE_NOT_FOUND` | 5 |
| `MISSING` | `REQUEST_TYPE_NOT_FOUND` | 1 |

`NEEDS_CONFIRMATION` 217건 중 **`LLM_INVALID_RESPONSE` + `LLM_TIMEOUT` + `LLM_REASON_CODE_MISSING` = 101건(47%)** 이 LLM 응답 관련 코드다.

### 사유 코드 × 항목 (상위)

| `reason_code` | `field_code` | 건수 |
|---|---|---|
| `LLM_INVALID_RESPONSE` | `IMPLEMENTATION_PLAN` | 34 |
| `VALUE_NOT_SPECIFIC` | `BUSINESS_PERIOD` | 30 |
| `REQUEST_TYPE_AMBIGUOUS` | `REQUEST_TYPE` | 30 |
| `EVIDENCE_OWNERSHIP_UNRESOLVED` | `BUSINESS_NEED` | 23 |
| `LLM_TIMEOUT` | `LINKED_POLICY` | 10 |
| `LLM_TIMEOUT` | `BUSINESS_NEED` | 10 |
| `LLM_TIMEOUT` | `IMPLEMENTATION_PLAN` | 10 |
| `LLM_TIMEOUT` | `NEW_OR_CHANGED_CONTENT` | 10 |
| `LLM_INVALID_RESPONSE` | `DELIVERY_SYSTEM` | 8 |
| `LLM_INVALID_RESPONSE` | `LINKED_POLICY` | 6 |
| `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` | `IMPLEMENTATION_PLAN` | 6 |
| `EVIDENCE_CONFLICT` | `TARGET_AND_CONDITIONS` | 5 |
| `EXPLICIT_VALUE_NOT_FOUND` | `LEGAL_BASIS` | 4 |
| `EVIDENCE_CONFLICT` | `PURPOSE_GOAL` | 3 |
| `EVIDENCE_OWNERSHIP_UNRESOLVED` | `DELIVERY_SYSTEM` | 3 |
| `LLM_INVALID_RESPONSE` | `NEW_OR_CHANGED_CONTENT` | 3 |
| `LLM_INVALID_RESPONSE` | `TARGET_AND_CONDITIONS` | 3 |

`IMPLEMENTATION_PLAN` 51건의 내역: `LLM_INVALID_RESPONSE` 34 + `LLM_TIMEOUT` 10 + `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` 6 + `EVIDENCE_OWNERSHIP_UNRESOLVED` 1 = 51.

---

## 7. FIT 결과 (47건 × 7관계 = 329건)

`module_status` 는 47건 전부 `AVAILABLE`.

`assessable_count` (비교 결과를 낼 수 있었던 관계 수, 최대 7) 분포:

| 값 | 건수 |
|---|---|
| 0 | 1 |
| 1 | 6 |
| 2 | 7 |
| 3 | **17** |
| 4 | 14 |
| 5 | 2 |
| 6·7 | 0 |

**7개 중 5개를 넘긴 실행이 없다.**

### 관계별 상태 분포

| 관계 | FIT | NEEDS_REVIEW | CONFLICT | INSUFFICIENT |
|---|---|---|---|---|
| `FIT-1` 목적 대상조건 ↔ 지원 대상 | 11 | 27 | 0 | 9 |
| `FIT-2` 목적 방향 ↔ 지원 활동·수단 | 34 | 0 | 0 | 13 |
| `FIT-3` 목적 방향 ↔ 기대효과 | 26 | 9 | 0 | 12 |
| `FIT-4` 사업 계층 간 비교 | **0** | **0** | **0** | **47** |
| `FIT-5` 대상군 ↔ 지원 조건 | 1 | 10 | 9 | 27 |
| `FIT-6` 수행기관 ↔ 절차·역할 | 7 | 3 | 0 | 37 |
| `FIT-7` 지원 내용 ↔ 지원 규모 | **0** | **0** | **0** | **47** |

- **`FIT-4` 와 `FIT-7` 은 47건 전부 `INSUFFICIENT`.** 한 번도 판정된 적이 없다.
- `CONFLICT` 는 `FIT-5` 에서만 9건 나왔다.

### 기록된 사유 코드

| `reason_code` | 판정 | 건수 |
|---|---|---|
| (없음) | `FIT` | 79 |
| `COMPARISON_EVIDENCE_MISSING` | `INSUFFICIENT` | 71 |
| `HIERARCHY_COMPARISON_NOT_AVAILABLE` | `INSUFFICIENT` | **47** |
| `COMPARISON_VALUE_INVALID` | `INSUFFICIENT` | 24 |
| `NO_CONDITIONS_SPECIFIED` | `INSUFFICIENT` | 20 |
| (null) | `NEEDS_REVIEW` | 16 |
| `SINGLE_SIDED_NO_CONFLICT` | `INSUFFICIENT` | 9 |
| `LLM_INVALID_RESPONSE` | `INSUFFICIENT` | 8 |
| (null) | `INSUFFICIENT` | 8 |
| `SCOPE_UNCLEAR` | `NEEDS_REVIEW` | 7 |
| `CONFLICT` | `CONFLICT` | 5 |
| `REVIEW_REQUIRED` | `NEEDS_REVIEW` | 3 |
| `CONDITIONS_SPECIFY_TARGET` | `NEEDS_REVIEW` | 3 |
| `TARGET_BROADNESS` | `NEEDS_REVIEW` | 2 |
| `CONFLICTING_TARGET_CONDITIONS` | `NEEDS_REVIEW` | 2 |

`HIERARCHY_COMPARISON_NOT_AVAILABLE` 47건은 `FIT-4` 의 전건이다.

---

## 8. SIM 결과

**후보 수 분포**

| 후보 수 | 보고서 수 |
|---|---|
| 0 | 1 (case 2121) |
| 5 | 46 |

후보 총 230건. 비교 대상 공고 코퍼스는 **6건**이었다.

### 축별 상태 분포 (230건 × 4축)

| 축 | SIMILAR | PARTIAL | DIFFERENT | INSUFFICIENT |
|---|---|---|---|---|
| `purpose` 사업 목적 | 20 | 99 | 106 | 5 |
| `target` 지원 대상 | **0** | 36 | **183** | 11 |
| `content` 지원 내용 | **0** | **0** | **0** | **230** |
| `delivery` 수행 체계 | 2 | 136 | 80 | 12 |

- **`content` 축은 230건 전부 `INSUFFICIENT`.** 한 번도 비교된 적이 없다.
- `target` 축은 `SIMILAR` 가 0건이다.

### 근거 (`candidate_evidence` 3,119건)

| `evidence_side` | 건수 |
|---|---|
| `REQUEST` | 2,199 |
| `ANNOUNCEMENT` | 920 |
| `COMPARISON` | 0 |

---

## 9. 챗봇

대화가 있는 케이스는 **4건**, 메시지 **16건**.

| case_id | 질문 | 답변 | 모델 |
|---|---|---|---|
| 5 | 1 | 1 | `gpt-4o-mini` |
| 6 | 1 | 1 | `gpt-4o-mini` |
| 7 | 3 | 3 | `gpt-4o-mini` |
| 2121 | 3 | 3 | `gpt-4o-mini` |

`input_tokens` · `output_tokens` 는 16건 전부 기록 없음(`NULL`).
case 2121 의 첫 질문 본문은 `"string"` 이다.

---

## 10. 한 번도 나오지 않은 값

DB 제약(`CHECK`)은 허용하지만 47건의 실행에서 **한 번도 기록되지 않은 값**이다.

| 테이블·컬럼 | 미발생 값 |
|---|---|
| `missing_check_item.result_status` | `NOT_APPLICABLE`, `PARSE_FAILED` |
| `document_parse_run.status` | `FAILED` |
| `retrieval_run.status` | `FAILED` |
| `retrieval_candidate.detail_parse_status` | `PARSING`, `SUCCESS`, `PARTIAL_SUCCESS`, `FAILED` (전부 `NOT_REQUESTED`) |
| `candidate_evidence.evidence_side` | `COMPARISON` |
| FIT `FIT-4`, `FIT-7` | `FIT`, `NEEDS_REVIEW`, `CONFLICT` |
| SIM `content` 축 | `SIMILAR`, `PARTIAL`, `DIFFERENT` |
| SIM `target` 축 | `SIMILAR` |
| CPL `IMPLEMENTATION_PLAN` | `PRESENT`, `MISSING`, `NOT_APPLICABLE` |

---

## 11. 추출 방법

이 문서의 모든 수치는 팀 Supabase 에 읽기 전용 쿼리로 얻었다. 대상 테이블:

`inspection_case` · `file_asset` · `document_parse_run` · `request_extraction` · `missing_check_run` · `missing_check_item` · `form_field_definition` · `inspection_report`(`report_json`) · `retrieval_run` · `retrieval_candidate` · `candidate_evidence` · `chat_session` · `chat_message` · `announcement`

FIT·SIM 수치는 `inspection_report.report_json` 안의 `structural_consistency` 와 `similar_candidates` 를 펼쳐서 셌다. 이 컬럼은 UPDATE 가 트리거로 차단된 불변 스냅샷이다.

입력 파일은 `backend/storage/` 에 있으며, 보고서가 있는 47건 중 **25건**의 원본 파일이 이 저장소에 남아 있다.
