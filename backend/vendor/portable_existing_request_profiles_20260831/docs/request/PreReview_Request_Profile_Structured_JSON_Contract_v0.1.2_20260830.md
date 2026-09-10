# 중소기업지원사업 사전협의 Pre-review
# Request Structured JSON Contract v0.1.2

- 작성일: 2026-08-30
- 대상: `pre_review_request_profile/v0.1`
- 목적: 사전협의 요청서의 구조화 JSON 계약을 확정하고, CPL/FIT/SIM에서 재사용할 수 있는 공통 데이터 구조를 정의한다.
- 상태: **v0.1 schema 계약 revision 0.1.2 — Request 내부 계약 + Common IR v1 + Existing Program Profile v0.2 동기화 완료**
- 주의: top-level `schema_version`은 계속 `pre_review_request_profile/v0.1`을 사용한다. 본 문서의 `v0.1.2`는 계약 문서 revision이다. `delivery_structure`, `reason_codes` taxonomy, component ownership 등은 실제 케이스 테스트 후 확정한다.

---

## 0. v0.1.1 → v0.1.2 동기화 요약

이번 revision은 Request 의미 스키마를 새로 설계한 버전이 아니라, 확정된 Common IR v1 및 Existing Program Profile v0.2 계약을 Request 계약에 최종 반영한 문서 revision이다.

주요 반영사항:

1. Request/Existing의 **공유 Raw 비교 어휘를 16개 field로 고정**한다.
2. Request의 `delivery_relations` / `delivery_methods`는 유지하되 공유 Raw 비교 어휘에는 포함하지 않고 Delivery Comparison Adapter에서 Existing `delivery_roles`와 대응한다.
3. `value_source`는 CandidatePack exact-span locator만 담당하고, Common IR document/block/cell/occurrence provenance는 `evidence[]`로 분리한다.
4. `source_documents[]`는 원본 메타와 `common_ir` lineage를 분리하는 구조로 동기화한다.
5. `processing_metadata.candidate_pack` lineage를 Existing v0.2와 동일한 필드명으로 보존한다.
6. `source_numeric_candidate_id = {source_block_id}#num[{index}]` 계약과 `numeric_candidate_extractor_version = numeric_candidate_v1` 추적 규칙을 확정한다.
7. Existing `source_profile_id` 형식은 Request `profile_id`에 강제하지 않는다. 각 프로필의 전역 식별 계약은 독립적으로 유지한다.

## 1. 설계 원칙

사전협의 요청서와 기존 지원사업 공고를 완전히 분리된 의미 체계로 만들지 않는다.
SIM에서 비교하는 정보는 **공통 semantic comparison axis**를 공유한다.

단, 이는 Request와 Existing의 Raw JSON key/shape를 완전히 동일하게 만든다는 뜻이 아니다.
특히 수행체계는 Request의 `delivery_relations` / `delivery_methods`와 Existing의 Raw 수행체계 구조가 다를 수 있으며, 후단 Comparison Adapter에서 의미를 대응한다.

```text
사전협의 요청서
        │
        ▼
pre_review_request_profile
        │
        ├─ 요청서 전용 정보
        │    ├─ request_type
        │    ├─ program_hierarchy
        │    ├─ request_context
        │    └─ field_states
        │
        └─ comparison_profile ─────────────┐
                                           │
                                           │ 동일한 비교 의미 계약
                                           │
기존 지원사업 공고                        │
        │                                  │
        ▼                                  │
existing_program_profile                  │
        │                                  │
        └─ comparison_profile ─────────────┘
                          │
                          ▼
                       SIM 비교
```

핵심 원칙은 다음과 같다.

1. **Raw Fact는 원문 사실을 보존한다.**
2. **정규화·비교용 구조는 Derived Projection에서 만든다.**
3. **원문 근거가 없는 값이나 관계를 LLM이 추론하여 생성하지 않는다.**
4. **Canonical vocabulary는 제한된 목록에서만 선택하며 임의의 enum 추가를 금지한다.**
5. **값이 없으면 없는 상태를 기록하되 가짜 Fact/Evidence를 만들지 않는다.**
6. **문서 구조화 검증과 CPL/FIT/SIM의 업무 판단을 분리한다.**

---

## 2. 최상위 구조

```text
pre_review_request_profile/v0.1
│
├─ schema_version
├─ profile_id
├─ profile_type
│
├─ identity
│
├─ request_type
├─ program_hierarchy
│
├─ request_context
│    ├─ implementation_plan
│    ├─ business_need
│    ├─ legal_basis
│    ├─ linked_policy
│    ├─ expected_effect
│    └─ performance_indicator
│
├─ comparison_profile
│    ├─ purpose_goal
│    ├─ applicant_eligibility
│    ├─ support_target
│    ├─ eligibility_conditions
│    ├─ beneficiary
│    ├─ exclusions
│    ├─ participation_requirements
│    ├─ program_period
│    ├─ support_period
│    ├─ support_activities
│    ├─ support_methods
│    ├─ support_items
│    ├─ support_content
│    ├─ support_scale
│    ├─ total_budget
│    ├─ cost_sharing
│    ├─ delivery_relations
│    └─ delivery_methods
│
├─ support_components
│
├─ derived_projections
│    ├─ target_constraints
│    ├─ support_facets
│    ├─ support_scale_measures
│    └─ delivery_structure          # 세부 계약은 후속 확정
│
├─ field_states
├─ unresolved_relations
├─ source_documents
└─ processing_metadata
```

`unresolved_observations`는 v0.1 Business Profile에서 제거한다.

## 2.1 ID scope

Request Profile 내부 식별자 scope는 다음과 같이 고정한다.

| ID | scope | 규칙 |
|---|---|---|
| `profile_id` | global | 시스템에서 Profile을 식별하는 전역 식별자 |
| `fact_id` | profile-local | 실제 식별 단위는 `(profile_id, fact_id)` |
| `program_node_id` | profile-local | 실제 식별 단위는 `(profile_id, program_node_id)` |
| `support_component_id` | profile-local | 실제 식별 단위는 `(profile_id, support_component_id)` |
| `delivery_relation_id` | profile-local | 실제 식별 단위는 `(profile_id, delivery_relation_id)` |

추가 규칙:

- local ID 자체를 시스템 전체 global PK로 가정하지 않는다.
- `source_block_id`는 Request가 독자적으로 정의하는 ID가 아니다. Common IR v1에서 생성된 **CandidatePack block ID**를 사용한다.
- Common IR block/cell/occurrence ID는 Common IR v1 namespace를 그대로 사용하며 Request에서 재정의하지 않는다.
- `source_numeric_candidate_id`는 CandidatePack-local 수치 locator이며, Existing Program Profile v0.2와 동일 계약을 사용한다.

