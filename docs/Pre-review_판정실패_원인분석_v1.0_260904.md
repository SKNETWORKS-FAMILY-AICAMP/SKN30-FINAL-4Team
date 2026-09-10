# Pre-review 판정 실패 원인 분석

2026-09-04 · 코드 `backend/app/services/cpl/logic_validator.py`, `analysis_pipeline.py`, `hwp_parser.py`
데이터 팀 Supabase 실측 (보고서 47건 / CPL 항목 663건)

[과거 분석실행 기록](Pre-review_과거_분석실행_기록_v1.0_260904.md)에서 "코드를 봐야 알 수 있다"고 남긴 세 가지를 코드 경로까지 추적한 결과다. **결정적인 코드는 이 문서 안에 인용해 두었으므로 코드 없이도 읽을 수 있다.**

---

## 요약

| 질문 | 답 | 근거 |
|---|---|---|
| `PARTIAL_SUCCESS` 39건이 CPL 결과를 나쁘게 만드나 | **상태값 자체는 아무 분기도 만들지 않는다.** 다만 그 상태를 만든 원인(`segments` 분해)이 근거 귀속 실패를 통해 CPL 판정을 강등시킨다 | `EVIDENCE_OWNERSHIP_UNRESOLVED` 24건 중 **23건이 `PARTIAL_SUCCESS` 보고서** |
| `LLM_TIMEOUT` 41건이 왜 났나 | **HTTP 30초 타임아웃 10회.** 한 번의 호출이 여러 항목을 함께 처리해서 1회 실패가 4~5개 항목을 한꺼번에 강등시켰다 | 10개 케이스 × 4~5개 필드 = 41. 타임아웃 재시도 코드 없음 |
| `IMPLEMENTATION_PLAN` 이 51건 전부 `NEEDS_CONFIRMATION` 인 이유 | **관문이 세 개인데 셋 다 통과한 적이 없다.** 연차계획·내역사업 두 Rule 판정과 LLM 접지를 모두 통과해야 `PRESENT` 가 된다 | Rule 관문 실패 6건 + LLM 실패 45건 = 51 |

---

# 1. `PARTIAL_SUCCESS` 와 CPL 결과

## 1.1 상태값 자체는 아무것도 안 한다

`PARTIAL_SUCCESS` 를 읽는 곳은 저장소 전체에 **세 군데뿐**이고, 셋 다 `SUCCESS` 와 **똑같이 취급**한다.

```sql
-- analysis_pipeline.py:504, cpl/checker.py:288 — 파싱 결과를 불러오는 조건
AND r.status IN ('SUCCESS', 'PARTIAL_SUCCESS')
```

```python
# document_parsing.py:206 — 상태를 정하는 유일한 지점
terminal_status = "PARTIAL_SUCCESS" if parsed.partial else "SUCCESS"
```

**`partial` 을 보고 판정을 바꾸는 분기는 없다.** 두 상태 모두 그대로 CPL 로 넘어간다.

## 1.2 그런데 그 상태를 만든 원인은 판정에 영향을 준다

`partial=True` 가 되는 조건은 하나뿐이다 — 표 셀 하나에 인라인 런이 여러 개라 문단 경계를 확정하지 못했을 때.

```python
# hwp_parser.py — 셀 분해
if len(paragraphs) > 1: ...
if any(len(segments) > 1 for segments in inline_runs):
    result["segments"] = [{"segment_index": i, "text": v} for i, v in ...]
    result["structure_status"] = "unresolved"          # ← 표시
    warnings.append("TABLE_CELL_INLINE_SEGMENTS: ...")  # ← 경고
...
partial = bool(warnings)                                # ← PARTIAL_SUCCESS
```

이 `segments` 가 **CPL 근거 추출의 입력 단위를 바꾼다.**

```python
# logic_validator.py:878 — 셀을 조각으로 만드는 곳
segments = cell.get("segments")
if isinstance(segments, list) and len(segments) > 1:
    for segment_index, segment in enumerate(segments):
        fragments.append(_Fragment(
            evidence_ref=f"{cell_ref}:segment:{segment_index}",   # ← 조각마다 별도 근거 ID
            text=segment_text.strip(), ...
        ))
```

