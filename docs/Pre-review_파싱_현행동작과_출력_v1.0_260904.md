# Pre-review 파싱 현행 동작과 출력

2026-09-04 · 코드는 `backend/app/parsers/hwp_parser.py` · 수치는 팀 Supabase 실측 51건

파싱 전략과 출력 형식을 바꾸기 전의 **기준선**이다. 코드에서 읽은 동작과 DB에 남은 실제 값만 적는다.

---

# 1. 파이프라인

```
업로드 (POST /cases)
  ├ 확장자·MIME 확인          case_upload.py
  └ 디스크 저장               backend/storage/users/{uid}/cases/{cid}/{uuid}.hwp

파싱                          document_parsing.py → hwp_parser.py
  ├ 1) 안전 사전검사          _preflight_source()
  ├ 2) rhwp.Document.from_bytes()
  ├ 3) document.to_ir()       → IR body 블록 순회
  ├ 4) 블록 변환              paragraph / table 두 종류만
  ├ 5) 정본 텍스트 재조립     _canonical_text()
  └ DB 저장                   document_parse_run

CPL 판정                      cpl/logic_validator.py
  ├ semantic_fragments()      블록 → 조각
  ├ Rule 4항목                evaluate_cpl_rules()
  ├ LLM 9항목                 merge_llm_result() + ground_llm_response()
  └ DB 저장                   request_field_value, missing_check_item

FIT → SIM → 보고서            fit_engine.py, sim_engine.py, reporting.py
  └ inspection_report.report_json (불변)
```

**파서는 `rhwp-python` 0.8.1 하나뿐이다.** `pdf_parser.py` 는 0바이트 빈 파일이다.

---

# 2. 무엇을 받고 무엇을 거부하는가

## 지원 형식

[hwp_parser.py:54](../backend/app/parsers/hwp_parser.py:54) `supports()` — **확장자와 MIME 이 둘 다 맞아야** 한다.

| 확장자 | MIME |
|---|---|
| `hwp` | `application/x-hwp` |
| `hwpx` | `application/hwp+zip` |

PDF 는 DB 제약(`declared_format`)에는 있지만 파서가 거부한다.

## 사전 검사 (`_preflight_source`)

**HWP** — OLE 복합문서로 열어 `FileHeader` 40바이트를 읽고 보호 플래그를 본다.

```python
HWP_PROTECTED_FLAGS = (1<<1) | (1<<2) | (1<<4) | (1<<8) | (1<<10)
```
플래그가 걸려 있으면 `Protected HWP documents are not supported`.

**HWPX** — ZIP 폭탄·경로 탈출 방어.

| 검사 | 한계값 |
|---|---|
| 엔트리 수 | 10,000개 |
| 압축 해제 총 크기 | 200 MB |
| 압축률 | 1,000배 |
| 절대경로·`..`·중복 이름 | 거부 |
| 암호화 엔트리(`flag_bits & 0x1`) | 거부 |
| 심볼릭 링크 | 거부 |

**실측**: 51건 중 이 단계에서 거부된 기록은 **0건**이다.

---

# 3. 파싱 결과의 구조

## 블록 계약

IR `body` 를 순회하며 **두 종류만** 만든다. 나머지는 버리고 경고를 남긴다.

| IR 블록 타입 | 결과 `block_type` |
|---|---|
| `ParagraphBlock`, `ListItemBlock` | `paragraph` |
| `TableBlock` | `table` |
| 그 외 | **버림** + `Unsupported {type} skipped` 경고 |

각 블록:

```json
{
  "block_id": "body:4",
  "block_type": "table",
  "text": "…",
  "section_path": ["section:0"],
  "source_locator": { … }
}
```

`block_id` 는 `body:{순번}` 이다. **이게 모든 근거 인용의 좌표계**다.

## `source_locator` 공통 키

`rhwp` 의 `block.prov` 에서 가져온다 ([hwp_parser.py:196](../backend/app/parsers/hwp_parser.py:196)).

