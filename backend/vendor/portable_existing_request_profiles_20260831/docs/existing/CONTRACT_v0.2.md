# existing_program_profile/v0.2 계약 노트

- 작성일: 2026-08-30
- 상태: 승인된 문서 계약. 이 노트만이 v0.2 정식 계약이다. 코드·JSON 산출물·Common IR·기존 리뷰/트리아지 문서는 여기서 수정하지 않는다.
- 목적: Existing 공고 프로필의 식별, 조인 키, Common IR lineage, 비교 어휘, 수행체계 경계를 고정한다.
- 비목적: 의미 품질 회귀, Request 프로필 변경, 물리 스키마 DDL.

관련 입력 계약은 [Common IR v1 구조화 입력 안내](../handoff/common_ir_v1_for_semantic_structuring.md)를 따른다. 필드명은 그 문서의 정식 이름을 그대로 쓰고, 별칭을 만들지 않는다.

## 1. 전역 식별

`source_profile_id`가 소스 프로필의 전역 식별자다. 같은 공고의 HWP / HWPX / PDF 결과는 각각 독립 프로필이며, 포맷을 ID에 포함한다.

```text
hwp:PBLN_000000000125056
```

`notice_id`는 공고 식별(`bizinfo:PBLN_…`)이다. 포맷별 결과를 합치지 않는다.

요청서의 `profile_id`를 Existing에 추가하지 않는다. Existing 쪽 전역 키는 `source_profile_id`만 사용한다.

## 2. ID 범위와 RDB 논리 키

로컬 ID는 전역 유일하지 않다. 적재·조인은 아래 복합 키로만 한다.

| 대상 | 논리 키 | 범위 |
| --- | --- | --- |
| Raw Fact | `(source_profile_id, fact_id)` | `fact_id`는 해당 소스 프로필 안에서만 유일 |
| 지원 컴포넌트 | `(source_profile_id, support_component_id)` | `support_component_id`는 해당 소스 프로필 안에서만 유일 |
| CandidatePack exact-span 블록 | `(candidate_pack_id, source_block_id)` | `source_block_id`는 CandidatePack 안에서만 유일 |
| Common IR 원문 블록 | `(common_ir_document_id, common_ir_block_id)` | `common_ir_block_id`는 해당 Common IR 문서 안에서만 유일 |

`value_source.source_block_id`는 CandidatePack의 exact-span text 기준 ID다. CandidatePack lineage는
`processing_metadata.candidate_pack`에 보관한다. 원 Common IR 조인은 Fact의
`evidence[].common_ir_document_id`와 `evidence[].common_ir_block_id`를 사용한다. 현재 표본은 두 ID가
같은 문자열처럼 보일 수 있으나, 계약상 같은 namespace로 취급하지 않는다.

포맷 선택 우선순위(`HWPX → HWP → native PDF`)는 ingest resolver 정책이다. 프로필에 `source_priority_rank`를 두지 않는다.

## 3. `source_documents.common_ir` lineage

각 `source_documents[]`는 원본 파일 메타와 입력 Common IR lineage를 분리한다. lineage 객체는 Common IR 정식 이름만 복사한다.

```json
{
  "document_id": "hwp:PBLN_000000000125056",
  "schema_version": "common_ir_v1",
  "source_kind": "hwp",
  "source_sha256": "…",
  "source_location": "…",
  "artifact_role": "production"
}
```

| 필드 | 출처 | 필수 |
| --- | --- | --- |
| `document_id` | Common IR `document.document_id` | 필수 |
| `schema_version` | Common IR 최상위 `schema_version` | 필수 |
| `source_kind` | Common IR `document.source_kind` | 필수 |
| `source_sha256` | Common IR `document.provenance.source_sha256` | 필수 |
| `source_location` | Common IR `document.provenance.source_location` | 필수 |
| `artifact_role` | Common IR `document.artifact_role` | 선택. 원본에 있을 때만 복사 |

사용하지 않는 이름: `common_ir_document_id`, `common_ir_version`, `document_hash`, `format`(lineage 안), `file_format`. `format`은 `source_documents[].format`에만 둔다.

## 4. `source_numeric_candidate_id`

`support_scale_measures.measures[].source_numeric_candidate_id`는 CandidatePack 결정적 수치 추출기가 만든 locator다. Common IR 노드 ID도, 사업 ID도 아니다. Common IR에 numeric node를 추가하지 않는다.

```text
{source_block_id}#num[{index}]
예: hwp:b43#num[0]
```

계약:

- 각 measure는 `source_fact_id`가 필수다. 해당 Fact는 `support_scale`이어야 한다.
- 같은 locator를 재현하려면 같은 추출 규칙·버전이 필요하다.
- `numeric_candidate_extractor_version`은 `support_scale_measures`가 실제로 생성된 프로필에서만
  `processing_metadata.derived_projection_producers.support_scale_measures`에 기록한다. Projection이 없으면 넣지 않는다.