## 2.2 Request Field Registry

모델이 허용 목록 밖의 임의 canonical business field를 생성하지 않도록 Request Profile의 field registry를 고정한다.

### Request `comparison_profile` 허용 field

Request의 `comparison_profile`에는 아래 18개 field를 허용한다. 다만 이 중 **Request/Existing이 Raw `field_name`으로 직접 공유하는 비교 어휘는 앞의 16개뿐**이다.

#### Request/Existing 공유 Raw 비교 어휘 — 16개

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

#### Request 전용 수행체계 field

```text
delivery_relations
delivery_methods
```

`delivery_relations` / `delivery_methods`는 Request `comparison_profile`에 물리적으로 존재하지만 Existing과 공유하는 Raw `field_name` 목록에는 포함하지 않는다. Existing은 자신의 `delivery_roles` 계약을 유지하며 수행체계 비교는 Delivery Comparison Adapter에서 대응한다.

### Request 전용 구조

```text
request_type
program_hierarchy
request_context.implementation_plan
request_context.business_need
request_context.legal_basis
request_context.linked_policy
request_context.expected_effect
request_context.performance_indicator
support_components
field_states
unresolved_relations
```

### Derived Projection registry

```text
target_constraints
support_facets
support_scale_measures
delivery_structure   # 세부 schema 후속 확정
```

규칙:

- registry 밖의 canonical business field를 LLM이 임의 생성하지 않는다.
- `support_components`는 top-level structural container이며 일반 Raw Fact field와 동일하게 취급하지 않는다.
- Request의 `delivery_relations` / `delivery_methods`는 Request registry에 유지한다.
- 이를 이유로 Existing의 Raw 수행체계 schema를 Request와 동일하게 변경하지 않는다.
- Request와 Existing의 공통성은 **semantic comparison axis** 기준이며, physical Raw schema 통일을 의미하지 않는다.

---

# 3. 요청서 전용 구조

## 3.1 `request_type`

사전협의 요청사유의 실제 체크 결과를 구조화한다.

### 허용 코드

```text
detail_program_new
sub_program_new
sub_sub_program_new
program_content_change
```

| 원문 선택값 | 내부 코드 |
|---|---|
| 세부사업 신설 | `detail_program_new` |
| 내역사업 신설 | `sub_program_new` |
| 내내역사업 신설 | `sub_sub_program_new` |
| 사업내용 변경 | `program_content_change` |

### checkbox provenance 계약

Common IR v1에서는 checkbox를 별도 semantic node로 만들지 않는다.
checkbox glyph와 label은 동일 cell/paragraph occurrence의 원문 text 안에 보존되며 CandidatePack text에도 그대로 투영된다.

따라서 `request_type`은 **label 근거와 selection 근거를 분리**한다.

```json
{
  "request_type": {
    "selected_code": "detail_program_new",
    "value_raw": "세부사업 신설",
    "value_source": {
      "source_block_id": "hwp:t652#r0c1p0",
      "start_char": 2,
      "end_char": 9,
      "text_basis": "common_ir_v1_candidate_pack"
    },
    "selection_source": {
      "glyph_raw": "",
      "source_block_id": "hwp:t652#r0c1p0",
      "start_char": 0,
      "end_char": 1,
      "text_basis": "common_ir_v1_candidate_pack"
    },
    "evidence": [
      {
        "source_block_id": "hwp:t652#r0c1p0",
        "common_ir_document_id": "hwp:pre_review_guide_2023",
        "common_ir_block_id": "hwp:t652",
        "common_ir_cell_id": "hwp:t652:c1",
        "common_ir_occurrence_ids": ["occ:rhwp:t652:c1"]
      }
    ]
  }
}
```

검증식:

```text
value_raw
== candidate_pack_block.text[value_source.start_char:value_source.end_char]

glyph_raw
== candidate_pack_block.text[selection_source.start_char:selection_source.end_char]
```

### 규칙

- 본문 내용을 보고 요청유형을 추론하지 않는다.
- 실제 checkbox glyph 상태와 label을 함께 근거로 한다.
- `value_source`는 어떤 option인지를 증명하는 label exact span이다.
- `selection_source`는 해당 option이 실제 선택되었음을 증명하는 glyph exact span이다.
- `value_source`와 `selection_source`는 CandidatePack exact-span만 담당한다. Common IR provenance는 `evidence[]`에 둔다.
- 두 anchor는 동일 CandidatePack block 안에서 복원되어야 하며, `evidence[]`가 가리키는 동일 Common IR cell/occurrence 관계로 연결되어야 한다.
- 반복되는 `□` 등의 glyph를 어떤 occurrence에 대응시키는지는 resolver 구현 책임이며, 계약은 최종 선택된 glyph/label의 정확한 span과 provenance를 요구한다.
- 체크 상태가 불명확하면 `selected_code`를 억지로 확정하지 않는다.
- 비정상적인 복수 체크 등은 validation/diagnostic 대상으로 처리할 수 있다.
- `request_type`과 `program_hierarchy`의 의미상 불일치는 구조 validator 오류가 아니라 CPL/FIT 분석 대상으로 본다.

---

## 3.2 `program_hierarchy`

사전협의 대상 사업의 행정적 계층을 표현한다.

```text
세부사업
 └─ 내역사업
     └─ 내내역사업
```

### level 코드

```text
detail_program
sub_program
sub_sub_program
```

### 기본 구조

```json
{
  "program_hierarchy": {
    "nodes": [
      {
        "program_node_id": "node_001",
        "level": "detail_program",
        "parent_node_id": null,
        "name_raw": "예시 사업명",
        "value_source": {}
      }
    ]
  }
}
```

### 규칙

`program_hierarchy`와 `support_components`는 서로 다른 개념이다.

```text
program_hierarchy
= 행정적 사업 계층

support_components
= 지원패키지 / 참여유형 / 지원단계 등 사업 내부 지원 구성
```

Raw Fact는 필요한 경우 `program_node_id`를 선택적으로 참조할 수 있다.

---

## 3.3 `request_context`

기존 공고와 직접 비교하기보다는 현재 요청서 자체를 CPL/FIT에서 검토하기 위한 요청서 전용 정보다.