`segments` 가 없으면 **셀 하나 = 조각 하나**, 있으면 **세그먼트 하나 = 조각 하나**가 된다.

실측 — 근거 1,700건이 어느 단위에서 나왔나:

| 단위 | RULE | LLM | 계 |
|---|---|---|---|
| `cell` (셀 통째) | 677 | 231 | 908 |
| **`segment`** (조각) | 317 | 413 | **730** |
| `paragraph` | 35 | 27 | 62 |

`structure_status` 기준으로는 **1,700건 중 996건(59%)이 `unresolved` 셀에서 나온 근거**다.

## 1.3 조각으로 쪼개지면 "이 문장이 어느 항목 것인지"를 못 정할 수 있다

코드에 적힌 문제 정의:

```python
# logic_validator.py:975 주석
# Some HWPX cells flatten visual lines into inline segments.  A label-only
# segment owns the following unlabeled segments until the next label in the cell.
```

라벨(`◦(사업목적)` 같은 머리말)이 붙은 조각이 **다음 라벨이 나올 때까지 뒤따르는 조각을 소유한다**는 규칙이다. 이 소유권이 안 정해지면 근거가 버려진다.

```python
# logic_validator.py:759 — 접지 단계
owned_fields, cited_role = _evidence_context(fragment, occurrence.raw_text)
if owned_fields and item.field_code not in owned_fields:
    dropped.append("FIELD_NOT_ALLOWED"); continue
if not owned_fields:
    dropped.append("FIELD_OWNERSHIP_UNRESOLVED"); continue   # ← 소유권 미정
```

버려진 사유는 **책임 소재별로 분류**된다. 코드 주석이 이유를 명시한다:

```python
# logic_validator.py:36
# 근거를 잃은 원인을 구분한다. 파서가 라벨 여러 개를 한 조각에 담아 귀속하지
# 못한 것과, 모델이 원문에 없는 말을 인용한 것은 다른 사건이다. 둘을 같은
# 코드로 적으면 Rule·LLM 비교 지표가 우리 결함을 LLM 실패로 센다.

_LLM_FAULT_DROPS  = {"BLANK_EVIDENCE","REF_NOT_FOUND","NOT_VERBATIM",
                     "FIELD_NOT_ALLOWED","AXIS_NOT_ALLOWED"}
_STRUCTURAL_DROPS = {"FIELD_OWNERSHIP_UNRESOLVED"}       # ← 우리 파서 쪽 문제
_BENIGN_DROPS     = {"EXPLICIT_MISSING_VALUE","EXPLICIT_ABSENCE_VALUE"}
```

```python
# logic_validator.py:1397 _incomplete_reason() — 심각한 쪽이 이긴다
if contract_error in _LLM_FAULT_CONTRACT_ERRORS or reasons & _LLM_FAULT_DROPS:
    return "LLM_INVALID_RESPONSE"
if reasons & _STRUCTURAL_DROPS:
    return "EVIDENCE_OWNERSHIP_UNRESOLVED"               # ← 여기
```

그 결과 해당 항목은 강등된다:

```python
# logic_validator.py:629
for item in merged_items:
    if item.field_code in invalid_unresolved_fields:
        item.status = CplStatus.NEEDS_CONFIRMATION
        item.reason_code = invalid_unresolved_fields[item.field_code]
```

## 1.4 데이터가 이 경로를 확인한다

보고서 47건을 파싱 상태로 갈라 `EVIDENCE_OWNERSHIP_UNRESOLVED` 발생을 세면:

| 파싱 상태 | 보고서 | 소유권 실패가 있는 보고서 | 총 발생 |
|---|---|---|---|
| `PARTIAL_SUCCESS` | 35 | **23 (66%)** | **23** |
| `SUCCESS` | 12 | 1 (8%) | 1 |

**24건 중 23건이 `PARTIAL_SUCCESS` 쪽에서 나왔다.**