| 키 | 뜻 |
|---|---|
| `body_block_index` | 블록 순번 |
| `section_index` | 구역 번호 |
| `paragraph_index` | 문단 번호 |
| `char_start`, `char_end` | 문자 오프셋 |
| `page_range` | 페이지 범위 (**실측 데이터에는 나타나지 않는다**) |

## 표 블록의 추가 키

| 키 | 뜻 |
|---|---|
| `rows`, `cols` | 표 크기 |
| `cells` | 셀 배열 |

셀 하나:

```json
{
  "row": 2, "col": 1,
  "row_span": 1, "col_span": 7,
  "grid_index": 17,
  "role": "data",
  "text": "◦(사업목적) ICT혁신기업이 …",
  "paragraphs": ["…", "…"],
  "segments": [{"segment_index": 0, "text": "…"}, …],
  "structure_status": "unresolved"
}
```

`role` 은 `data` / `column_header` / `layout` 이 관측됐다.

## `segments` 와 `unresolved` — 경고 350건의 정체

정상적인 셀은 문단마다 `ParagraphBlock` 이 하나씩 나온다. 그런데 일부 문서는 **한 `ParagraphBlock` 안에 여러 인라인 런**이 들어 있다.

파서는 그 런을 **문단 경계로 간주하지 않고** 원형 그대로 `segments` 에 순서대로 보존하고 `structure_status: "unresolved"` 를 붙인다 ([hwp_parser.py:216](../backend/app/parsers/hwp_parser.py:216)). 그리고 경고를 남긴다:

```
TABLE_CELL_INLINE_SEGMENTS: body:4:cell:2:1 preserved as segments;
paragraph boundaries are not inferred
```

**이 경고가 하나라도 있으면 `partial=True` → `document_parse_run.status = PARTIAL_SUCCESS`.**

## 정본 텍스트 재조립 (`_canonical_text`)

`rhwp` 의 `extract_text()` 는 **표 내용을 빠뜨린다.** 코드 주석에 명시돼 있다:

> rhwp's plain-text view can omit table content (the mockup samples do this), so the persisted extraction must be rebuilt from the structured blocks.

그래서 `extracted_text` 는 `extract_text()` 결과가 아니라 **블록에서 다시 조립한 것**이다:

- paragraph → `text` 그대로
- table → 셀을 순회하며, `segments` 를 이어붙인 게 `cell.text` 와 정확히 같을 때만 segment 를 줄바꿈으로 잇고, 아니면 `cell.text` 사용
- 최종적으로 전부 `\n` 으로 연결

## 실패 조건

| 조건 | 예외 |
|---|---|
| 지원 블록 0개 | `Document contains no supported text blocks` |
| 조립 텍스트가 공백뿐 | `Document contains no extractable text` |

**실측**: 51건 중 `document_parse_run.status = FAILED` 는 **0건**이다.

---

# 4. 실제로 무엇이 파싱됐나 (51건 실측)

## 블록 분포

| `block_type` | 블록 수 | 등장 파싱 건수 |
|---|---|---|
| `paragraph` | 2,753 | 51 |
| `table` | 230 | 50 |

건당 블록 수: 최소 5, 평균 58.5, 최대 93.

## 텍스트는 표에서 나온다

| `block_type` | 총 문자 수 | 빈 블록 |
|---|---|---|
| `table` | **89,260 (62%)** | 0 / 230 (0%) |
| `paragraph` | 55,452 (38%) | **1,408 / 2,753 (51.1%)** |

**paragraph 블록의 절반이 빈 문자열이고, 내용의 3분의 2가 표 안에 있다.**

## 경고

| 경고 종류 | 발생 | 영향 받은 파싱 건 |
|---|---|---|
| `TABLE_CELL_INLINE_SEGMENTS` | 350 | **39 / 51** |
| `Unsupported … skipped` | 0 | 0 |

`PARTIAL_SUCCESS` 39건은 전부 이 경고 하나 때문이다.