| 필드 | 의미 |
|---|---|
| `implementation_plan` | 연차별·내역사업별 추진계획 |
| `business_need` | 사업 신설·변경 필요성 |
| `legal_basis` | 사업 추진·지원 근거 |
| `linked_policy` | 국정과제·기본계획 등 연계정책 |
| `expected_effect` | 기대효과·파급효과 |
| `performance_indicator` | 성과지표 |

### `change_summary` 제거

기존 초안의 `change_summary`는 제거한다.

사유:

- 공식 요청서의 “신설·변경 주요내용”은 별도의 독립 의미 필드라기보다 사업기간·사업예산·지원대상·지원조건·지원내용·수행기관 등 이미 다른 구조에 저장되는 정보의 상위 구획에 가깝다.
- 이를 별도 Raw Fact로 다시 저장하면 동일 의미가 중복될 가능성이 높다.
- v0.1에서는 해당 영역의 실제 세부 정보를 각 의미 필드에 직접 저장한다.

`request_context` 항목은 기본적으로 일반 Raw Fact 계약을 사용하며 과도하게 정규화하지 않는다.

---

# 4. Request `comparison_profile` — 공유 비교 어휘와 Request 전용 수행체계

Request Profile에서 SIM 비교에 사용할 의미 필드를 모은 구조다. Request와 Existing이 **Raw `field_name`으로 직접 공유하는 어휘는 2.2절의 16개 field로 제한**한다. 한쪽에 값이 없더라도 sparse Fact model로 비교하며 계약 실패로 보지 않는다.

Request의 `delivery_relations` / `delivery_methods`는 Request 전용 Raw 구조로 유지한다. 수행체계는 Existing의 `delivery_roles`와 후단 Delivery Comparison Adapter에서 의미축을 대응하며 Raw JSON shape 동일성을 요구하지 않는다.

---

## 4.1 사업 목적

```text
purpose_goal
```

사업이 달성하려는 목적·목표를 저장한다.

SIM-1의 핵심 비교 대상이다.

---

## 4.2 지원대상 및 지원조건

```text
applicant_eligibility
support_target
eligibility_conditions
beneficiary
exclusions
participation_requirements
```

| 필드 | 의미 |
|---|---|
| `applicant_eligibility` | 실제 신청주체 및 신청자 자격 |
| `support_target` | 정책적으로 지원하려는 기본 대상 집합 |
| `eligibility_conditions` | 대상 범위를 좁히는 긍정 조건 |
| `beneficiary` | 신청자와 다를 수 있는 실제 수혜자 |
| `exclusions` | 제외·신청불가·지원제한 |
| `participation_requirements` | 선정 이후 이행 의무 |

### Derived Projection: `target_constraints`

SIM-2 비교용 정규화 구조다.

```json
{
  "projection_type": "target_constraints",
  "positive_source_fact_ids": [
    "applicant_01",
    "target_01",
    "condition_01"
  ],
  "exclusion_source_fact_ids": [
    "exclusion_01"
  ],
  "entity_types": ["중소기업"],
  "regions": ["서울"],
  "industries": ["제조업"],
  "status": "identified"
}
```

규칙:

- 긍정 조건과 제외조건 source Fact를 분리한다.
- `beneficiary`는 `target_constraints`의 직접 입력에서 제외한다.
- 불명확한 조건은 임의 정규화하지 않는다.
- Projection은 Raw Fact로 역추적 가능해야 한다.

---

## 4.3 기간

### `program_period`

사업 자체의 공식 운영·수행 기간.

규칙:

- 기존 공고에서는 선택적일 수 있다.
- `support_components`에 연결하지 않는다.
- 명시되지 않으면 생성하지 않는다.

### `support_period`

선정 이후 기업·팀·과제·수혜자에게 실제 제공되는 지원·협약·수행 기간.

### 기간에서 제외하는 것

다음 값은 `program_period`, `support_period`에 넣지 않는다.

```text
신청기간
접수기간
공고기간
```

기간의 주체가 불분명하면 어느 필드에도 강제로 배치하지 않는다.

---

## 4.4 지원내용

```text
support_activities
support_methods
support_items
support_content
```

| 필드 | 의미 |
|---|---|
| `support_activities` | 무엇을 하도록 지원하는가 |
| `support_methods` | 수혜자에게 어떤 수단으로 지원하는가 |
| `support_items` | 구체적으로 무엇에 사용하거나 무엇을 제공받는가 |
| `support_content` | 위 세 필드로 안전하게 분리하기 어려운 지원내용 원문을 위한 제한적 escape hatch |

예:

```text
support_activities
→ 사업화
→ 제품 실증
→ 청년 고용

support_methods
→ 보조금
→ 융자
→ 교육
→ 멘토링

support_items
→ 인건비
→ 장비비
→ 인증비
```

동일 원문 구간을 여러 FactField에 중복 적재하지 않는다.

복수 의미 분류가 필요하면 `support_facets` Derived Projection을 사용한다.

---

## 4.5 지원규모

`support_scale` Raw Fact는 다음 정량정보만 대상으로 한다.

```text
지원·선정 대상 수
지원금액
지원비율
지원한도
```

기간은 `support_scale`에 포함하지 않는다.

### Derived Projection: `support_scale_measures`

#### `measure_type`

```text
count
amount
rate
```

#### `measure_role`

```text
selection_capacity
support_amount
support_limit
support_rate
```

#### `comparator`

```text
eq
lt
lte
gt
gte
range
approx
```

#### 기타 규칙

- rate 단위는 BPS를 사용한다.
- `80% = 8000 BPS`
- `source_fact_id` 필수
- `source_numeric_candidate_id` 필수
- `eq`: lower = upper
- `lte`, `lt`: upper 사용
- `gte`, `gt`: lower 사용
- `range`: lower/upper 사용
- `approx`: 중심값을 lower=upper로 저장
- `aggregation_scope = PER_UNIT`이면 `applies_per` 필수
- `aggregation_scope = TOTAL`이면 `applies_per` 비움
- 활동 횟수는 지원규모가 아니다.
- 예: `교육 4회` ≠ support_scale
- 예: `20개사 지원` = `selection_capacity`

### `source_numeric_candidate_id` 계약

Request는 Existing Program Profile v0.2와 동일한 CandidatePack 수치 locator 계약을 사용한다.

```text
{source_block_id}#num[{index}]

예:
hwp:b43#num[0]
hwp:t45#r2c1p0#num[1]
```

규칙:

- `source_numeric_candidate_id`는 Common IR node ID나 사업 ID가 아니라 **CandidatePack 결정적 수치 추출기의 locator**다.
- locator 안의 `source_block_id`는 해당 `value_source.source_block_id`와 같은 CandidatePack namespace를 사용한다.
- locator를 해석·재현할 때는 `processing_metadata.candidate_pack.candidate_pack_id`와 함께 사용한다.
- 각 measure의 `source_fact_id`는 필수이며 해당 Fact의 `field_name`은 `support_scale`이어야 한다.
- 동일 locator 재현에는 동일 CandidatePack과 동일 수치 추출 규칙·버전이 필요하다.
- `support_scale_measures` Projection이 실제 생성된 경우에만 `processing_metadata.derived_projection_producers.support_scale_measures.numeric_candidate_extractor_version`을 기록한다. 현재 canonical 값은 `numeric_candidate_v1`이다.
- v0.1에는 독립 `numeric_measure_normalizer_version`을 두지 않는다. 별도 versioned normalizer가 도입되는 시점에 Request/Existing 계약을 함께 revision한다.
- `source_numeric_candidate_id`로 Common IR `blocks[]`, `cells[]`, `occurrences[]`를 직접 조인하지 않는다. Common IR provenance는 Fact의 `evidence[]`를 사용한다.

---

# 5. 수행체계 구조

Request Profile의 기존 초안에서 사용하던 `delivery_roles`는 제거하고 `delivery_relations`로 대체한다. 이 변경은 Existing Program Profile의 Raw 수행체계 schema 변경을 의미하지 않는다.

수행체계 구조화의 목적은 단순 기관 목록 수집이 아니라 다음 질문에 답하기 위한 것이다.

```text
누가
→ 어떤 역할로
→ 무엇을 하는가
```

이 구조는:

- FIT-6: 수행기관·역할·절차의 내부 정합성 확인
- SIM-4: 수행주체·수행방식·주요 전달절차의 사업 간 비교

에 공통 사용한다.

---

## 5.1 `delivery_relations`

기본 구조:

```json
{
  "delivery_relation_id": "delivery_01",
  "actor": {
    "value_raw": "A진흥원",
    "canonical_actor_type": "public_agency",
    "value_source": {}
  },
  "role": {
    "value_raw": "전담기관",
    "canonical_role": "dedicated_agency",
    "value_source": {}
  },
  "actions": [
    {
      "value_raw": "사업을 공고한다",
      "canonical_action": "announce",
      "value_source": {}
    },
    {
      "value_raw": "선정기업과 협약을 체결한다",
      "canonical_action": "agreement",
      "value_source": {}
    }
  ],
  "relation_container": {}
}
```

### 관계 원칙

- `actor`, `role`, `action`이 원문에서 명시적으로 연결된 경우에만 관계를 만든다.
- 서로 떨어진 문단의 기관명과 행위를 LLM이 임의 연결하지 않는다.
- 표에서는 동일 row 등 명시적 relation container 안에서 연결되어야 한다.
- 원문 표현과 provenance가 canonical 값보다 우선한다.
- 안전한 canonicalization이 불가능하면 `null` 또는 허용된 `other`를 사용한다.

---

## 5.2 `canonical_actor_type`

v0.1 허용값:

```text
central_government
local_government
public_agency
financial_institution
private_operator
other
null
```

의미:

| 코드 | 의미 |
|---|---|
| `central_government` | 중앙행정기관 |
| `local_government` | 지방자치단체 |
| `public_agency` | 공공기관·공공 수행기관 |
| `financial_institution` | 은행 등 금융기관 |
| `private_operator` | 민간 수행·운영기관 |
| `other` | 기관 유형은 명시되나 현 enum에 안전하게 대응하지 못함 |
| `null` | 기관 유형을 안전하게 특정할 수 없음 |

`전문기관`처럼 해당 사업에서의 기능적 지위를 나타내는 표현은 actor type보다는 `canonical_role`로 처리하는 것을 우선한다.

---

## 5.3 `canonical_role`

v0.1 허용값:

```text
lead_agency
operating_agency
dedicated_agency
participating_partner
demand_partner
cooperating_organization
null
```

`announcing_agency`는 제거한다.

사유:

```text
공고기관
```

은 별도의 역할 유형으로 저장하기보다:

```text
actor = 해당 기관
canonical_action = announce
```

로 표현하는 것이 역할과 행위의 경계를 더 명확하게 유지한다.

새로운 역할 표현이 나타나더라도 LLM이 enum을 추가하지 않는다.

```text
value_raw = 원문 역할 표현
canonical_role = null
```

로 보존할 수 있다.

---

## 5.4 `canonical_action`

v0.1 허용값:

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

| 코드 | 의미 |
|---|---|
| `announce` | 사업 공고 |
| `recruit` | 참여기업·참여자 모집 |
| `receive` | 신청·접수 |
| `review` | 서류·요건·적격 검토 |
| `evaluate` | 심사·선정평가 |
| `select` | 최종 선정 |
| `recommend` | 후보·사업자 등 추천 |
| `agreement` | 협약 체결 |
| `provide_support` | 교육·컨설팅·서비스 등 비금전적 지원 제공 |
| `disburse` | 지원금 지급·사업비 집행·대출 실행 등 자금 집행 |
| `manage` | 사업 전반의 운영·관리 |
| `monitor` | 수행 중 진행·이행상태 점검 |
| `settle` | 정산 |
| `report` | 결과·실적 보고 |
| `follow_up` | 지원 종료 또는 선정 이후 사후관리 |
| `null` | 안전한 분류 불가 |

### 주요 경계

```text
review
= 형식·자격·요건 검토

evaluate
= 선정·심사를 위한 평가
```

```text
manage
= 사업 전반 운영·관리

monitor
= 수행 중 이행·진행 점검

follow_up
= 지원 이후 사후관리
```

`pay` 대신 `disburse`를 사용한다.

사유:

```text
보조금 지급
대출 실행
사업비 집행
```

등 서로 다른 자금 지원 형태를 수행절차 차원에서는 하나의 자금 집행 action으로 표현할 수 있기 때문이다.

지원수단의 차이는 별도 `support_methods`에서 표현한다.

---

## 5.5 `delivery_methods`

사업 자체의 수행·집행 방식을 표현한다.

`support_methods`와 구분한다.

```text
support_methods
= 수혜자에게 어떤 수단으로 지원하는가
  예: 보조금, 융자, 보증, 교육, 멘토링

delivery_methods
= 사업을 어떤 구조로 수행·집행하는가
  예: 직접, 보조, 출연, 위탁
```

### v0.1 허용값

```text
direct
subsidy
contribution
commissioned
other
null
```

