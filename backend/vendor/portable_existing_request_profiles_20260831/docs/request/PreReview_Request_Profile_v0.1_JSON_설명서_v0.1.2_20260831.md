# PreReview Request Profile v0.1 — JSON 설명서 v0.1.2

- 작성일: 2026-08-31
- 대상 schema: `pre_review_request_profile/v0.1`
- 기준 계약: `PreReview_Request_Profile_Structured_JSON_Contract_v0.1.2_20260830.md`
- 동반 fixture: `pre_review_request_profile_v0.1_contract_fixture_v0.1.2_20260831.md`
- 대체 대상: `PreReview_Request_Profile_v0.1_JSON_설명서_20260829.md`
- 상태: **Request Pipeline 구현 handoff용 최신 설명서**

---

## 0. 문서 사용법

Request Pipeline 구현 시 문서 우선순위는 다음과 같다.

```text
1. Request Structured JSON Contract v0.1.2
   → canonical 계약

2. 본 설명서
   → 구현 관점 해설 / 작업순서 / 책임경계

3. contract fixture v0.1.2
   → 객체 shape와 provenance 예시
```

충돌 시 항상 **Request Structured JSON Contract v0.1.2**를 우선한다.

20260829의 기존 fixture/설명서는 legacy 자료로 본다.

---

## 1. 현재 구현 기준선

Request Pipeline의 입력·출력은 다음으로 고정한다.

```text
사전협의 요청서 HWP/HWPX
        ↓
Common IR v1
        ↓
CandidatePack
        ↓
Request semantic structuring
        ↓
exact-span / evidence 복원
        ↓
Derived Projection
        ↓
Contract Validation
        ↓
pre_review_request_profile/v0.1
```

Request 전용 Common IR dialect는 만들지 않는다.

---

## 2. Existing Pipeline과의 관계

Request 구현 담당자는 Existing Program Profile v0.2 Pipeline도 구현한 담당자이므로,
**Existing을 reference implementation으로 사용한다.**

새로 복제하지 말고 가능한 한 다음 공통 infrastructure를 재사용한다.

```text
Common IR v1 reader
CandidatePack builder
exact-span resolver
evidence/provenance builder
Common IR block/cell/occurrence lookup
numeric candidate extractor
source_numeric_candidate_id
lineage builder
공통 validator
```

Request에서 새로 필요한 것은 주로 **의미 추출 계약**이다.

```text
공통 infrastructure
        │
   ┌────┴─────┐
   ↓          ↓
Existing    Request
rules       rules
```

Existing의 공고 의미 규칙을 Request에 그대로 복제하거나,
Request 의미 규칙을 Existing에 역으로 강제하지 않는다.

---

## 3. 최상위 구조

```text
pre_review_request_profile/v0.1
│
├ identity
├ request_type
├ program_hierarchy
│
├ request_context
│  ├ implementation_plan
│  ├ business_need
│  ├ legal_basis
│  ├ linked_policy
│  ├ expected_effect
│  └ performance_indicator
│
├ comparison_profile
│  ├ purpose_goal
│  ├ applicant_eligibility
│  ├ support_target
│  ├ eligibility_conditions
│  ├ beneficiary
│  ├ exclusions
│  ├ participation_requirements
│  ├ program_period
│  ├ support_period
│  ├ support_activities
│  ├ support_methods
│  ├ support_items
│  ├ support_content
│  ├ support_scale
│  ├ total_budget
│  ├ cost_sharing
│  ├ delivery_relations
│  └ delivery_methods
│
├ support_components
├ derived_projections
├ field_states
├ unresolved_relations
├ source_documents
└ processing_metadata
```

top-level `schema_version`은 계속:

```text
pre_review_request_profile/v0.1
```

이다.

`v0.1.2`는 문서 revision이며 schema version을 바꾸는 것이 아니다.

---

## 4. Request/Existing 공유 비교 계약