| 파싱 상태 | 건수 | 추출 텍스트 길이 |
|---|---|---|
| `PARTIAL_SUCCESS` | 39 (76%) | 667 ~ 4,040자 |
| `SUCCESS` | 12 (24%) | 777 ~ 2,133자 |

## 요청서의 실제 모양

`case 2121` (`사전협의서_예시.hwp`, 93블록) 앞부분:

```
body:0  paragraph  ''
body:1  paragraph  ''
body:2  paragraph  '< ㅇㅇ부 >'
body:3  paragraph  ''
body:4  table      ← 요청서 본문 전체가 이 표 하나에 들어 있다 (10행 × 8열)
body:5  paragraph  ''
body:6  paragraph  ''
body:7  paragraph  '첨부자료 : ...'
```

`body:4` 표의 셀:

| row | col | role | status | text |
|---|---|---|---|---|
| 0 | 0 | data | | `사전협의 요청사유` |
| 0 | 1 | data | | ` 세부사업 신설   □ 내역사업 신설 □ 내내역사업 신설\n□ 사업내용 변경…` |
| 1 | 0 | column_header | | `사업명` |
| 1 | 1 | column_header | | `ICT지원사업` |
| 2 | 0 | data | | `신설·변경 필요성` |
| 2 | 1 | data | **unresolved** | `◦(사업목적) ICT혁신기업이 …` |
| 3 | 0 | data | | `신설·변경 주요내용` |
| 3 | 1 | data | **unresolved** | `◦(사업기간) …◦(사업예산) …◦(지원대상) …` |
| 4 | 0 | data | | `기대효과` |
| 4 | 1 | data | **unresolved** | `◦(파급효과) …` |
| 5 | 0 | data | | `타 제도 협의·심사여부(해당 시)` |

**구조가 `col=0` 이 항목명, `col=1` 이 내용인 2열 표다.** 표 크기 분포에서도 `19×7`, `10×8`, `4×3`, `3×2` 가 각 30건씩 반복된다 — 같은 서식이 반복 입력됐다는 뜻이다.

---

# 5. 파싱 결과가 CPL 로 넘어가는 방식

13개 항목이 **두 갈래**로 갈린다 ([logic_validator.py:344](../backend/app/services/cpl/logic_validator.py:344)).

| | 항목 | 처리 |
|---|---|---|
| **Rule 전용 4개** | `REQUEST_TYPE` `BUSINESS_PERIOD` `LEGAL_BASIS` `BUDGET` | 정규식·문자 집합으로 판정 |
| **Rule + LLM 9개** (`CPL_SEMANTIC_FIELDS`) | `PURPOSE_GOAL` `IMPLEMENTATION_PLAN` `NEW_OR_CHANGED_CONTENT` `BUSINESS_NEED` `LINKED_POLICY` `TARGET_AND_CONDITIONS` `SUPPORT_CONTENT_AND_SCALE` `DELIVERY_SYSTEM` `EXPECTED_EFFECTS_AND_PERFORMANCE` | LLM 이 판정하고 Rule 이 원문에 접지 |

**근거 1,700건의 출처:**

| `extraction_method` | 버전 | 건수 |
|---|---|---|
| `RULE` | `cpl-alpha-v0.3` | 1,029 (61%) |
| `LLM` | `cpl-semantic-v0.9` | 671 (39%) |

이 구분이 앞서 측정한 재현성과 일치한다 — 같은 파일을 반복 실행했을 때 판정이 **한 번도 안 갈린 항목**은 `BUDGET` `BUSINESS_PERIOD` `LEGAL_BASIS` `REQUEST_TYPE`, 즉 Rule 전용 4개다.

## 체크박스 처리 — `REQUEST_TYPE_AMBIGUOUS` 30건

코드가 "선택됨"으로 인정하는 문자 ([logic_validator.py:178](../backend/app/services/cpl/logic_validator.py:178)):

```python
_SELECTED_MARKS = frozenset({"■", "☑", "▣", "✓", "✔"})
```

표 셀 원문에 실제로 나타난 체크 문자 (51건 전수):