예:

```text
민간 전문기관에 위탁하여 사업을 운영하고,
선정기업에 보조금을 지원한다.
```

구조:

```text
delivery_method = commissioned
support_method = 보조금
```

---

## 5.6 Derived Projection: `delivery_structure`

`delivery_structure`는 FIT-6/SIM-4가 사용하기 위한 후단 구조다.

개념적으로는 다음 정보를 조립할 수 있다.

```text
actor
role
actions
action sequence
delivery_method
```

단, **v0.1에서는 세부 스키마를 확정하지 않는다.**

후속 실제 케이스 테스트를 통해 다음을 검증한 뒤 정의한다.

- action 순서를 언제 안전하게 부여할 수 있는지
- actor가 생략된 절차 문장을 어떻게 처리할지
- 여러 문단·표에 분산된 관계를 어떤 조건에서 연결할지
- FIT-6 충돌 탐지에 필요한 최소 relation 구조
- SIM-4 비교에 필요한 정규화 수준

원문에 순서가 명시되지 않으면 절차 순서를 임의 생성하지 않는다.

---

# 6. Raw Fact provenance 계약

Request Profile은 Common IR v1을 입력 계약으로 사용하고, 실제 exact-span 좌표는 Common IR에서 투영된 CandidatePack text를 기준으로 한다.

LLM이 선택 단계에서 제출한 임시 anchor를 검증 없이 최종 Evidence로 사용하지 않는다.

## 6.1 `value_source`와 `evidence[]` 책임 분리

`value_source`는 CandidatePack exact-span locator만 담당하고, Common IR provenance는 `evidence[]`에 분리한다.

```json
{
  "fact_id": "scale_01",
  "field_name": "support_scale",
  "value_raw": "기업당 최대 3천만원",
  "evidence": [
    {
      "source_block_id": "hwp:t652#r1c2p0",
      "common_ir_document_id": "hwp:request_001",
      "common_ir_block_id": "hwp:t652",
      "common_ir_cell_id": "hwp:t652:c5",
      "common_ir_occurrence_ids": ["occ:rhwp:t652:c5"]
    }
  ],
  "value_source": {
    "source_block_id": "hwp:t652#r1c2p0",
    "start_char": 5,
    "end_char": 16,
    "text_basis": "common_ir_v1_candidate_pack"
  },
  "context_source_block_ids": [
    "hwp:t652#r1c2p0"
  ],
  "status": "identified"
}
```

### ID 의미

```text
value_source.source_block_id
= CandidatePack block ID
= exact-span 계산 기준
= 논리 해석 단위: (candidate_pack_id, source_block_id)

evidence[].common_ir_document_id
evidence[].common_ir_block_id
evidence[].common_ir_cell_id
evidence[].common_ir_occurrence_ids
= 원 Common IR provenance
```

CandidatePack block ID와 Common IR block ID가 문자열상 우연히 같더라도 같은 namespace로 취급하지 않는다.

`evidence[]` 규칙:

- `common_ir_document_id`: 필수
- `common_ir_block_id`: 필수
- `common_ir_cell_id`: 표 셀 기반 근거일 때 사용하며 비표 근거에서는 생략 가능
- `common_ir_occurrence_ids`: 원문 occurrence 추적에 사용
- `source_block_id`: 해당 evidence가 대응하는 CandidatePack block ID. exact-span 자체는 `value_source`에서 검증한다.
- 표의 행/열 좌표는 evidence에 복제하지 않고 Common IR `cells[]`를 조회한다.

Request와 Existing의 공유 Fact를 비교할 때도 `value_raw` / `value_source` / `evidence[]`의 역할을 섞지 않는다.

## 6.2 exact-span 계약

- `text_basis`: `common_ir_v1_candidate_pack`
- 기준 텍스트: CandidatePack `block.text`
- 문자 기준: Python Unicode code-point
- `start_char`: inclusive
- `end_char`: exclusive
- CandidatePack text를 요약·재작성·공백 정규화한 값을 span 기준으로 사용하지 않는다.

검증식:

```text
value_raw
== candidate_pack_block.text[start_char:end_char]
```

### 원칙

- 일반 Raw Fact는 원칙적으로 하나의 연속 value span을 가진다.
- 문맥용 block은 `context_source_block_ids`로 분리하며 이 ID 역시 CandidatePack block ID다.
- 떨어진 여러 위치의 문자열을 이어 붙여 하나의 Raw Fact로 만들지 않는다.
- source locator는 사용한 Common IR/CandidatePack lineage와 함께 해석한다.

## 6.3 표 provenance

표 안의 값도 table 전체 text를 span 기준으로 사용하지 않는다.

- 값 exact span: 해당 셀/문단의 CandidatePack block text
- 표 구조 provenance: `common_ir_block_id` + `common_ir_cell_id` + `common_ir_occurrence_ids`
- 행/열 좌표(`row_index`, `col_index`, `row_span`, `col_span`)는 Structured JSON evidence에 복제하지 않는다.
- 행/열 관계가 필요하면 `common_ir_document_id + common_ir_cell_id`로 Common IR `cells[]`를 조회한다.
- PDF 등 source에서 cell/relation이 제공되지 않으면 Request Profile이 임의 생성하지 않는다.

`delivery_relations`는 관계형 Raw Fact의 예외로서 actor/role/action 각각의 provenance와 명시적 `relation_container`를 요구한다.

## 6.4 locator 재현 조건

동일 locator 재현은 최소 다음 조건이 동일한 경우에만 기대한다.

```text
source_sha256
source_kind
parser/version
Common IR generator/version
Common IR schema_version
CandidatePack generator/version
```

원본 또는 관련 버전이 바뀌면 기존 locator를 새 IR/CandidatePack에 이식하지 않고 재생성한다.

---

# 7. `field_states`

CPL과 부분 실행을 지원하기 위해 atomic field 단위의 상태를 저장한다.

### 상태 코드

```text
identified
partial
not_found
mentioned_unresolved
extraction_failed
not_applicable
```

| 상태 | 의미 |
|---|---|
| `identified` | 값을 안전하게 식별 |
| `partial` | 객관적으로 일부만 추출·식별됨 |
| `not_found` | 해당 정보를 찾았으나 값이 문서에 존재하지 않음 |
| `mentioned_unresolved` | 관련 언급은 있으나 값을 안전하게 특정할 수 없음 |
| `extraction_failed` | 관련 원문 영역은 있으나 기술적 추출·provenance 검증 실패 |
| `not_applicable` | 요청유형·문서구조상 해당 필드가 적용되지 않음 |