Raw `field_name`으로 직접 공유하는 어휘는 아래 **16개가 전부**다.

```text
purpose_goal
applicant_eligibility
support_target
eligibility_conditions
beneficiary
exclusions
participation_requirements
program_period
support_period
support_activities
support_methods
support_items
support_content
support_scale
total_budget
cost_sharing
```

값이 한쪽에 없더라도 sparse Fact model로 처리하며 계약 실패가 아니다.

### 공유하지 않는 수행체계 Raw schema

Request:

```text
delivery_relations
delivery_methods
```

Existing:

```text
delivery_roles
```

두 Raw schema를 합치지 않는다.

SIM-4에서 필요할 때 **Delivery Comparison Adapter**가 의미축을 연결한다.

---

## 5. Request 전용 구조

### 5.1 `request_type`

허용 코드:

```text
detail_program_new
sub_program_new
sub_sub_program_new
program_content_change
```

반드시 실제 checkbox 선택상태를 사용한다.

```text
value_source
→ option label exact-span

selection_source
→ checked glyph exact-span

evidence[]
→ 동일 Common IR cell/occurrence
```

본문 의미로 request type을 추론하지 않는다.

복수 체크 / 선택 없음 / glyph-label 대응 불명확:

```text
임의 선택 금지
→ unresolved / diagnostic 처리
```

### 5.2 `program_hierarchy`

```text
detail_program
sub_program
sub_sub_program
```

`support_components`와 구분한다.

```text
program_hierarchy
= 행정적 사업계층

support_components
= 지원패키지 / 참여유형 / 지원단계 등 내부 지원구성
```

### 5.3 `request_context`

```text
implementation_plan
business_need
legal_basis
linked_policy
expected_effect
performance_indicator
```

`change_summary`는 사용하지 않는다.

---

## 6. Raw Fact provenance

### 6.1 `value_source`

CandidatePack exact-span 전용이다.

```json
{
  "source_block_id": "hwp:t21#r0c1p0",
  "start_char": 0,
  "end_char": 8,
  "text_basis": "common_ir_v1_candidate_pack"
}
```

논리 키:

```text
(candidate_pack_id, source_block_id)
```

검증:

```text
value_raw
==
candidate_pack_block.text[start_char:end_char]
```

### 6.2 `evidence[]`

원 Common IR provenance 전용이다.

```text
common_ir_document_id
common_ir_block_id
common_ir_cell_id       # 표일 때
common_ir_occurrence_ids
```

Common IR 조인 기준:

```text
(common_ir_document_id, common_ir_block_id)
```

CandidatePack `source_block_id`와 Common IR `common_ir_block_id`를 같은 namespace로 취급하지 않는다.

### 6.3 표

표의 값 span:

```text
cell-paragraph CandidatePack text
```

행/열 관계:

```text
common_ir_document_id + common_ir_cell_id
→ Common IR cells[] 조회
```

Structured JSON evidence에 `row_index`, `col_index`를 복제하지 않는다.

---

## 7. `field_states`

상태:

```text
identified
partial
not_found
mentioned_unresolved
extraction_failed
not_applicable
```

typed reference:

```text
fact_ids?
relation_ids?
component_ids?
```

예:

```json
{"field_name":"support_target","status":"identified","fact_ids":["target_01"]}
{"field_name":"delivery_relations","status":"identified","relation_ids":["delivery_01"]}
{"field_name":"support_components","status":"identified","component_ids":["component_01"]}
{"field_name":"support_content","status":"not_found"}
```

금지:

```text
delivery_relation_id를 fact_ids에 넣기
support_component_id를 fact_ids에 넣기
not_found용 가짜 Fact 만들기
```

`reason_codes`는 optional이며 taxonomy는 아직 확정하지 않는다.

---

## 8. 지원규모와 numeric locator

Raw `support_scale`:

```text
지원·선정 대상 수
지원금액
지원비율
지원한도
```