| 문자 | 코드포인트 | 등장 | `_SELECTED_MARKS` 포함 |
|---|---|---|---|
| `□` | U+25A1 | 122 | 아니오 (미선택 박스) |
| **``** | **U+F0FE** | **30** | **아니오** |
| `☐` | U+2610 | 27 | 아니오 (미선택 박스) |
| `■` | U+25A0 | 11 | 예 |
| `☑` | U+2611 | 9 | 예 |

`U+F0FE` 는 유니코드 **사설 사용 영역**이다. 폰트에 따라 체크된 상자로 보이지만 표준 문자가 아니다.

DB 에 기록된 판정 결과:

| `mark` | `selected` | `request_reason` | 건수 |
|---|---|---|---|
| `□` | false | `CONTENT_CHANGE` | 37 |
| `□` | false | `SUBSUBPROGRAM_NEW` | 36 |
| `□` | false | `SUBPROGRAM_NEW` | 33 |
| `☐` | false | (3종) | 26 |
| `☑` | **true** | `DETAIL_NEW` / `CONTENT_CHANGE` | 9 |
| `■` | **true** | `DETAIL_NEW` / `SUBPROGRAM_NEW` | 11 |
| `□` | false | `DETAIL_NEW` | 4 |

**`U+F0FE` 는 `mark` 로 한 번도 기록되지 않았다.** 그리고 `U+F0FE` 등장 횟수(30)와 `REQUEST_TYPE_AMBIGUOUS` 건수(30)가 같다.

> 이 문서 앞 절에 적힌 API 명세의 서술과 일치한다 — *"`display`(요청 유형 체크박스)는 없앴다. 문서의 체크 표시를 정확히 읽지 못해 늘 미선택으로 나왔다."*

---

# 6. 최종 리포트 출력

`inspection_report.report_json` · 스키마 `alpha-report-v0.1` · **UPDATE 트리거로 차단된 불변 행**
실측 크기: case 3019 기준 **133,060자**.

## 최상위 10개 키

```
{
  case                     { case_id, title, created_at, completed_at }
  schema_version           "alpha-report-v0.1"
  ui_status                "COMPLETED"
  self_check               ← CPL
  structural_consistency   ← FIT
  similar_candidates[5]    ← SIM
  review_issues[9]         통합 이슈 목록
  ben_references[0]        항상 빈 배열
  differences[0]           항상 빈 배열
  warnings[13]             파싱 경고가 그대로 올라온다
}
```

## `self_check` (CPL)

```
self_check
├ items[13]
├ total_count       13
├ confirmed_count   9
├ confirmation_rate 69.23076923076923   ← API 로 안 나감
├ ruleset_version   "cpl-alpha-v0.3"
├ prompt_version    "cpl-semantic-v0.9"
├ model_profile     "gpt-4o-mini"
└ warnings[11]
```

**항목 하나 (PRESENT):**

```json
{
  "field_code": "PURPOSE_GOAL",
  "status": "PRESENT",
  "reason_code": null,
  "explanation": null,
  "occurrences": [ { …근거… }, … ]
}
```

**근거(occurrence) 하나 — 여기가 가장 두껍다:**

```json
{
  "excerpt": "ICT혁신기업이 신시장 창출 동력 확보를 위한 …",
  "field_code": "PURPOSE_GOAL",
  "axis_code": "PURPOSE_PROBLEM_DOMAIN",
  "evidence_ref": "request:PURPOSE_GOAL:0",
  "source_id": "case:3019",
  "source_side": "REQUEST",
  "source_role": null,
  "block_id": "body:4",
  "page_no": null,
  "section_path": ["section:0"],
  "source_locator": {
    "body_block_index": 4, "section_index": 0, "paragraph_index": 3,
    "rows": 10, "cols": 8,
    "line_index": 0, "span_start": 8, "span_end": 86,
    "table_cell": {
      "row": 2, "col": 1, "role": "data",
      "row_span": 1, "col_span": 7, "grid_index": 17,
      "segment_index": 12,
      "structure_status": "unresolved"
    }
  },
  "normalized_value": { "text": "…" },
  "extraction_method": "RULE",
  "extraction_version": "cpl-alpha-v0.3"
}
```