> `missing_check_item` 기준으로는 28건이다. 저 표는 `inspection_report` 47건 기준이라 보고서가 없는 4건이 빠졌다.

## 1.5 결론

```
표 셀에 인라인 런 여러 개
  → 파서가 segments 로 분해 + structure_status: "unresolved"
  → 경고 TABLE_CELL_INLINE_SEGMENTS → partial=True → PARTIAL_SUCCESS   ← 여기는 라벨일 뿐
  → CPL 이 세그먼트 단위로 조각을 만든다 (근거의 59%)
  → 라벨 소유권을 못 정한 조각의 근거가 버려진다 (FIELD_OWNERSHIP_UNRESOLVED)
  → EVIDENCE_OWNERSHIP_UNRESOLVED → 그 항목 NEEDS_CONFIRMATION           ← 여기가 영향
```

**`PARTIAL_SUCCESS` 라는 상태가 판정을 바꾸는 게 아니라, 그 상태를 만든 표 구조가 근거 귀속을 어렵게 만든다.** 앞 문서에서 "인과를 안 붙였다"고 한 부분의 실제 경로가 이것이다.

다만 CPL `confirmed_count` 평균 차이(SUCCESS 9.1 / PARTIAL_SUCCESS 8.3)는 여전히 **표본이 12 대 35이고 입력 문서도 달라** 이 경로만으로 설명되지 않는다.

---

# 2. `LLM_TIMEOUT` 41건

## 2.1 타임아웃은 30초, 재시도 없음

```python
# core/config.py:64
cpl_llm_timeout_seconds: int = Field(default=30, ge=1)
```

```python
# infrastructure/openai_llm_client.py:34
self._timeout = httpx.Timeout(timeout_seconds)
```

`httpx.Timeout(30)` 은 connect·read·write·pool 전부 30초다.

재시도 정책:

```python
# openai_llm_client.py:117
for attempt in range(2):
    response = await client.post(...)
    if response.status_code != 429 and response.status_code < 500:
        break
    if attempt == 0:
        await asyncio.sleep(0.25)
except httpx.TimeoutException:
    raise LLMTimeoutError("LLM request timed out") from None
```

**429 와 5xx 만 1회 재시도한다. 타임아웃은 재시도하지 않는다.** `except` 가 `for` 밖에 있어 첫 타임아웃에서 바로 예외가 된다.

## 2.2 한 번의 호출이 여러 항목을 함께 처리한다

```python
# analysis_pipeline.py:321
response = await llm_client.generate_structured(
    task_name="cpl_semantic_evidence",
    messages=_semantic_messages(document, semantic_fields, ...),  # ← 필드 집합을 한꺼번에
    response_schema=CplSemanticResponse, ...
)
```

호출 대상은 **Rule 이 확정하지 못한 항목만**이다:

```python
# analysis_pipeline.py:89
semantic_fields = {
    item.field_code for item in rule_result.items
    if item.field_code in CPL_SEMANTIC_FIELDS
    and item.status not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
}
```

타임아웃이 나면 **그 호출에 포함된 항목 전부**가 한꺼번에 강등된다:

```python
# analysis_pipeline.py:346
except LLMTimeoutError as error:
    result = merge_llm_result(rule_result, llm_error="LLM_TIMEOUT",
                              requested_fields=semantic_fields)
```

```python
# logic_validator.py:2461 _result_with_llm_failure()
for item in rule_result.items:
    if copied.field_code in degradable and copied.status not in {PRESENT, NOT_APPLICABLE}:
        copied.status = CplStatus.NEEDS_CONFIRMATION
        copied.reason_code = error_code          # ← "LLM_TIMEOUT"
```

## 2.3 데이터가 이 구조를 그대로 보여준다

`LLM_TIMEOUT` 이 기록된 케이스와 그때 강등된 필드:

| case_id | 필드 수 | 필드 |
|---|---|---|
| 7 | 4 | `BUSINESS_NEED`, `IMPLEMENTATION_PLAN`, `LINKED_POLICY`, `NEW_OR_CHANGED_CONTENT` |
| 9 | 4 | (동일) |
| 10 | 4 | (동일) |
| 14 | 4 | (동일) |
| 15 | 4 | (동일) |
| 18 | 4 | (동일) |
| **24** | **5** | 위 4개 + `TARGET_AND_CONDITIONS` |
| 28 | 4 | (동일) |
| 38 | 4 | (동일) |
| 44 | 4 | (동일) |

**41건 = 케이스 10건 × 필드 4~5개.** 항목이 41번 실패한 게 아니라 **호출이 10번 실패**한 것이다.

보고서 경고에서도 확인된다:

```
CPL semantic extraction incomplete: LLM_TIMEOUT     ← 10건 (보고서당 1개)
```

이 경고는 전체 호출 실패 경로에서만 나온다:

```python
# logic_validator.py:2471 (_result_with_llm_failure 안)
warning = f"CPL semantic extraction incomplete: {error_code}"
```

## 2.4 부수 사실 — 같은 케이스 4개 필드가 반복되는 이유

10건 모두 강등 필드가 같다. 이건 **그 10건에서 Rule 이 나머지 5개 의미 항목을 이미 `PRESENT`/`NOT_APPLICABLE` 로 확정했다**는 뜻이다. 2.2 의 `semantic_fields` 계산식이 그렇게 만든다.

케이스 id 가 7~44 로 전부 초기 구간(2026-08-31 ~ 09-02)이다. **DB 에는 실패 시각·응답 시간·재시도 여부가 남지 않는다.** 30초를 왜 넘겼는지는 기록이 없다 — `chat_message` 와 달리 CPL 호출은 토큰 수도 지연 시간도 저장하지 않는다.

---

# 3. `IMPLEMENTATION_PLAN` 이 51건 전부 `NEEDS_CONFIRMATION` 인 이유

## 3.1 이 항목만 관문이 셋이다

다른 12개 항목과 달리 `IMPLEMENTATION_PLAN` 은 **Rule 판정 두 개를 합성한 뒤 LLM 관문을 또 통과**해야 한다.

```python
# logic_validator.py:2364
def _resolve_implementation_plan(items):
    plan   = (IMPLEMENTATION_PLAN 항목)
    period = (BUSINESS_PERIOD 항목)
    annual_status     = _annual_plan_status(period, plan)      # 관문 A: 연차별 계획
    subprogram_status = _subprogram_plan_status(plan)          # 관문 B: 내역사업 계획
    statuses = {annual_status, subprogram_status}

    if MISSING in statuses:                  plan.status = MISSING
    elif NEEDS_CONFIRMATION in statuses:     plan.status = NEEDS_CONFIRMATION
    elif statuses == {NOT_APPLICABLE}:       plan.status = NOT_APPLICABLE
    else:                                    plan.status = PRESENT
```

**`PRESENT` 가 되려면 A·B 가 둘 다 `PRESENT` 이거나 한쪽이 `NOT_APPLICABLE` 이어야 한다.** 하나라도 `NEEDS_CONFIRMATION` 이면 전체가 `NEEDS_CONFIRMATION` 이다.

### 관문 A — 연차별 계획

```python
# logic_validator.py:2389
periods = [BUSINESS_PERIOD 의 normalized_value 들]
if not periods:                    return NEEDS_CONFIRMATION   # 사업기간을 못 뽑음
if any(single_year is True):       return NOT_APPLICABLE       # 단년도면 연차계획 불필요
multi_year = (continuing is True) or (start[:4] != end[:4])
if not multi_year:                 return NEEDS_CONFIRMATION   # 다년도인지 확정 못 함
return PRESENT if (축 ANNUAL_PLAN_CONTENT + 역할 ANNUAL_PLAN 근거 있음) else MISSING
```

`PRESENT` 조건이 까다롭다 — 근거에 **축이 `ANNUAL_PLAN_CONTENT` 이면서 동시에 `source_role` 이 `ANNUAL_PLAN`** 이어야 한다.

### 관문 B — 내역사업 계획