기간은 넣지 않는다.

Derived `support_scale_measures`:

```text
measure_type:
count | amount | rate

measure_role:
selection_capacity
support_amount
support_limit
support_rate

comparator:
eq | lt | lte | gt | gte | range | approx
```

각 measure:

```text
source_fact_id 필수
source_numeric_candidate_id 필수
```

numeric locator:

```text
{source_block_id}#num[{index}]
```

예:

```text
hwp:t21#r0c1p0#num[0]
```

이 ID는 Common IR node가 아니다.

재현에는:

```text
candidate_pack_id
+
numeric_candidate_extractor_version
```

이 필요하다.

현재:

```text
numeric_candidate_extractor_version = numeric_candidate_v1
```

독립 `numeric_measure_normalizer_version`은 아직 없다.

---

## 9. 수행체계

### `delivery_relations`

목표:

```text
누가
→ 어떤 역할로
→ 무엇을 하는가
```

actor type:

```text
central_government
local_government
public_agency
financial_institution
private_operator
other
null
```

role:

```text
lead_agency
operating_agency
dedicated_agency
participating_partner
demand_partner
cooperating_organization
null
```

action:

```text
announce
recruit
receive
review
evaluate
select
recommend
agreement
provide_support
disburse
manage
monitor
settle
report
follow_up
null
```

### 관계 생성 원칙

- actor / role / action의 명시적 관계가 있을 때만 생성
- 서로 떨어진 문단을 임의 연결하지 않음
- 표는 동일 row/container 등 구조 근거가 있을 때만 연결
- 안전하지 않은 canonicalization은 `null` 또는 허용된 `other`
- 원문에 순서가 없으면 action sequence를 임의 생성하지 않음

### `delivery_methods`

```text
direct
subsidy
contribution
commissioned
other
null
```

`support_methods`와 의미가 다르다.

```text
support_methods
= 수혜자에게 어떻게 지원하는가

delivery_methods
= 사업 자체를 어떻게 집행하는가
```

---

## 10. Derived Projection

현재 registry:

```text
target_constraints
support_facets
support_scale_measures
delivery_structure
```

### 지금 구현 기준

우선:

```text
target_constraints
support_facets
support_scale_measures
```

를 Raw Fact 뒤에서 생성한다.

Raw Fact가 먼저다.

```text
Common IR/CandidatePack
→ Raw Fact
→ validate
→ Derived Projection
```

Projection 생성 실패가 유효한 Raw Fact 삭제로 이어져서는 안 된다.

### 아직 확정하지 않는 것

`delivery_structure` 상세 schema:

```text
step 구조
sequence 규칙
actor 생략
분산 relation 연결
FIT-6 입력형태
SIM-4 정규화 수준
```

구현 중 임의 확정하지 않는다.

---

## 11. lineage

### `source_documents`

```json
{
  "format": "hwp",
  "common_ir": {
    "document_id": "...",
    "schema_version": "common_ir_v1",
    "source_kind": "hwp",
    "source_sha256": "...",
    "source_location": "...",
    "artifact_role": "..."
  }
}
```

`source_documents[].common_ir` 안에서 alias를 만들지 않는다.

사용하지 않음:

```text
common_ir_version
document_hash
file_format
```

### `processing_metadata`

최소 추적:

```text
pipeline_version
structured_schema_version
input_contract

common_ir_document_id
common_ir_source_sha256
common_ir_schema_version
common_ir_generator
common_ir_generator_version

candidate_pack.candidate_pack_id
candidate_pack.candidate_pack_generator
candidate_pack.candidate_pack_generator_version
candidate_pack.common_ir_document_id
candidate_pack.common_ir_source_sha256
candidate_pack.text_basis

model_id
prompt_version
processed_at
```

`support_scale_measures`가 생성되면 추가:

```text
derived_projection_producers
  .support_scale_measures
  .numeric_candidate_extractor_version
```