**파싱 결과가 여기까지 그대로 따라온다.** `block_id` · `table_cell.row/col/grid_index` · `segment_index` · `structure_status` 가 전부 파서가 만든 값이다.

`REQUEST_TYPE` 은 `normalized_value` 모양이 다르다:

```json
{ "mark": "□", "selected": false, "request_reason": "SUBPROGRAM_NEW" }
```

## `structural_consistency` (FIT)

```
structural_consistency
├ module_status  "AVAILABLE"
├ score { value: 83.33, numerator: 250, denominator: 300,
│         assessable_count: 3, total_count: 7,
│         scoring_version: "fit-alpha-v0.2" }     ← value/numerator/denominator 는 API 로 안 나감
├ relations[7]
└ warnings[1]
```

**관계 하나:**

```json
{
  "relation_id": "FIT-1",
  "status": "NEEDS_REVIEW",
  "score": 50,
  "summary": "목표 조건인 '…'와 실제 타겟 그룹인 '…' 간의 관계가 명확하지 않음. …",
  "reason_code": "null",
  "rule_version": "fit-v0.3",
  "prompt_version": "fit-v0.5",
  "left_evidence": [ {…occurrence…} ],
  "right_evidence": [ {…occurrence…} ]
}
```

근거의 모양은 CPL 과 같고 `evidence_ref` 만 `fit:FIT-1:left:0` 형식이다. `extraction_method` 는 `LLM`, `extraction_version` 은 `fit-v0.5`.

> `reason_code` 에 문자열 `"null"` 이 들어간 행이 있다. `null` 값이 아니라 4글자 문자열이다.

## `similar_candidates` (SIM)

**후보 하나:**

```json
{
  "rank": 1,
  "title": "2026년 ICT 미래시장 선점 R&D 지원사업 공고",
  "source_url": "https://www.bizinfo.go.kr/mock/MOCK-ICT-0001",
  "announcement_id": "MOCK-ICT-0001",
  "announcement_version_id": 1,
  "comparison_summary": "일부 비교축을 완료하지 못했습니다.",
  "semantic_similarity": 0.817202998601319,
  "semantic_similarity_display": 82,
  "weighted_score": null,
  "review_grade": "ON_HOLD",
  "assessable_axis_count": 3,
  "axes": { "purpose": {…}, "target": {…}, "content": {…}, "delivery": {…} },
  "ruleset_version": "sim-v0.2",
  "prompt_version": "sim-v0.3",
  "scoring_version": "sim-alpha-v0.2",
  "model_profile": "gpt-4o-mini",
  "warnings": [ … ]
}
```

`rank` · `semantic_similarity` · `weighted_score` · `review_grade` 는 **API 로 안 나간다.**

**축 하나 (성공):**

```json
{
  "axis_id": "SIM-1",
  "status": "SIMILAR",
  "score": 100,
  "summary": "ICT 혁신 기업의 신시장 창출을 위한 …",
  "common_points": ["…", "…"],
  "differences": [],
  "reason_code": null,
  "request_evidence": [ {…} ],
  "candidate_evidence": [ {…} ]
}
```

**축 하나 (실패):**

```json
{
  "axis_id": "SIM-3",
  "status": "INSUFFICIENT",
  "score": null,
  "summary": "해당 비교축의 의미 분석 응답을 검증하지 못했습니다.",
  "common_points": [],
  "differences": [],
  "reason_code": "LLM_INVALID_RESPONSE",
  "request_evidence": [ {…근거는 있다…} ]
}
```

**근거는 뽑혔는데 비교 판정만 실패한 형태다.** `content` 축(`SIM-3`)은 230건 전부 이 상태다.

---

# 7. API 로 나가는 것과 잘리는 것