## 7.1 typed reference

`field_states`는 참조 대상의 ID type을 섞지 않는다.

사용 가능한 reference field:

```text
fact_ids?: string[]
relation_ids?: string[]
component_ids?: string[]
```

각 배열은 해당 타입을 실제 참조할 때만 사용하며 필요 없는 배열은 생략할 수 있다.

예:

```json
{
  "field_name": "support_target",
  "status": "partial",
  "fact_ids": ["target_01"],
  "reason_codes": ["candidate_validation_failed"]
}
```

```json
{
  "field_name": "delivery_relations",
  "status": "identified",
  "relation_ids": ["delivery_01", "delivery_02"]
}
```

```json
{
  "field_name": "support_components",
  "status": "identified",
  "component_ids": ["component_01", "component_02"]
}
```

규칙:

- Fact ID는 `fact_ids`에만 둔다.
- `delivery_relation_id`는 `relation_ids`에만 둔다.
- `support_component_id`를 상태에서 추적하는 경우 `component_ids`에만 둔다.
- 서로 다른 ID type을 하나의 배열에 섞지 않는다.
- `component_ids`의 존재는 Raw Fact를 component에 귀속한다는 의미가 아니다. component ownership은 후속 결정사항이다.
- 이 typed-reference 계약을 Existing Program Profile에 강제하지 않는다.

### `reason_codes`

```text
reason_codes?: string[]
```

- 선택 필드다.
- v0.1에서는 필수 taxonomy를 확정하지 않는다.
- 실제 파싱·구조화 회귀 테스트에서 반복되는 실패 유형을 기반으로 목록을 정한다.
- 특히 `partial`, `mentioned_unresolved`, `extraction_failed`의 진단에 사용할 수 있다.

### `partial` 사용 규칙

내용이 “부족해 보인다”는 판단만으로 `partial`을 주지 않는다.

객관적으로:

```text
일부 후보/하위요소는 성공
+
다른 일부는 추출 또는 검증 실패
```

가 확인되는 경우에만 사용한다.

---

# 8. `not_found`와 Evidence

`not_found`는 없는 값을 표현하기 위한 상태이지 Fact가 아니다.

따라서:

```json
{
  "field_name": "legal_basis",
  "status": "not_found"
}
```

처럼 표현한다.

### 금지

- “없음”이라는 사실을 만들기 위한 가짜 Fact 생성
- 없는 값에 대한 가짜 `value_source` 생성
- absence evidence를 만들기 위해 임의의 문단을 근거로 연결

정형 요청서에서 어떤 영역을 검사했는지는 필요하다면 parser/run log에서 관리한다.

Business Profile에는 absence용 Fact를 강제하지 않는다.

---

# 9. `unresolved_relations`

`unresolved_relations`는 유지한다.

용도:

```text
개별 Fact는 확보했지만
Fact 간 관계를 안전하게 확정할 수 없는 경우
```

예:

```text
기관명 Fact 존재
역할 Fact 존재
하지만 두 값이 동일 기관-역할 관계인지 명확하지 않음
```

이 경우 임의로 `delivery_relations`를 만들지 않고 관계 미확정 상태로 남길 수 있다.

---

# 10. `unresolved_observations` 제거

`unresolved_observations`는 `pre_review_request_profile/v0.1`에서 제거한다.

사유:

1. “어느 필드인지는 모르지만 중요해 보이는 내용” 자체가 별도의 모호한 중요도 판단을 요구한다.
2. LLM이 중요해 보이는 문장을 임의로 축적하면 Business Profile의 의미 경계가 흐려진다.
3. Common IR에 원문이 보존되므로 구조화되지 않은 문장이 사라지는 것은 아니다.
4. 향후 구조화 규칙 개선 시 Common IR에서 다시 처리할 수 있다.

필요성이 실제 구현 과정에서 확인되면 Business Profile이 아니라 별도 debug/processing artifact로 도입한다.

---

# 11. Validation 계약

Validation의 목적은 **데이터 구조 및 근거 일관성 검증**이다.

정책·사업·중복 여부 등 업무 판단을 validator가 수행하지 않는다.

## 11.1 검증 범주

```text
1. Schema
2. Provenance
3. Reference Integrity
4. Contract Invariant
5. Derived Projection
```

### Schema

예:

- `schema_version`
- `profile_type`
- 필수 필드 타입
- enum 허용값
- `fact_id` 중복 여부

### Provenance

예:

- `value_source.source_block_id`가 사용한 CandidatePack에 존재하는지 여부
- `evidence[].common_ir_document_id` / `common_ir_block_id` / 필요 시 cell / occurrence 참조 유효성
- CandidatePack locator와 Common IR provenance namespace를 혼동하지 않는지 확인
- offset 범위 유효성
- `value_raw == candidate_pack_block.text[start_char:end_char]`

### Reference Integrity

예:

- 존재하지 않는 `source_fact_id`
- 존재하지 않는 `program_node_id`
- 존재하지 않는 `support_component_id`
- 존재하지 않는 `delivery_relation_id`

를 참조하지 않는지 확인한다.

### Contract Invariant

예:

- `program_period`를 component에 연결하지 않음
- `support_scale`에 duration을 저장하지 않음
- rate 단위가 BPS인지 확인
- `PER_UNIT`이면 `applies_per` 존재
- 동일 occurrence를 서로 다른 semantic field에 중복 저장하지 않음
- `delivery_relations`의 relation provenance 존재
- canonical enum이 허용 목록 밖으로 증가하지 않음

### Derived Projection

Projection이 참조하는 Raw Fact가 유효한지 확인한다.

Projection 생성 실패가 유효한 Raw Fact 삭제로 이어져서는 안 된다.

---

## 11.2 실패 수준

```text
fatal
reject_item
warning
```

| 수준 | 의미 |
|---|---|
| `fatal` | 전체 입력/Profile 생성 불가 |
| `reject_item` | 특정 Fact/Projection만 거부·재시도, 나머지는 계속 처리 |
| `warning` | 비치명적 진단·부분 문제 |

예:

```text
문서 전체 parsing 실패
→ fatal

특정 Fact offset 검증 실패
→ reject_item

선택적 Projection 생성 실패
→ warning 또는 해당 Projection reject
```

### Validation에 포함하지 않는 것

`request_type ↔ program_hierarchy`의 의미상 정합성 같은 업무 규칙은 hard structure validator의 책임이 아니다.

이는 CPL/FIT 분석에서 다룬다.

---