```python
# logic_validator.py:2421
if (축 PROGRAM_LEVEL_ABSENT 이고 program_level_absent=True):  return NOT_APPLICABLE
levels = {PROGRAM_LEVEL 축 근거의 program_level 값들}
has_content = (축 SUBPROGRAM_PLAN_CONTENT + 역할 SUBPROGRAM_PLAN 근거 있음)
if levels & {"SUBPROGRAM", "SUBSUBPROGRAM"}:
    return PRESENT if has_content else MISSING
return NEEDS_CONFIRMATION            # ← 사업 계층을 못 찾으면 여기
```

**사업 계층(`세부사업`/`내역사업`/`내내역사업`)을 문서에서 못 찾으면 무조건 `NEEDS_CONFIRMATION`** 이다.

### 관문 C — LLM

`IMPLEMENTATION_PLAN` 은 `CPL_SEMANTIC_FIELDS` 9개에 포함된다. 그래서 LLM 호출 대상이고, 실패하면 강등된다. 게다가 Rule 재계산 자체가 **LLM 이 성공했을 때만** 다시 돈다:

```python
# logic_validator.py:614
plan_candidate = candidate_by_code.get(IMPLEMENTATION_PLAN)
if (plan_candidate is not None
        and IMPLEMENTATION_PLAN not in invalid_fields
        and plan_candidate.reason_code not in CPL_INCOMPLETE_REASON_CODES):
    _resolve_implementation_plan(merged_items)      # ← LLM 이 온전할 때만 실행
```

LLM 이 실패하면 이 재계산을 건너뛰고, 바로 아래에서 `NEEDS_CONFIRMATION` 으로 덮인다.

## 3.2 51건이 어디서 걸렸나

DB 에 기록된 `IMPLEMENTATION_PLAN` 의 사유 코드 전수:

| `reason_code` | 건수 | 어느 관문 |
|---|---|---|
| `LLM_INVALID_RESPONSE` | **34** | C — 근거 접지 실패 |
| `LLM_TIMEOUT` | **10** | C — 호출 타임아웃 (2절의 그 10건) |
| `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` | **6** | A 또는 B — Rule 이 `NEEDS_CONFIRMATION` 반환 |
| `EVIDENCE_OWNERSHIP_UNRESOLVED` | **1** | C — 조각 소유권 미정 (1절) |
| | **51** | |

**Rule 관문까지 도달한 건 6건뿐이고, 그 6건도 A 또는 B 에서 막혔다.** 나머지 45건은 LLM 관문에서 끝나 Rule 재계산이 실행되지도 않았다.

`IMPLEMENTATION_PLAN_REVIEW_REQUIRED` 라는 코드는 `_resolve_implementation_plan()` 안의 이 한 줄에서만 나온다:

```python
elif CplStatus.NEEDS_CONFIRMATION in statuses:
    plan.status = CplStatus.NEEDS_CONFIRMATION
    plan.reason_code = "IMPLEMENTATION_PLAN_REVIEW_REQUIRED"
```

## 3.3 왜 안정적으로 보였나

앞 문서에서 `IMPLEMENTATION_PLAN` 은 "같은 파일에서 판정이 한 번도 안 갈린 5개 항목" 중 하나로 나왔다. **그건 결정적이어서가 아니라 항상 같은 값(`NEEDS_CONFIRMATION`)이었기 때문**이다. 사유 코드는 `LLM_INVALID_RESPONSE` / `LLM_TIMEOUT` / `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` 로 흔들렸다.

나머지 4개(`BUDGET` `BUSINESS_PERIOD` `LEGAL_BASIS` `REQUEST_TYPE`)는 Rule 전용이라 진짜로 결정적이다. **이 항목만 성격이 다르다.**

---

# 4. 부록 — CPL 사유 코드 발생 지점 지도

각 코드가 어디서 나오는지, 누구 책임인지.