- v0.2에는 독립 numeric normalizer version을 두지 않는다. 현재 measure 변환은 서버의 결정적
  `derive_support_scale_measures_v02` 처리 일부이며, 별도 versioned normalizer를 도입할 때만 새 metadata key를 추가한다.
- 이 ID로 Common IR `blocks[]` / `occurrences[]` / `cells[]`를 직접 조인하지 않는다.
- CandidatePack exact-span 기준은 `(candidate_pack_id, value_source.source_block_id)` 및 `start_char` / `end_char`를 사용한다.
- 원 Common IR provenance 조인은 `evidence[].common_ir_document_id`와 `evidence[].common_ir_block_id`를 사용한다. 표 셀은 `common_ir_cell_id`, 원문 occurrence 추적은 `common_ir_occurrence_ids`를 사용한다.

## 5. 공유 비교 어휘

요청서와 Existing의 공통 비교는 아래 `field_name`만 사용한다. 이 목록이 공유 어휘의 전부다.

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

공유 비교는 sparse Fact model로 한다. 한쪽에 값이 없다고 계약 실패가 아니다. 요청서 전용 키(`request_context`, `program_hierarchy`, `field_states`, `delivery_relations`, `delivery_methods`)를 Existing에 강제하지 않는다.

### `beneficiary.subject_role`

`beneficiary`에는 신청자와 정책상 혜택 대상 또는 금전 수령자가 다를 때만 아래 제한 어휘를 선택적으로 사용한다.

| 값 | 의미 |
| --- | --- |
| `financial_recipient` | 원문상 금전·환급·지급을 받는 주체 |
| `policy_beneficiary` | 원문상 고용·서비스·성과 등 정책 혜택의 직접 대상 |

두 역할 모두 원문에서 명시되면 각각 별도 exact-span Fact로 남긴다. 금액·기간·비용 Fact는 실제 금전 수령자가 명시된 경우에만 그 `beneficiary` Fact를 `recipient_fact_ids`로 참조한다. 불명확하면 `null`이며, `subject_role`은 `beneficiary` 외 Fact에 사용하지 않는다.

## 6. Existing 전용 등록 필드

아래는 Existing에 등록된 필드다. 공유 비교 어휘로 올리지 않는다.

| 필드 | 상태 |
| --- | --- |
| `payment_terms` | Existing 전용. 지급·정산 원문 |
| `duplicate_support_conditions` | Existing 전용. 중복수혜·타사업 제한 |
| `applicable_entity` | 일시 미결. 조건 적용 주체로 등록은 유지하되, 의미 품질 회귀 전까지 승격·재분류하지 않음 |

`applicable_entity`를 공유 어휘에 넣거나 다른 대상 필드로 합치지 않는다.

## 7. `delivery_roles`

Existing `delivery_roles`는 변경하지 않는다. 원문에 명시된 기관–역할 raw 관계만 저장한다.

- `canonical_actor_type`을 추가하지 않는다.
- `canonical_role`은 안전하게 매핑될 때만 채우고, 아니면 `null`이다. 원문 역할 표현은 유지한다.
- 문단은 하나의 관계 표현 안에서, 표는 같은 명시 행 안에서만 연결한다.
- 요청서 `delivery_relations` / `delivery_methods`는 이후 비교 계층 어댑터가 대응한다. Existing raw JSON을 요청서 형태로 바꾸지 않는다.

## 8. 이번 계약에서 하지 않는 것

- v0.1 산출물 마이그레이션. 적재는 v0.2 재추출본만 대상으로 한다.
- candidate-span 롤백. exact `value_source` 계약을 유지한다.
- 프로필에 `source_priority_rank` 추가.
- PDF native-only에 HWP/HWPX와 같은 table cell/relation을 강제.
- 요청서 `field_states`를 Existing에 도입.

## 9. 즉시 계약과 보류된 의미 품질 확인

이 노트가 고정하는 즉시 계약 작업은 1–8절이다. lineage 필드 보강, numeric locator 설명, 공유 어휘·전용 필드 경계, 수행체계 어댑터 전제.

아래는 계약 위반이 아니라 다음 의미 품질 단계에서 판정한다.

| 항목 | 이유 |
| --- | --- |
| `target_constraints` 및 Projection 생성 조건 | 현 샘플에서 비어 있는 것이 원문상 불필요인지, 생성 단계 미적용인지 Gold로 확인 |
| count unit 분포 | 선정 수 단위 어휘의 실제 분포와 정규화 품질을 회귀에서 확인 |
| `applicable_entity` 결정 | 승격·재분류·유지 여부는 의미 품질 회귀 이후 |
| PDF blind test | native-only 표·레이아웃 한계가 핵심 A Fact 누락으로 이어지는지 별도 측정 |