# 12. `source_documents` — 원본 메타와 Common IR v1 lineage 분리

Request Structured JSON은 실제 구조화에 사용한 원본 문서 메타와 Common IR lineage를 분리하여 보존한다. Existing Program Profile v0.2와 동일하게 `source_documents[].common_ir`에 Common IR 정식 필드명을 사용한다.

권장 구조:

```json
{
  "source_documents": [
    {
      "format": "hwp",
      "common_ir": {
        "document_id": "hwp:request_001",
        "schema_version": "common_ir_v1",
        "source_kind": "hwp",
        "source_sha256": "<sha256>",
        "source_location": "<source-location>",
        "artifact_role": "<artifact-role>"
      }
    }
  ]
}
```

### `common_ir` lineage

| 필드 | 출처 | 규칙 |
|---|---|---|
| `document_id` | Common IR `document.document_id` | 필수 |
| `schema_version` | Common IR 최상위 `schema_version` | 필수 |
| `source_kind` | Common IR `document.source_kind` | 필수 |
| `source_sha256` | Common IR `document.provenance.source_sha256` | 필수 |
| `source_location` | Common IR `document.provenance.source_location` | 필수 |
| `artifact_role` | Common IR `document.artifact_role` | 원본에 존재할 때 그대로 복사 |

규칙:

- Common IR lineage 안에서는 `document_id`, `schema_version`, `source_kind`, `source_sha256`, `source_location`, `artifact_role`이라는 정식 이름을 사용한다.
- `common_ir_document_id`, `common_ir_version`, `document_hash`, `file_format` 같은 alias를 `source_documents[].common_ir` 안에 만들지 않는다.
- `format`은 source document 수준에 두며 Common IR lineage 안의 `source_kind`와 역할을 구분한다.
- `artifact_role`을 Request가 새 의미로 재작성하지 않고 Common IR 원값을 그대로 보존한다.
- 중간 Format IR의 hash/schema는 Request Profile의 필수 lineage 대상이 아니다.

# 13. `processing_metadata`

목적:

> 이 Structured JSON이 어떤 Request structuring pipeline, Common IR, CandidatePack, 모델/프롬프트 및 Derived Projection producer 계보로 생성되었는지 추적할 수 있도록 한다.

`common_ir_version` 같은 모호한 단일 키는 사용하지 않는다. Common IR 자체 lineage와 CandidatePack lineage의 책임을 분리한다.

### 권장 구조

```json
{
  "processing_metadata": {
    "pipeline_version": "<pipeline-version>",
    "structured_schema_version": "pre_review_request_profile/v0.1",
    "input_contract": "common_ir_v1",

    "common_ir_document_id": "hwp:request_001",
    "common_ir_source_sha256": "<sha256>",
    "common_ir_schema_version": "common_ir_v1",
    "common_ir_generator": "<generator>",
    "common_ir_generator_version": "<version>",

    "candidate_pack": {
      "candidate_pack_id": "<pack-id>",
      "candidate_pack_generator": "semantic_structuring.common_ir_v1",
      "candidate_pack_generator_version": "1",
      "common_ir_document_id": "hwp:request_001",
      "common_ir_source_sha256": "<sha256>",
      "text_basis": "common_ir_v1_candidate_pack"
    },

    "derived_projection_producers": {
      "support_scale_measures": {
        "numeric_candidate_extractor_version": "numeric_candidate_v1"
      }
    },

    "model_id": "model-name-or-id",
    "prompt_version": "0.1.0",
    "processed_at": "2026-08-30T14:00:00+09:00"
  }
}
```

### 필드 역할

| 필드 | 의미 |
|---|---|
| `pipeline_version` | Request 구조화 파이프라인 버전 |
| `structured_schema_version` | Request Structured JSON schema version |
| `input_contract` | 입력 Common IR 계약 |
| `common_ir_document_id` | 실제 사용한 Common IR document ID |
| `common_ir_source_sha256` | 해당 Common IR이 가리키는 원본 SHA-256 |
| `common_ir_schema_version` | Common IR schema version |
| `common_ir_generator` | Common IR generator 식별자 |
| `common_ir_generator_version` | Common IR generator version |
| `candidate_pack.candidate_pack_id` | 구조화에 사용한 CandidatePack 식별자 |
| `candidate_pack.candidate_pack_generator` | CandidatePack generator 식별자 |
| `candidate_pack.candidate_pack_generator_version` | CandidatePack generator version |
| `candidate_pack.common_ir_document_id` | CandidatePack이 투영된 Common IR document ID |
| `candidate_pack.common_ir_source_sha256` | CandidatePack이 연결된 원본 SHA-256 |
| `candidate_pack.text_basis` | exact-span text basis. `common_ir_v1_candidate_pack` |
| `derived_projection_producers.support_scale_measures.numeric_candidate_extractor_version` | 수치 candidate locator 생성 규칙 버전. Projection이 생성된 경우에만 기록 |
| `model_id` | 구조화에 사용한 모델 식별자 |
| `prompt_version` | 구조화 프롬프트 버전 |
| `processed_at` | 처리 시각 |

### 정합성 규칙

- top-level `common_ir_document_id` / `common_ir_source_sha256`와 `candidate_pack` 내부의 동일 필드는 같은 입력 계보를 가리켜야 한다.
- `source_documents[].common_ir.document_id` 및 `source_sha256`와도 일치해야 한다.
- `numeric_candidate_extractor_version`은 `support_scale_measures` Projection이 실제 존재할 때만 기록한다.
- v0.1에서는 독립 numeric normalizer version을 정의하지 않는다.

### parser/version 처리

locator 재현 조건에는 parser/version도 포함된다.
다만 Request Profile에서 별도의 `parser_version` canonical key를 중복 정의하지 않고, 참조된 Common IR document provenance에서 추적하는 것을 기본으로 한다.

구현상 별도 pass-through가 필요하면 diagnostic/lineage 확장으로 둘 수 있으나, Common IR provenance와 상충하는 별도 source of truth로 만들지 않는다.

# 14. v0.1.2 확정사항 요약