| `reason_code` | 발생 위치 | 원인 | 실측 |
|---|---|---|---|
| `LLM_INVALID_RESPONSE` | `_incomplete_reason()` 또는 파이프라인 예외 | 모델이 원문에 없는 말을 인용(`NOT_VERBATIM`), 없는 근거 ID 참조(`REF_NOT_FOUND`), 빈 근거(`BLANK_EVIDENCE`), 허용 안 된 필드·축(`FIELD_NOT_ALLOWED`/`AXIS_NOT_ALLOWED`), 또는 응답 형식 오류 | 54 |
| `LLM_TIMEOUT` | `_result_with_llm_failure()` | HTTP 30초 초과. 호출 단위로 전파 | 41 |
| `VALUE_NOT_SPECIFIC` | `logic_validator.py:1913` | 값이 있으나 구체적이지 않음 | 30 |
| `REQUEST_TYPE_AMBIGUOUS` | `_request_type_item()` :1229 | 선택된 체크박스가 **정확히 1개가 아님** (0개이거나 2개 이상) | 30 |
| `EVIDENCE_OWNERSHIP_UNRESOLVED` | `_incomplete_reason()` :1411 | **파서 쪽 문제.** 조각의 라벨 소유권 미정 | 28 |
| `EVIDENCE_CONFLICT` | `merge_llm_result()` :605 | Rule 은 근거를 찾았는데 LLM 은 `MISSING` 이라고 답함 | 10 |
| `LLM_REASON_CODE_MISSING` | `_incomplete_reason()` :1413 | LLM 이 `NEEDS_CONFIRMATION` 을 주면서 사유를 안 씀 | 6 |
| `IMPLEMENTATION_PLAN_REVIEW_REQUIRED` | `_resolve_implementation_plan()` :2380 | 연차/내역 관문 중 하나가 `NEEDS_CONFIRMATION` | 6 |
| `EXPLICIT_VALUE_NOT_FOUND` | `logic_validator.py:1909` | 명시적 값을 찾지 못함 | 5 |
| `REQUEST_TYPE_NOT_FOUND` | `_request_type_item()` :1226 | 체크박스 패턴 자체를 못 찾음 | 1 |

## 책임 분류

코드가 명시적으로 나눠 둔 것이다.

| 분류 | 코드 | 뜻 |
|---|---|---|
| `_LLM_FAULT_DROPS` | `BLANK_EVIDENCE` `REF_NOT_FOUND` `NOT_VERBATIM` `FIELD_NOT_ALLOWED` `AXIS_NOT_ALLOWED` | 모델 잘못 |
| `_STRUCTURAL_DROPS` | `FIELD_OWNERSHIP_UNRESOLVED` | **우리 파서 잘못** |
| `_BENIGN_DROPS` | `EXPLICIT_MISSING_VALUE` `EXPLICIT_ABSENCE_VALUE` | 어느 쪽 잘못도 아님 (문서가 스스로 공란이라고 밝힘) |

## 전체 호출 실패 vs 항목별 실패 구분법

보고서 경고 문자열로 구분된다.

| 경고 | 나오는 곳 | 뜻 | 실측 |
|---|---|---|---|
| `CPL semantic extraction incomplete: LLM_TIMEOUT` | `:2471` | **호출 전체 실패** | 10 |
| `CPL semantic extraction incomplete: LLM_INVALID_RESPONSE` | `:651` | 항목별 접지 실패 | 38 |
| `CPL semantic extraction incomplete: EVIDENCE_OWNERSHIP_UNRESOLVED` | `:651` | 항목별 소유권 실패 | 24 |
| `CPL semantic extraction incomplete: LLM_REASON_CODE_MISSING` | `:651` | 항목별 사유 누락 | 2 |
| `CPL semantic field incomplete: {필드}:LLM_INVALID_RESPONSE` | `:639` | 응답 자체가 계약 위반 | **0** |

`LLM_INVALID_RESPONSE` 54건은 **35개 케이스에 필드 1~2개씩 흩어져 있다.** 호출 전체가 죽은 게 아니라 개별 근거가 접지에 실패한 것이다. 계약 위반 경로(`:639`)는 한 번도 발생하지 않았다.

---