[reporting.py:443](../backend/app/services/reporting.py:443) `_report_response()` 가 깎는다.

| `report_json` | API `GET /cases/{id}` |
|---|---|
| `self_check` | → `report.cpl` |
| `structural_consistency` | → `report.fit` |
| `similar_candidates` | → `report.similar_candidates` |
| `review_issues` `ben_references` `differences` `warnings` `ui_status` `schema_version` | **잘림** |

항목 단위로도 잘린다:

| 필드 | API |
|---|---|
| CPL `reason_code` `explanation` | **잘림** (DB 에는 있다) |
| CPL `status = PARSE_FAILED` 항목 | **목록에서 제외** ([reporting.py:458](../backend/app/services/reporting.py:458)) |
| CPL `evidence` | `MISSING` · `NEEDS_CONFIRMATION` 일 때만 |
| CPL `confirmation_rate` | 잘림 |
| FIT `score.value/numerator/denominator` | 잘림 (`assessable_count` 만) |
| SIM `rank` `semantic_similarity` `weighted_score` `review_grade` | 잘림 |
| 근거 | `excerpt` 하나만 남고 `block_id`·`source_locator`·`normalized_value`·`extraction_method` 전부 잘림 |

근거는 `_display_evidence()` 가 중복을 합쳐 **최대 1개**로 줄인다.

---

# 8. 파싱을 바꾸면 함께 움직이는 것

파싱 출력 계약(`blocks[]`, `source_locator`)에 직접 의존하는 지점이다.

| 의존 지점 | 무엇을 쓰나 |
|---|---|
| `document_parse_run.extracted_text` | `_canonical_text()` 결과 |
| `document_parse_run.structured_content` | `{blocks, warnings, partial}` 통째 |
| `logic_validator.semantic_fragments()` | `block.text`, `source_locator.cells`, `segments` |
| `logic_validator._cell_evidence_ref()` | `table_cell.row/col/grid_index` |
| CPL 근거 `evidence_ref` | `block_id` 기반 |
| FIT `left/right_evidence` | 같은 occurrence 구조 |
| SIM `request_evidence` | 같은 occurrence 구조 |
| `inspection_embedding.input_text` | 축별로 조립된 텍스트 |
| PDF 렌더러 | `report_json` 의 근거 `excerpt` |
| `report_json.warnings` | 파서 경고가 그대로 올라간다 |
| `document_parse_run.status` | `partial` → `PARTIAL_SUCCESS` |

**`block_id` 와 `table_cell` 좌표가 근거 접지의 유일한 기준이다.** 여기가 바뀌면 CPL·FIT·SIM 세 곳의 근거가 전부 영향을 받는다.

---

# 9. 기준선 수치 요약

파싱 전략을 바꾼 뒤 이 표와 비교한다.

| 지표 | 현행 (51건) |
|---|---|
| 파서 | `rhwp-python` 0.8.1 |
| `SUCCESS` / `PARTIAL_SUCCESS` / `FAILED` | 12 / 39 / 0 |
| `TABLE_CELL_INLINE_SEGMENTS` 경고 | 350건, 39/51 파싱 |
| 블록 수 (건당) | 최소 5 / 평균 58.5 / 최대 93 |
| `paragraph` 블록 | 2,753개 (빈 블록 51.1%) |
| `table` 블록 | 230개 (빈 블록 0%) |
| 텍스트 비중 | table 62% / paragraph 38% |
| 추출 텍스트 길이 | 667 ~ 4,040자 |
| 근거 출처 | RULE 1,029 / LLM 671 |
| `page_range` 기록 | 0건 |
| 표 셀 `role` | `data`, `column_header`, `layout` |
| `U+F0FE` 등장 | 30회 (미인식) |
| CPL `confirmed_count` | 3 ~ 12 |
| FIT `assessable_count` | 0 ~ 5 (7 중) |
| SIM 축 `INSUFFICIENT` | `content` 230/230, `delivery` 12, `target` 11, `purpose` 5 |
| 보고서 크기 | 약 133,000자 |