| 구분 | 결정 |
|---|---|
| A. `request_context` | 6개 필드 유지, `change_summary` 제거 |
| B. `request_type` | 4개 내부 코드 + `value_source`/`selection_source` 이중 anchor 확정 |
| C. `program_hierarchy` | `detail_program / sub_program / sub_sub_program` 유지 |
| D. `field_states` | `fact_ids / relation_ids / component_ids` typed reference 확정 |
| E. ID scope | `profile_id` global, Fact/Node/Component/Relation ID는 profile-local |
| F. Field Registry | Request 허용 business/structural field 목록 고정 |
| G. `not_found` Evidence | Fact/Evidence 생성하지 않음 |
| H. `unresolved_observations` | v0.1 Business Profile에서 제거 |
| I. 수행체계 | Request는 `delivery_relations` / `delivery_methods` 유지 |
| J. Common IR | Request/Existing 공통 `common_ir_v1` 사용, Request 전용 dialect 금지 |
| K. exact-span | CandidatePack `block.text`, `common_ir_v1_candidate_pack` 기준 |
| L. provenance | CandidatePack locator와 Common IR document/block/cell/occurrence provenance 분리 |
| M. `source_documents` | Common IR v1 lineage 값 보존 |
| N. `processing_metadata` | Common IR/CandidatePack generator lineage를 명시적으로 보존 |
| O. `source_numeric_candidate_id` | CandidatePack numeric locator 계약 확정 (`{source_block_id}#num[{index}]`) |
| P. Shared comparison vocabulary | Request/Existing 공통 Raw `field_name` 16개로 고정 |
| Q. Existing 수행체계 | Existing `delivery_roles` 유지, Request 수행체계는 Adapter로 대응 |

---

# 15. 수행체계 canonical vocabulary v0.1

## actor type

```text
central_government
local_government
public_agency
financial_institution
private_operator
other
null
```

## role

```text
lead_agency
operating_agency
dedicated_agency
participating_partner
demand_partner
cooperating_organization
null
```

## action

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

## delivery method

```text
direct
subsidy
contribution
commissioned
other
null
```

### 공통 통제 규칙

1. 허용 목록 밖의 canonical enum을 LLM이 새로 만들지 않는다.
2. 적절한 값이 없으면 `null` 또는 허용된 `other`를 사용한다.
3. canonical 값은 원문으로 뒷받침될 때만 부여한다.
4. actor-role-action 관계는 명시적 관계 근거가 있을 때만 만든다.
5. 서로 다른 원문 위치의 정보를 추론으로 합치지 않는다.
6. action 순서는 원문 또는 문서 구조상 명확할 때만 부여한다.
7. `value_raw`와 provenance가 canonical 값보다 우선한다.

---

# 16. 후속 결정사항

아래 항목은 v0.1 Raw 계약과 분리하여 후속 테스트에서 확정한다.

## 16.1 `delivery_structure` Derived Projection

검토 예정:

- step 구조
- sequence 부여 규칙
- actor 생략 처리
- 분산된 관계의 연결 조건
- FIT-6 충돌 판정 입력 구조
- SIM-4 비교용 정규화 수준

## 16.2 `reason_codes` taxonomy

실제 개발·회귀 테스트에서 반복적으로 발생하는 실패 사례를 수집한 뒤 확정한다.

## 16.3 Existing Program Profile 동기화 — 완료

Existing Program Profile v0.2 전달 계약을 반영했으며 Raw schema를 Request 구조에 강제로 맞추지 않는다.

확정 원칙:

- Request/Existing 공통 Raw 비교 어휘는 16개 field로 고정한다.
- Request는 `delivery_relations` / `delivery_methods`를 유지한다.
- Existing은 `delivery_roles`를 유지한다.
- 수행체계 비교는 후단 **Delivery Comparison Adapter**에서 semantic axis를 대응한다.
- Request 전용 `request_type`, `program_hierarchy`, `request_context`, `field_states`를 Existing에 강제하지 않는다.
- Existing 전용 `payment_terms`, `duplicate_support_conditions`, `applicable_entity`를 Request Field Registry에 자동 추가하지 않는다.
- Common IR provenance/lineage 및 CandidatePack exact-span 계약은 같은 입력 계약을 사용한다.
- `source_numeric_candidate_id`와 `numeric_candidate_extractor_version`은 Existing v0.2와 동일 계약을 사용한다.
- Existing의 `source_profile_id` 형식(`hwp:PBLN_...`, `hwpx:PBLN_...`, `pdf:PBLN_...`)을 Request `profile_id` 형식에 강제하지 않는다.

### 아직 강제하지 않는 항목

- Existing `delivery_roles` → Request `delivery_relations`로의 Raw schema 변경
- Request `delivery_relations` → Existing 구조로의 축소
- support component ownership
- `delivery_structure` 상세 step schema

---

# 17. 최종 구조 요약

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
│
├ derived_projections
│  ├ target_constraints
│  ├ support_facets
│  ├ support_scale_measures
│  └ delivery_structure        # 후속 세부 확정
│
├ field_states
├ unresolved_relations
├ source_documents
└ processing_metadata
```

---

## 문서 상태

이 문서는 2026-08-30까지 확정된 `pre_review_request_profile/v0.1` schema의 **계약 문서 revision 0.1.2**다.

### 현재 Request 내부 계약

```text
1. field_states typed reference
2. Request-local ID scope
3. Request Field Registry
4. request_type checkbox provenance
5. support/target/period/delivery Raw 의미 계약
```

### 외부 입력·비교 계약 동기화 완료

```text
1. Common IR v1 exact-span / provenance / lineage
2. CandidatePack lineage 및 ID namespace
3. Existing Program Profile v0.2 shared comparison vocabulary
4. source_numeric_candidate_id 의미·재현 계약
5. numeric_candidate_extractor_version 추적 계약
6. Existing delivery_roles ↔ Request delivery_relations/delivery_methods Adapter 경계
```

따라서 Common IR 또는 Existing 계약의 추가 변경이 없는 한, **Request Structured Profile 구현은 본 revision을 기준선으로 진행할 수 있다.**

### 실제 사례 후 후속 결정

```text
1. support_components ↔ Raw Fact ownership
2. delivery_structure Derived Projection 상세 schema
3. reason_codes taxonomy
```

위 세 항목은 현재 미확정 상태를 유지하는 것이 의도된 결정이며, 구현 중 임의로 schema를 확장하지 않는다.

### 기존 synthetic fixture 상태

2026-08-29에 작성된 기존 synthetic fixture는 object shape/type 검토용 legacy 자료다. 기존 `body[n]` provenance, 이전 `field_states` reference, 과거 lineage 표현 및 `request_type` checkbox anchor는 본 v0.1.2 production 계약의 Gold evidence로 간주하지 않는다.

실제 Request Pipeline 구현 시에는 Common IR v1 CandidatePack을 입력으로 사용하여 본 v0.1.2 계약에 맞는 새로운 fixture/Gold sample을 생성한다.