# 5. 이 문서의 수치를 다시 뽑는 법

DB 만 있으면 코드 없이 재확인할 수 있다.

**파싱 상태 × 소유권 실패**
```sql
WITH p AS (SELECT r.inspection_case_id cid, pr.status ps
           FROM sims.inspection_report r
           JOIN sims.file_asset f ON f.inspection_case_id=r.inspection_case_id
                                 AND f.extension IN ('hwp','hwpx')
           JOIN sims.document_parse_run pr ON pr.file_asset_id=f.id),
     e AS (SELECT r.inspection_case_id cid,
                  count(*) FILTER (WHERE i->>'reason_code'='EVIDENCE_OWNERSHIP_UNRESOLVED') n
           FROM sims.inspection_report r,
                LATERAL jsonb_array_elements(r.report_json->'self_check'->'items') i
           GROUP BY 1)
SELECT p.ps, count(*), count(*) FILTER (WHERE e.n>0), sum(e.n)
FROM p JOIN e ON e.cid=p.cid GROUP BY 1;
```

**타임아웃이 한 번에 몇 개 항목을 죽였나**
```sql
SELECT r.inspection_case_id, count(*),
       string_agg(i->>'field_code', ', ' ORDER BY i->>'field_code')
FROM sims.inspection_report r,
     LATERAL jsonb_array_elements(r.report_json->'self_check'->'items') i
WHERE i->>'reason_code'='LLM_TIMEOUT'
GROUP BY 1 ORDER BY 1;
```

**항목별 사유 코드 분포**
```sql
SELECT i->>'field_code', i->>'reason_code', count(*)
FROM sims.inspection_report r,
     LATERAL jsonb_array_elements(r.report_json->'self_check'->'items') i
WHERE i->>'reason_code' IS NOT NULL
GROUP BY 1,2 ORDER BY 3 DESC;
```

**근거가 세그먼트 단위인지 셀 단위인지**
```sql
SELECT CASE WHEN o->'source_locator'->'table_cell' ? 'segment_index' THEN 'segment'
            WHEN o->'source_locator' ? 'table_cell'                  THEN 'cell'
            ELSE 'paragraph' END,
       o->>'extraction_method', count(*)
FROM sims.inspection_report r,
     LATERAL jsonb_array_elements(r.report_json->'self_check'->'items') i,
     LATERAL jsonb_array_elements(i->'occurrences') o
GROUP BY 1,2 ORDER BY 3 DESC;
```

**호출 실패인지 항목 실패인지**
```sql
SELECT w, count(*)
FROM sims.inspection_report r,
     LATERAL jsonb_array_elements_text(r.report_json->'self_check'->'warnings') w
WHERE w LIKE 'CPL semantic%' GROUP BY 1 ORDER BY 2 DESC;
```

---

# 6. 기록이 없어 확인 못 한 것

데이터로도 코드로도 답이 안 나오는 부분이다. 추측하지 않고 그대로 남긴다.

- **LLM 호출의 실제 지연 시간.** CPL·FIT·SIM 호출은 응답 시간·토큰 수를 DB 에 저장하지 않는다. `chat_message` 에만 `input_tokens`/`output_tokens` 컬럼이 있고 그것도 전부 `NULL` 이다. 30초를 왜 넘겼는지는 남은 기록이 없다.
- **`LLM_INVALID_RESPONSE` 54건의 세부 사유.** 코드는 `dropped_detail` 에 `NOT_VERBATIM:{축}@{근거ID}` 형태로 상세를 만들지만 **로그로만 남기고 DB 에는 저장하지 않는다** (`logic_validator.py:833`). 어떤 축이 왜 거부됐는지는 그때의 서버 로그가 있어야 안다.
- **`SUCCESS`/`PARTIAL_SUCCESS` 간 `confirmed_count` 차이(9.1 대 8.3)의 원인.** 표본이 12 대 35 이고 입력 문서 구성도 다르다. 1절의 경로가 기여했다는 것까지는 확인되지만, 이 숫자 차이 전체를 설명하지는 못한다.