parser/version의 source of truth는 Common IR provenance를 기본으로 한다.

---

## 12. Validation

5개 범주:

```text
1. Schema
2. Provenance
3. Reference Integrity
4. Contract Invariant
5. Derived Projection
```

### 대표 검증

Schema:

```text
schema_version
profile_type
enum
ID 중복
```

Provenance:

```text
CandidatePack block 존재
exact-span 일치
Common IR block/cell/occurrence 참조 유효
namespace 혼동 없음
```

Reference:

```text
fact_id
program_node_id
support_component_id
delivery_relation_id
```

Invariant:

```text
program_period를 component에 연결하지 않음
support_scale에 duration 저장 금지
rate는 BPS
PER_UNIT이면 applies_per
허용 enum 밖 값 생성 금지
```

Projection:

```text
source Fact 유효성
numeric locator 유효성
```

실패 수준:

```text
fatal
reject_item
warning
```

하나의 Fact 실패로 전체 Profile을 폐기하지 않는다.

---

## 13. 구현 순서 권장

### Step 1 — 공통부 재사용

Existing Pipeline에서 다음을 먼저 공통화/재사용한다.

```text
Common IR reader
CandidatePack
span resolver
evidence builder
numeric extractor
lineage
validator
```

### Step 2 — 정상 Request 1건 세로 슬라이스

```text
Common IR
→ CandidatePack
→ Request Raw Fact
→ Projection
→ Validation
→ final JSON
```

정상 1건을 끝까지 닫는다.

### Step 3 — Request 전용 예외

```text
checkbox
program hierarchy
request_context
delivery relation
field_states
```

### Step 4 — 표본 확대

다음 순서 권장:

```text
정상 문서
일부 필드 누락
checkbox 이상
표 기반 수치
수행체계 관계
복수 support component
```

---

## 14. 구현 완료조건

단순히 JSON 파일이 생성되는 것은 완료가 아니다.

최소 완료조건:

```text
□ pre_review_request_profile/v0.1 생성
□ Registry 밖 business field 없음
□ accepted Fact exact-span 검증 통과
□ accepted Fact Common IR provenance 역추적 가능
□ request_type label/glyph dual anchor 검증
□ field_states typed reference 통과
□ profile-local reference integrity 통과
□ source_documents lineage 일치
□ CandidatePack lineage 일치
□ numeric locator 재현 가능
□ validation report 생성
□ reject_item 때문에 다른 정상 Fact가 폐기되지 않음
```

---

## 15. 이번 단계에서 하지 않는 것

```text
FIT 판별
SIM 비교
DB 물리 스키마 확정
새 canonical field 추가
새 enum 추가
reason_codes taxonomy 확정
support component ownership 확정
delivery_structure 상세 schema 확정
Existing Raw schema 변경
```

Request Structured JSON은 CPL/FIT/SIM의 **입력 데이터 계약**이다.

---

## 16. 실제 샘플에서 계약 공백이 발견되면

구현자가 임의 확장하지 않는다.

다음 형태로 issue를 남긴다.

```text
[문서/케이스]
[원문]
[현재 CandidatePack/Common IR 구조]
[현재 계약으로 표현 불가능한 이유]
[임시 처리]
[계약 변경 필요 여부]
```

다음 세 항목은 특히 후속 결정사항으로 남아 있다.

```text
support_components ↔ Raw Fact ownership
delivery_structure 상세 schema
reason_codes taxonomy
```

---

## 17. 최종 요약

Request Pipeline의 핵심은:

```text
Existing에서 검증된 공통 structuring infrastructure
+
Request 전용 semantic contract
```

이다.

Request와 Existing은 **비교 의미축을 공유**하지만 Raw JSON을 하나로 합치지 않는다.

최종적으로 모든 accepted Raw Fact는:

```text
CandidatePack exact-span
+
Common IR provenance
+
재현 가능한 lineage
```

를 가져야 한다.
