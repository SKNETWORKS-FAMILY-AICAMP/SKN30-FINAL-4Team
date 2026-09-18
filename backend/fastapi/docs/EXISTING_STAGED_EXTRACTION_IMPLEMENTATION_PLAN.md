# Existing Profile 단계형 추출 구현 계획

- 문서 상태: `approved-v2-phase0-ready`
- 기준 브랜치: `backend-rebuild`
- 작성일: 2026-09-18
- 대상: Existing 공고 `Common IR -> Existing Profile v0.2` 의미 구조화
- 비대상: Request Profile, 공개 FastAPI 응답, DB 스키마, 기존 Profile v0.2 저장 계약

## 1. 결정 요약

현재의 단일 `SourceSelectionExtractionV02` LLM 호출을 즉시 폐기하지 않는다. 기존 경로를
`monolith` 기준선으로 유지하면서, 구조 단위 Map과 서버 측 Reduce로 구성한 `staged`
경로를 별도 구현한다.

초기 구현은 `off|shadow`만 허용한다. `shadow`는 우선 오프라인 canary에서만 실행하고
monolith 결과를 바꾸지 않는다. Terra 검증, Luna 비교, 별도 representable pristine
holdout을 모두 통과한 뒤 별도 커밋으로 `active` 모드를 추가한다. 그 전의 `active` 입력은
첫 provider 호출 전에 fail closed한다.

```text
Routed atomic CandidatePack + route-tags sidecar
  -> global native-exact transform
  -> dependency-aware immutable work-unit views + fixed specialist schedule
  -> work-unit fact/component-mention/coverage Map
  -> exact occurrence resolution and stable IDs
  -> deterministic fact/component Reduce, alias/conflict ledger
  -> evidence-bound relation patches + deterministic facet projection
  -> SourceSelectionExtractionV02 synthesis
  -> bounded global candidate attempts; one finalizer call per attempt
  -> exactly one accepted finalized bundle and one Profile assembly
  -> Existing Profile v0.2
```

핵심 원칙은 다음과 같다.

1. 토큰 길이로 원문을 임의 절단하지 않는다.
2. Component inventory를 Fact 발견의 선행 gate로 사용하지 않는다.
3. shard는 최종 Fact ID, `component_decision`, cross-shard edge, facet, measure를 만들지 않는다.
4. 서버가 exact source span을 확인한 뒤에만 stable ID를 발급한다.
5. 동일 원문 span은 최종 v0.2에서 정확히 하나의 Fact만 소유한다. 경쟁 claim은 진단 ledger에
   모두 보존하고, bounded adjudication으로 해결되지 않으면 contract gap으로 실패한다.
6. relation 모델은 Fact를 재작성할 수 없고, 명시적 근거가 있는 ID edge만 제안한다.
7. shard에서는 finalizer를 호출하지 않는다. 전역 candidate attempt마다 canonical finalizer를
   최대 한 번 호출하고, Profile에 사용되는 성공 bundle과 assembly는 각각 정확히 하나다.
8. Gold는 모델 호출이 끝난 뒤 평가기에서만 읽는다.
9. Profile v0.2, 공개 API, DB 계약은 변경하지 않는다.
10. 모든 primary source block과 명시적 목록·표 항목은 selected claim 또는 통제된
    `no_claim` disposition을 가져야 한다. 이 coverage ledger는 의미 정답을 주장하지 않지만
    원문 구역의 무음 누락을 막는다.

## 2. 현재 기준선과 문제 정의

2026-09-18 pristine hard-6 Terra 실행 결과는 다음과 같다.

- 6/6 Profile JSON 생성 성공
- provider 완료 호출 19회
- prompt 1,166,826 tokens
- completion 70,978 tokens
- total 1,237,804 tokens
- Gold fact core 104개
- candidate fact core 92개
- exact `field_name + value_raw` 일치 31개
- 공백·문장부호 정규화 후 일치 36개
- Gold 104개 책임 분해:
  - candidate가 정확히 선택: 31
  - routed source에 있었으나 selector가 미선택: 63
  - pristine Common IR에는 있으나 미라우팅: 1
  - 현재 exact-source 계약으로 표현 불가: 9

따라서 1차 병목은 Common IR이나 router가 아니라, 한 응답에서 Component, 20개 Fact
필드, scope, 3종 관계, facet을 모두 생성하는 source-selection의 의미 선택 부담이다.

현재 dirty-run 참고 산출물 SHA는 다음과 같다.

- pristine input ZIP:
  `24d70a944f43446563a257958fc5b97a5c484e28b5ac653ccf3f2792880d7800`
- candidate ZIP:
  `c6a18c1bd8624aa778429429ebfb3092d961225c3da32e90a60233c831ba4da4`
- canary report:
  `eb9253c1e84e1466f50e18cf0d43a315d1173256e96cb0cc02e0d75dfb9122a7`

candidate/report는 현재 `/tmp`에 있고 실행 당시 작업트리는 미커밋 상태이며 provider 출력도
seeded deterministic하지 않다. 따라서 input SHA만 재입력 pin이고, 위 candidate/report SHA는
과거 실행 진단값일 뿐 clean run에서 동일해야 하는 값이 아니다. staged 구현 전에 현재 15개
tracked 변경을 별도 검증하고 사용자 승인 checkpoint commit으로 고정한 뒤, clean checkout에서
같은 입력·실행 계약으로 새 authoritative baseline을 만들고 새 candidate/report SHA를 발급한다.
대형·민감 산출물은
`.runtime/evaluations/`에 두고, 다음 비민감 trust-root manifest만 Git에 둔다.

- 입력 ZIP 절대 SHA-256
- candidate ZIP SHA-256
- canary report SHA-256
- prompt/schema/model/effort
- logical/physical call counts
- token/latency totals
- clean code commit과 prompt bundle version/hash

baseline 보존 과정에서 원문이나 Gold를 Git에 추가하지 않는다.

## 3. 범위와 비범위

### 3.1 이번 구현 범위

- deterministic structural work-unit 계약과 builder
- staged 전용 내부 Pydantic 계약
- shard exact-anchor admission
- stable Fact ID와 deterministic merge
- span conflict ledger
- Component mention reconciliation
- evidence-bound relation/facet patch 계약
- staged lineage와 prompt bundle version
- 초기 `off|shadow` feature flag와 active 사전 차단 장치
- staged 전용 canary/report/evaluator
- Terra, Luna, hybrid 비교
- 로컬 LLM 연결이 가능한 모델 독립 port
- representable holdout corpus freeze와 multi-corpus comparator I-loader

### 3.2 이번 구현에서 하지 않는 것

- Common IR v1.1.0 형식 변경
- Profile JSON v0.2 형식 변경
- Supabase/PostgreSQL migration
- FastAPI/프론트 응답 변경
- Gold를 prompt, repair payload 또는 work-unit 경계 결정에 사용
- hard-6 문서별 정규식 예외
- staged 구현과 동시에 router 모델을 결정적 router로 교체
- staged 구현과 동시에 로컬 LLM을 기본 모델로 전환
- Existing producer를 polling worker/FastAPI 운영 경로에 바로 연결
- 현재 exact-source 계약으로 표현할 수 없는 Gold 9건을 억지로 합성

## 4. 내부 계약

새 내부 계약은 최종 `SourceSelectionExtractionV02`와 분리한다. 최종 저장 계약은 그대로
유지한다.

### 4.1 `StructuralWorkUnitV1`

필수 필드:

- `unit_id`: content-addressed deterministic ID
- `global_transformed_pack_id`
- `routing_sidecar_sha256`
- `unit_kind`: `section | list | table | mixed`
- `primary_source_block_ids`: unit의 구조적 atomic primary coverage를 나타내는 블록
- `context_source_block_ids`: 읽기 전용 인접 문맥; Fact anchor 소유 금지
- `claim_source_block_ids`: 이 unit에서 claim을 만들 수 있는 atomic/derived 블록
- `lineage_dependency_block_ids`: 검증에 필요한 parent/source-span closure
- `table_ids`
- `route_tags`
- `scheduled_specialists`: 실행 전에 고정된 specialist 목록과 순서
- `coverage_targets`: 서버가 만든 `(coverage_target_id, source block/item identity,
  allowed_field_group, exact source/atomic region)`의 완전집합
- `source_order_start`, `source_order_end`
- `parent_provenance_digest`
- `unit_manifest_sha256`
- `estimated_input_tokens`
- optional `oversized_reason`

불변식:

- 모든 A-routed atomic source block은 정확히 하나의 primary unit에 속한다.
- Fact claim 가능 여부는 `primary_source_block_ids`가 아니라 `claim_source_block_ids`로만
  판단하며, 각 claim-eligible atomic/derived block은 정확히 한 unit에 속한다.
- context 중복은 허용하지만 `context_only=true`로 구분한다.
- unit은 새 `CandidatePack`이 아니라 하나의 검증된 global transformed pack에 대한 immutable view다.
- 목록을 항목 중간에서 절단하지 않는다.
- 표를 임의 셀 수나 token 위치에서 절단하지 않는다.
- Common IR에 검증된 semantic header/footnote 표지가 없으면 이를 추론해 경계로 사용하지 않는다.
- budget을 넘는 indivisible unit은 `oversized_unit`으로 기록하고 Phase 1~2에서는 첫 provider
  호출 전에 fail closed한다. 큰-context/table fallback은 call plan과 A/B manifest를 갱신하는
  별도 versioned 계약으로만 나중에 추가한다.
- `estimated_input_tokens`는 관측과 사전 budget 검사에만 사용하며 구조 경계를 자르는 근거로
  사용하지 않는다. tokenizer 이름과 버전은 manifest에 기록한다.

### 4.2 routing sidecar와 native-exact 후보 정책

현재 hard-6 canary는 `lines+continuations`를 사용한다. 공정 A/B를 위해 staged arm도 같은
후보 universe를 보아야 한다.

1. router 출력에서 `route_tags_by_atomic_block` sidecar를 별도로 보존하고 canonical digest를
   manifest에 기록한다. combined `CandidatePack`만으로 route tag를 복원하려 하지 않는다.
2. routed atomic pack 전체에 native-exact transform을 정확히 한 번 적용한다.
   derived block의 route tag는 모든 atomic parent route tag의 정렬된 결정적 union이다.
3. `native_parent_block_id`와 모든 `source_spans[].source_block_id`로 dependency graph를 만들고
   transitive closure를 계산한다.
4. composite의 parent들이 초기 unit을 가로지르면 해당 unit들을 source order 기준으로
   결정적으로 합친다. 합친 뒤에도 dependency closure가 닫히지 않으면 provider 호출 전에
   fail closed한다.
5. atomic parent와 derived block은 모두 기존 candidate universe와 동일하게 claim 후보가 될 수
   있다. 각 candidate block은 claim-owning unit 하나에만 배정한다. 다른 unit의 dependency
   closure에서 참조될 때만 lineage/context 전용이다. parent나 derived 후보를 조용히 제외하지
   않는다.
6. claim admission 뒤에는 exact/fully-projected atomic range를 ownership key로 사용한다. 같은
   underlying region을 parent/line/composite claim이 함께 주장하면 alias/conflict reducer가
   canonical claim 하나로 닫아야 하며 둘을 최종 V0.2에 함께 넣지 않는다.
7. 현재 `restrict_a_pack_to_*`는 transformed pack 검증에 필요한 parent lineage를 잃어
   validation 자체가 실패하므로 staged transformed pack에는 사용하지 않는다.
8. 동일 입력에서 global transformed pack, routing sidecar, unit view를 재생성해 byte/hash가
   같아야 한다.

### 4.3 `FactSelectionShardV1`

필수 필드:

- `unit_id`, `global_transformed_pack_id`, `unit_manifest_sha256`, `specialist_kind`
- `allowed_fields`
- local `local_claim_id`
- `field_name`, `status`
- exact `value_anchor`
- `context_source_block_ids`
- 허용되는 `semantic_role`, `subject_role`
- local `component_mentions`
- optional `unresolved_component_hint`
- `coverage_dispositions`

금지 필드:

- 최종 `fact_id`
- `component_decision`
- 최종 `primary_component_id`
- `modifies_fact_ids`, `recipient_fact_ids`, `basis_fact_ids`
- `support_facets`
- `support_scale_measures`

서버는 `specialist_kind`별 field allowlist를 강제한다. allowlist 밖 필드는 repair하지 않고
admission 실패로 기록한다.

manifest builder는 Gold 없이 각 specialist에게 노출할 block/명시적 목록·표 항목마다 opaque
`coverage_target_id`를 발급하고 `StructuralWorkUnitV1.coverage_targets`에 완전집합을 고정한다.
`CoverageDispositionV1`은 모델이 target을 만들지 못하며 다음 key만 되돌려준다.

`(unit_id, specialist_kind, coverage_target_id, allowed_field_group)`

각 target에 대해 다음 중 하나만 허용한다.

- `selected`: 하나 이상의 `local_claim_id`
- `no_claim`: allowlist에 정의된 사유 코드와 짧은 비권위적 설명

global coverage reducer는 manifest의 expected key 집합과 shard key 집합이 정확히 같은지
검증하고, 누락·중복·모델이 발명한 target을 모두 거부한다. coverage ledger는 Gold 없이
생성·검증하며, `no_claim`이 사실상 의미 누락을 숨기지 않는지는 사후 평가에서 측정한다.
`selected`가 참조한 claim은 먼저 admission을 통과해야 하며, 그 exact locus 또는 atomic
projection이 서버가 target에 고정한 region과 실제로 겹치거나 포함되어야 한다. region binding이
실패한 claim은 coverage로 세지 않고 admission failure로 처리한다.

동일 anchor 문자열이 block 안에 반복되면 모델이 offset이나 ordinal을 쓰게 하지 않는다.
서버가 exact match마다 opaque `occurrence_candidate_id`를 만든 뒤, focused occurrence resolver가
unit 문맥·field·필요한 local component/role hint만 보고 하나를 선택한다. 서버가 선택을 원문에
재검증한 뒤에만 start/end와 stable ID를 만든다. 첫 occurrence fallback은 금지하며, 미해결은
admission failure다. 이 절차는 Fact `value_anchor`뿐 아니라 organization/role anchor와 Component
`name_anchor`에도 적용한다. 다만 후자의 세 anchor는 position override 계약이 없으므로 서버가
고른 block/native line atom 안에서 anchor 문자열이 정확히 한 번 나타나는 경우만 admission한다.
모든 exact anchor는 shard admission에서 unique occurrence 또는 지원되는 override로 검증돼야
한다. 이 단계의 component/role hint는 occurrence 식별용일 뿐 최종 관계가 아니다.

`delivery_roles`는 일반 Fact claim에 억지로 넣지 않고 `DeliveryRoleClaimV1`으로 분리한다.
이 계약은 Fact 자체의 exact `value_anchor`, exact organization/name anchor, exact role anchor,
canonical role enum, 그리고 이 anchor block들을 포함하는 `context_source_block_ids`를 가진다.
delivery organization은 notice-scoped 수행기관이지 support Component가 아니므로 local Component
mention ID를 요구하거나 support Component로 소유시키지 않는다. 서버가 기존 V0.2의
`organization_anchors`, `role_anchor`, `canonical_role`로 검증 가능한 경우에만 합성한다.

### 4.4 specialist 경계

구조 단위가 1차 분할이고, specialist는 각 unit 내부의 vocabulary 축소 수단이다.

- `global_program`: `purpose_goal`, `program_period`; dedicated delivery specialist가 배정되지
  않은 경우에만 `delivery_roles`
- `target_rules`: applicant/target/eligibility/beneficiary/applicable entity/exclusions/duplicate
- `support_bundle`: support activities/methods/items/content/scale/support period/total budget
- `obligation_terms`: cost sharing/payment terms/participation requirements
- `delivery_roles`: manifest builder가 조직/역할 marker를 결정적으로 검출한 unit에만 실행

`support_content`와 `support_scale`을 기계적으로 분리하지 않는다. 같은 benefit bundle의
수혜자, 항목, 금액, 기간이 한 unit에서 함께 보존돼야 한다.

specialist 실행 여부와 순서는 `scheduled_specialists`에 고정한다. 런타임 모델이 “조밀함”을
자의적으로 판단해 호출 수를 바꾸지 않는다. manifest는 `field_producer_by_field`를 함께
고정하고, dedicated `delivery_roles`가 scheduled되면 `global_program` allowlist에서 해당 field를
제외한다. 각 `(unit, field)`의 producer는 정확히 하나여야 한다.

### 4.5 stable ID, alias와 span conflict

Fact ID는 모델이 만들지 않는다. exact anchor occurrence를 materialize한 뒤 다음 canonical
tuple로 서버가 생성한다.

```text
fact-id-v1(
  common_ir_source_sha256,
  source_block_id,
  start_char,
  end_char,
  field_name
)
```

canonical UTF-8 JSON serialization과 hash algorithm을 versioned contract로 고정한다.

atomic, line, composite가 같은 원문을 표현할 수 있으므로
`project_value_source_to_atomic_ranges()`로 완전히 투영 가능한 claim은 정렬된 atomic range
집합으로 alias key를 만든다. composite separator 등으로 완전 투영할 수 없으면 local locus를
유지하고 자동 alias 병합하지 않는다. 여기서 alias 동일성은 partial/nested overlap이 아니라
완전히 투영된 atomic range 집합의 equality다. 완전 투영 불가 claim이 동일 field/value로 다른
claim과 source region을 겹쳐 alias 검사를 우회할 가능성이 있으면 conflict ledger와 bounded
adjudication으로 보내며, 서로 다른 값을 가진 정당한 nested Fact를 일괄 삭제하지 않는다.

동일 locus/field에 대해 exact `value_raw`, `status`, `semantic_role`, `subject_role`까지 같은
경우만 deterministic duplicate로 병합한다. local component hint는 reconcile 전에는 claim의
동일성 metadata가 아니며 Component ownership proposal ledger에서 별도로 병합한다. 위 claim
metadata가 하나라도 다르면 `ClaimConflictV1`이다. 동일 원문 span에 서로 다른 field가 경쟁하는
경우도 `SpanConflictV1`에 모두 남긴다. 현 V0.2 uniqueness 계약상 최종 accepted set에는 source
span당 하나의 Fact만 남아야 한다.

alias duplicate의 evidence representative와 Fact ID는 shard 도착 순서로 선택하지 않는다.
`evidence-representative-v1`의 고정 total order
`(origin_rank, source_order, source_block_id, start_char, end_char)`를 사용한다. origin rank는
`native_composite < native_line_atom < atomic`으로 versioned contract에 고정한다. 선택된 canonical
representative로 Fact ID를 한 번 계산하고, 나머지 alias evidence는 diagnostic lineage로 남긴다.

- 무음 자동 삭제 금지
- field 우선순위 하드코딩 금지
- 근거 없는 multi-label 허용 금지
- focused adjudication은 원문 unit, competing claim, 허용 field만 받고 최대 한 번 결정을 반환
- 미해결 경쟁 또는 현 Profile 계약으로 표현 불가능한 진짜 다중 의미는 contract gap으로
  분류하고 해당 candidate attempt를 실패시킨다.
- 진단 ledger에는 탈락 claim과 사유를 모두 보존한다.

### 4.6 Component reconciliation

각 unit은 Component를 확정하지 않고 다음 `ComponentMentionV1`만 제안한다.

- `local_component_mention_id`
- exact `name_anchor`
- `evidence_source_block_ids`
- optional `proposed_kind`
- optional `scope_hint`

서버는 exact name occurrence, source provenance, structural adjacency로 mention을 병합한다.

- Component ID는 `component-id-v1(source_sha256, canonical evidence fingerprint)`의 versioned
  canonical hash로 서버가 생성한다.
- `component_kind`는 identity key로 사용하지 않고 reconcile 결과로 결정
- 새로운 mention을 기존 catalog 부재만으로 버리지 않는다.
- `component_decision`은 reconciled Component 종류에서 서버가 유도한다.
- 모순되는 kind/ownership은 conflict ledger와 최대 1회 focused adjudication으로 보낸다.
- current V0.2 validator보다 강한 staged reducer 검사를 두어 component kind,
  `component_decision`, Fact ownership의 정합성을 확인한다.

### 4.7 relation/facet patch

관계 단계 입력은 materialized Fact card, reconciled Component, exact evidence card, 필요한
동일 unit/인접 unit 문맥이다. 원문 전체나 Gold를 보내지 않는다.

`RelationPatchV1`은 다음만 반환할 수 있다.

- `patch_id`
- 기존 materialized Fact/Component ID
- `primary_component_id`, `applicability_component_ids`
- `modifies`, `recipient`, `basis` edge와 edge type
- 관계를 지지하는 source block/evidence ID

Fact의 field, value, anchor, status는 수정할 수 없다. proximity만으로 생성된 edge는 거부한다.
Fact가 많으면 Component별로 분할하고 notice-wide 공통 cap/조건만 별도 reconcile한다.

각 edge는 predicate별로 허용된 source Fact field/type을 만족하고, edge 양 끝과 관계 자체를
지지하는 evidence가 있어야 admission된다. dangling ID, evidence laundering, 관계 단계의 Fact
재작성은 즉시 거부한다.

`support_facets.activities/methods/items`는 모델이 자유 문자열로 쓰지 않는다. 확정된 exact
Fact 값과 field에서 서버가 versioned allowlist projection으로 생성한다. 기존 계약상 모델
분류가 꼭 필요한 facet은 enum만 반환하게 하고 source Fact ID를 의무화한다. facet `status`는
projection 결과 존재 여부와 source Fact status를 입력으로 하는 versioned deterministic mapping으로
서버가 설정한다.

### 4.8 global finalization

부분 shard에는 exact-anchor 존재, field allowlist, schema 같은 per-candidate admission만 적용한다.
다음 전역 검사는 Reduce 이후 한 번만 실행한다.

- exact materialization 최종 확인
- value-source uniqueness
- semantic duplicate normalization
- explicit list completeness
- support cap completeness
- Component structure
- condition variant relations
- support scale measure derivation/validation

finalizer 전에 pre-finalizer coverage gate를 통과해야 한다. 모든 primary block과 명시적
list/table/cap 항목이 accepted claim 또는 통제된 `no_claim` disposition을 가져야 하며,
conflict와 contract gap이 0이어야 한다.

최종 reducer는 기존 `SourceSelectionExtractionV02` candidate를 만든다. staged 경로는
`apply_finalize_with_fallback_v02()`를 그대로 사용하지 않는다. 해당 helper는 한 호출 안에서
canonical finalizer를 두 번 실행할 수 있기 때문이다.

- shard finalizer 호출: 0
- global candidate attempt: 최대 2회
- candidate attempt당 `finalize_source_selection_v02()` 호출: 최대 1회
- 첫 실패의 targeted repair는 새 candidate attempt를 만들며 repair budget은 1
- accepted run은 `successful_finalizer_count == 1`, `assembled_bundle_count == 1`
- 실패 run은 `successful_finalizer_count == 0`, `assembled_bundle_count == 0`

staged occurrence resolver가 확정한 모든 position은
`finalize_source_selection_v02(..., value_source_overrides=...)`로 주입한다. finalizer가 anchor text를
다시 검색해 다른 occurrence를 선택하게 두지 않으며, 최종 `value_source`가 staged span과 정확히
같은지 검사한다. synthesized `SourceSelectionExtractionV02.candidate_pack_id`와 finalizer에 넘기는
pack은 모두 global transformed pack이다. pre-transform routed pack ID는 lineage로만 보존한다.

targeted repair가 기존 `do_not_restore_fact_ids` 같은 final Fact ID 계약을 사용하면 reducer의
`local_claim_id -> canonical fact_id` ledger를 통해서만 변환한다. raw local ID를 finalizer repair
계약에 직접 넘기지 않는다.

모든 attempt와 validation 분류·개수, repair 범위·결과를 audit report에 기록한다. source-free
report에는 allowlist된 stage, error class/code, count만 기록한다. raw exception 문자열,
Fact/block ID, anchor와 source text는 영속화하지 않는다. 두 attempt가 모두 실패하면 staged
run은 실패하며 monolith 결과로 조용히 대체하지 않는다.

## 5. orchestration과 rollout

### 5.1 feature flag

```text
PREREVIEW_EXISTING_STAGED_EXTRACTION_MODE=off|shadow
```

- 기본값: `off`
- 알 수 없는 값: fail closed
- `off`: staged import, 추가 call/artifact/lineage 없이 현 monolith 경로와 byte-compatible
- `shadow`: 초기에는 `run_existing_staged_canary.py`의 오프라인 실행만 의미한다. production
  요청 경로·큐·SLO에는 영향을 주지 않으며 전용 Gold 밖 output dir에 비교 artifact만 저장한다.
- `active`: 초기 enum과 배포 설정에 존재하지 않는다. Phase 6 gate를 통과한 별도 커밋에서
  추가한다. 그 전의 문자열 입력은 provider 호출 전에 실패한다.

현재 Existing producer seam은 script/canary 중심이며 polling worker/FastAPI 운영 경로에 연결된
production producer가 아니다. 따라서 초기 shadow는 배포 rollout이 아니라 오프라인 품질
실험이다. 향후 production shadow를 연결하려면 별도 queue, budget, deadline, artifact sink,
failure isolation 계약을 먼저 추가한다.

active 도입 후에도 실패를 monolith로 조용히 fallback하지 않는다. rollback은 명시적으로
`off`를 적용해 monolith로 되돌리고 audit reason을 남긴다. operational path에서는 환경변수
`off`가 explicit/cached mode보다 항상 우선하는 kill switch다. explicit mode override는
오프라인 test/canary에서만 허용하고 운영 caller가 `active`를 직접 강제하지 못하게 한다.

### 5.2 모델 profile

현재 adapter는 model-profile map을 받지만 `announcement_profiles` caller의 task별 선택과
client-wide effort/token cap 분리가 충분하지 않다. staged 구현 전에 task별 client/config
dispatch seam을 추가하고 기존 monolith 호출 pin은 바꾸지 않는다. 모델은 staged 코드에
하드코딩하지 않는다.

- map model profile
- component/relation reduce model profile
- focused repair/adjudication model profile
- repeated-occurrence resolution profile 또는 adjudication profile 내 전용 task

비교 순서:

1. all-Terra
2. all-Luna
3. Luna Map + Terra Reduce/repair
4. 이후 동일 shard 계약의 local LLM Map

모든 A/B는 model 이외의 input ZIP, routed pack, routing sidecar, global transformed pack,
work-unit/base specialist schedule, prompt/schema hash, reasoning effort, token cap, concurrency,
retry budget과 downstream partition algorithm/threshold를 동일하게 고정한다. 최초 비교 effort는
`medium`이다.

실행 전 deterministic dry-run으로 다음을 계산한다.

- unit 수와 `scheduled_specialists`
- stage/specialist별 logical call 수
- reduce/adjudication/finalization 후보 attempt 최대 수
- repeated-occurrence resolver의 notice별 logical/physical call cap
- notice/run별 hard logical·physical call budget과 repair budget
- task별 payload bytes와 tokenizer/version 기반 estimated tokens

hard budget을 넘으면 첫 provider 호출 전에 실패한다. Terra/Luna/hybrid는 같은 base manifest,
Map call plan, downstream partition algorithm과 hard budget envelope를 사용하고 task별 model
profile만 바꾼다. Map 결과에 따라 Component 수, relation partition, focused adjudication 여부가
달라질 수 있으므로 실제 downstream call plan이 byte-identical하다고 주장하지 않는다. reducer는
materialized 결과를 받은 뒤 고정 정렬·threshold로 downstream plan을 만들고 첫 downstream
provider 호출 전에 그 plan/hash와 조건부 call 사유를 기록한다. 이 모델 유발 분기와 실제 비용도
end-to-end 비교 결과에 포함한다.

단일 GPU local runtime에서 논리적 병렬과 물리적 동시 실행을 분리한다. 기본 물리
concurrency는 1이며, 2 이상은 KV cache/OOM/throughput benchmark 후 허용한다.

### 5.3 prompt와 lineage

기존 monolith `PROMPT_BUNDLE_VERSION`을 변경하지 않는다. staged 전용
`STAGED_PROMPT_BUNDLE_VERSION`을 만든다.

lineage에는 다음을 남긴다.

- pipeline variant와 version
- parent CandidatePack/Common IR identity, route-tags sidecar와 global transformed pack hash
- unit manifest version/hash
- deterministic specialist schedule와 pre-call budget plan hash
- stage별 prompt/schema hash
- 요청 model profile과 provider가 실제 사용한 model identity
- reasoning effort, token limit, retry budget
- shard logical/physical attempt와 safe failure code
- merge/conflict/finalizer version

원문 prompt/response와 비밀값은 artifact에 기록하지 않는다.

## 6. 구현 단계와 gate

### Phase 0. 기존 상태 보존

- 현재 dirty worktree와 untracked 디렉터리를 보존한다.
- 기존 15개 수정 파일을 임의 revert/overwrite하지 않는다.
- 기존 15개 변경은 staged 구현과 섞지 않는다. 관련 테스트를 통과한 뒤 사용자에게 commit
  메시지를 확인받아 checkpoint commit으로 고정한다.
- checkpoint의 source-selection prompt bundle, schema, model/effort pin을 정확히 기록한다.
- pristine hard-6 Terra baseline을 clean checkpoint checkout에서 같은 input/실행 계약으로 다시
  실행하고 새 authoritative candidate/report SHA를 발급한다.
- 대형 artifact는 `.runtime/evaluations/`에 두고, SHA와 비민감 실행 계약만
  `backend/baselines/existing_profile/`의 작은 manifest로 commit한다.
- `git diff --check`, 관련 compile/test 결과와 baseline clean commit을 기록한다.

Gate 0:

- input ZIP SHA `24d70a94...` 재확인
- clean run이 새로 발급한 authoritative candidate/report SHA와 manifest 검증
- clean checkout에서 재현 가능한 baseline과 실제 prompt bundle pin
- Gold/adjudication 입력 부재 확인
- flag-off 기존 경로 변화 0

### Phase 1. LLM-free structural preflight

- `StructuralWorkUnitV1`과 builder 구현
- route-tags sidecar와 global native transform 구현
- dependency-aware atomic/native-derived ownership과 lineage 구현
- 모든 A block primary coverage 검사
- context-only 중복 검사
- list/table 경계 fixture
- oversized unit fail-closed 검사
- 현재 candidate와 Gold를 사후 투영하여 다음 ledger 생성:
  - `D_gold`: 전체 Gold
  - `D_I`: pristine I에서 exact evidence 표현 가능
  - `D_unit`: evidence와 필수 문맥이 하나의 unit에 보존
  - `D_selector`: 실제 shard 입력에 노출

Gate 1:

- source primary coverage 100%
- silent drop 0
- unit manifest deterministic/byte stable
- route-tags sidecar와 global transformed pack deterministic/byte stable
- global transformed pack의 claim-eligible atomic/derived block ownership coverage 100%, 중복 0
- representable Gold span 절단 0
- unresolved/cross-unit native dependency 0
- Gold가 builder 입력에 사용되지 않았음을 테스트로 보장

### Phase 2. 내부 shard 계약과 admission

- `FactSelectionShardV1`, `CoverageDispositionV1`, `DeliveryRoleClaimV1`
- `ComponentMentionV1`, `RelationPatchV1`, `ClaimConflictV1`, `SpanConflictV1`
- allowed-field admission
- repeated-anchor occurrence candidate 생성·focused resolution·materialization
- versioned Fact/Component stable ID
- atomic-range alias와 metadata-aware deterministic duplicate merge
- 동일 span 다중 field conflict fixture
- delivery-role V0.2 synthesis fixture
- deterministic facet projection fixture
- staged lineage schema

Gate 2:

- unsupported source/field 0
- 무음 conflict 삭제 0
- repeated occurrence 미해결 0
- same-span final ownership 위반 0
- 입력 순서 변경에도 동일 ID/merge 결과
- atomic/line/composite 입력 순서 permutation에도 동일 canonical evidence와 Fact ID
- partial shard가 final Profile 계약으로 오인되지 않음

### Phase 3. Terra fact-only shadow

- 오프라인 canary의 authoritative 비교 대상은 여전히 monolith
- staged Map은 Fact/Component mention까지만 수행
- relation/facet/final Profile은 아직 생성하지 않음
- hard-6의 `D_selector` 기준 core precision/recall 측정
- stage/unit별 token, latency, retry, cost 기록
- 첫 호출 전 dry-run call/token budget 검증

구조 viability 기준:

- 현행 pristine baseline 대비 selector recall `+10 percentage points` 이상
- shard exact-anchor materialization 98% 이상
- unsupported fact 0
- selector precision 하락 2pp 이하
- mandatory list/table boundary fixture 전부 통과

Phase 4 진입 기준은 위 viability와 별도로 기존 미선택 63개 중 최소 32개 추가 회수다.
둘 중 하나가 아니라 둘 다 만족해야 한다. 미달이면 관계 단계를 구현하지 않고
work-unit/specialist prompt를 수정한다. 98%는 shard-level 진단 지표이고, final accepted
Profile의 anchor/materialization은 100%여야 한다.

Gate 3:

- deterministic call plan과 hard budget 준수
- viability와 Phase 4 진입 기준 모두 충족
- coverage ledger 누락 0

### Phase 4. deterministic Reduce와 relation/facet patch

- Component mention reconcile
- component decision 유도
- evidence-bound edge candidate 생성/선택
- deterministic facet projection
- current V02 합성
- candidate attempt당 canonical finalizer 최대 1회
- validation failure를 unit/stage로 귀속한 최대 1회 targeted repair/new attempt

Gate 4:

- dangling ID 0
- relation without explicit evidence 0
- Component/관계/지원규모 precision·recall이 monolith 이하로 하락하지 않음
- accepted run은 `successful_finalizer_count == 1`, `assembled_bundle_count == 1`
- candidate attempt 2 이하, attempt당 finalizer 호출 1 이하
- accepted Fact의 exact materialization 100%, unresolved conflict/contract gap 0
- Profile v0.2 schema/API/DB 변화 0

### Phase 5. Terra/Luna/hybrid 비교

공정 A/B:

| 구성 | 목적 |
|---|---|
| all-Terra | staged 품질 상한 |
| all-Luna | 비용 하한 |
| Luna Map + Terra Reduce/repair | 우선 배포 후보 |

Luna 잠정 채택 기준:

- Terra 대비 selector recall 하락 3pp 이하
- exact materialization 성공률 하락 1pp 이하
- unsupported fact 0
- schema/repair/timeout 실패율이 Terra의 1.25배 이하
- 실제 추정 비용이 Terra의 25% 이하
- p95 end-to-end latency가 Terra의 1.5배 이하

가격 계산은 실행 시점 OpenAI 공식 price card version과 함께 저장하고 invoice가 아니라
estimate임을 명시한다.

### Phase 6. pristine holdout, 운영 shadow와 active 전환

hard-6는 contract gap 9건이 포함된 개발·진단 세트로만 사용하며 strict `C == G` active
gate로 쓰지 않는다. 현 exact-source 계약으로 100% 표현 가능한 문서만으로 형식·구조를
층화한 최소 20건 semantic holdout을 사전 등록하고 봉인한다.

- PDF/HWP/HWPX
- 표/목록 유무
- 단일/다중 Component
- 지원금액·비율·기간 관계
- 수행기관 관계

Phase 6 사전조건으로 holdout 원문에서 pristine Common IR을 새로 생성하고, 최소 20건의
representability를 독립 검수한 Gold/oracle, freeze manifest, input SHA pin을 만든다. hard-6의
6건/고정 SHA 전용 I-loader를 재사용하지 않고 multi-corpus manifest를 받도록 comparator I-loader
계약을 확장한다. 이 holdout input/freeze/comparator preflight가 통과하기 전에는 semantic
holdout 실행이나 active 판정을 시작하지 않는다.

Holdout preflight는 실제 representable 문서가 20건 이상이고 위 형식·표/목록·Component·금액·기간·
수행기관 strata가 manifest에 모두 포함됐음을 검증한다. 수량이나 strata가 부족하면 active gate는
평가 없이 실패한다.

Gold를 보는 것 자체는 leakage가 아니지만, holdout 결과를 보고 prompt/rule을 수정하면
해당 holdout은 폐기하고 새 집합을 등록한다. 별도로 Gold가 없는 operational shadow corpus를
두어 latency, 비용, failure mode만 본다.

active gate:

- representable sealed holdout에서 위 품질 기준과 B/G/I/C strict gate 통과
- hard-6는 `D_gold/D_I/D_unit/D_selector`와 contract gap attribution 보고 통과
- strict graph 차이 원인 전부 attribution 가능
- 비용/latency/runtime resource budget 통과
- production shadow를 연결할 경우 별도 queue/deadline/budget/sink/failure isolation 검증
- `off` rollback rehearsal 통과
- Profile body뿐 아니라 processing metadata, lineage, source-selection artifact,
  오류·fallback·운영 동작까지 compatibility matrix 통과
- 운영 문서 및 환경변수 예제 현행화

이 gate를 통과한 뒤에만 `active` enum/config/authoritative path를 별도 커밋으로 추가한다.

## 7. 테스트 전략

### 7.1 단위 테스트

- work-unit coverage/order/determinism
- list/table/adjacent context envelope
- routing sidecar replay와 native dependency closure/ownership
- oversize fail-closed
- shard allowed fields
- value/organization/role/component-name exact-anchor occurrence resolution
- finalized `value_source`와 staged `value_source_overrides` span 동등성
- stable ID determinism
- atomic-range alias, metadata-aware duplicate merge와 claim/span conflict
- server-issued block/item coverage target와 selected-claim region binding
- unit/field별 producer exactly-one
- delivery-role V0.2 synthesis
- Component mention reconciliation
- relation evidence admission
- deterministic facet projection
- lineage serialization/hash
- finalizer attempt/success/assembly count
- Gold access ordering

### 7.2 통합 테스트

- fake LLM으로 Map -> Reduce -> V02 -> finalizer
- shard 순서를 뒤섞어도 동일 결과
- 한 shard timeout/invalid schema/repair budget 소진
- monolith `off` byte compatibility
- `shadow`가 최종 Profile을 변경하지 않음
- 초기 구현에서 `active`가 provider 호출 전에 fail closed
- active 도입 후 fail-closed 및 `off` rollback
- 운영 caller의 explicit/cached `active`보다 환경변수 `off` kill switch가 우선

다음 falsification fixture를 필수로 둔다.

1. 같은 exact span을 서로 다른 field가 주장한다.
2. continuation/list split 때문에 하나의 의미 단위가 둘로 보인다.
3. parent와 derived/composite가 서로 다른 unit에 중복된다.
4. transformed pack에 기존 restrict helper를 적용하면 lineage가 깨진다.
5. 같은 이름이지만 서로 다른 Component kind 제안이 충돌한다.
6. 관계 patch가 근거 ID를 빌려 evidence laundering을 시도한다.
7. 일부 표/목록만 처리했는데 coverage가 완료로 보이는 false completeness가 생긴다.
8. production shadow가 queue/SLO를 침범한다.
9. dirty 실행과 clean checkpoint 실행 결과가 달라진다.
10. semantic holdout 결과를 보고 tuning한 뒤 같은 holdout을 재사용한다.
11. Luna Map이 Fact recall을 높이지만 relation 품질을 떨어뜨린다.
12. flag-off인데 lineage/metadata 때문에 Profile 또는 artifact byte가 달라진다.
13. oversized indivisible unit이 무음 절단되거나 budget 밖 provider call을 만든다.

### 7.3 corpus/eval

- pristine hard-6 development
- staged fact-only Terra
- Terra/Luna/hybrid 동일 manifest A/B
- 사전 등록 pristine holdout
- Gold 없는 operational shadow corpus
- 기존 Gold100 comparator는 calibration으로 유지하고 live 품질 증명으로 오해하지 않음

## 8. 관측 보고서

staged report는 raw exception, Fact/block ID, anchor, source text 없이 allowlist된 분류·집계만
다음처럼 남긴다.

- run/input/unit/prompt/schema/model hashes
- routing sidecar/global transformed pack/specialist schedule/budget-plan hashes
- task별 model profile, 실제 model identity, effort, concurrency, retry budget
- logical/physical calls와 safe outcome
- input/cached/output/reasoning tokens
- 272K 장문 surcharge 여부
- stage/unit p50/p95와 end-to-end latency
- estimated price card와 비용
- `D_gold/D_I/D_unit/D_selector`
- fact/component/relation/facet/support-scale precision/recall
- drop attribution:
  - absent in I
  - split boundary
  - routing/exposure
  - model unselected
  - admission rejected
  - reducer conflict/drop
  - finalizer rejected
- coverage disposition과 conflict/contract-gap 원인
- `finalizer_attempt_count`, `successful_finalizer_count`, `assembled_bundle_count`

staged report CLI는 성공/품질 gate 실패/계약·runtime 실패를 서로 다른 exit code로 반환한다.
비용 계산은 cached input을 포함한 공식 price-card version을 저장하고 실제 invoice와 구분한다.

## 9. 파일 배치 초안

새 orchestration/계약은 re-vendor 시 덮어쓰이지 않도록 `backend/worker` 아래 둔다. 기존 vendor
CandidatePack·finalizer는 import해 사용하고, 필수 공통 primitive 수정만 vendor upstream과 함께
별도 검토한다.

- `backend/worker/staged_existing/contracts.py`
- `backend/worker/staged_existing/work_units.py`
- `backend/worker/staged_existing/materialize.py`
- `backend/worker/staged_existing/reduce.py`
- `backend/worker/staged_existing/orchestrator.py`
- `backend/worker/staged_existing/model_profiles.py`
- `backend/baselines/existing_profile/*.json`
- `backend/scripts/validate_existing_staged_units.py`
- `backend/scripts/run_existing_staged_canary.py`
- `backend/tests/test_existing_staged_contracts.py`
- `backend/tests/test_existing_staged_work_units.py`
- `backend/tests/test_existing_staged_reduce.py`
- `backend/tests/test_existing_staged_canary.py`

기존 `announcement_profiles.py`에는 mode 선택과 최종 호출 seam만 최소 추가한다. task별 client
dispatch가 필요한 adapter/port 변경은 monolith 기본값과 기존 prompt pin을 보존한다.

## 10. 커밋 경계

0. 현재 15개 변경 검증 후 사용자 승인 checkpoint commit
1. `test(profile): staged 구조 단위와 사전 검증 계약 추가`
2. `feat(profile): routing sidecar·native dependency 단위 구성 추가`
3. `feat(profile): staged claim 계약과 결정적 병합 기반 추가`
4. `feat(profile): task별 모델 profile과 오프라인 shadow 실행 추가`
5. `test(profile): staged Terra 의미 회귀와 보고서 추가`
6. `feat(profile): staged relation 조립과 bounded 최종화 연결`
7. `test(profile): Terra·Luna·hybrid 비교 gate 추가`
8. `test(profile): representable holdout freeze와 multi-corpus 비교 gate 추가`
9. `feat(profile): 검증된 staged active 모드 추가` — Phase 6 통과 후에만
10. `docs(profile): 단계형 추출 운영·롤백 절차 정리`

각 커밋은 별도 테스트를 통과해야 하며, push는 사용자 명시 승인 후에만 수행한다.

## 11. 구현 시작 전 정합성 gate

다음 질문에 모두 `pass`여야 Phase 1 구현을 시작한다.

- 구조 unit 계약이 Gold를 보지 않고 결정적인가?
- route tag를 sidecar에서 재생할 수 있고 global native transform이 한 번만 실행되는가?
- 모든 native dependency가 닫히고 claim-owning 후보가 정확히 한 unit에 귀속되는가?
- repeated anchor occurrence가 first-match 없이 exact span으로 확정되는가?
- 동일 span 다중 의미가 ledger에 남고 final accepted ownership이 하나로 닫히는가?
- shard가 최종 ID/관계/Component 결정을 선점하지 않는가?
- delivery role, Component, relation, facet을 V0.2로 합성할 계약이 완결됐는가?
- relation 단계가 Fact를 재작성하지 못하는가?
- finalizer가 attempt당 한 번 이하이고 성공 bundle/assembly가 각각 하나인가?
- flag-off에서 extra call/artifact/lineage와 공개 Profile/API/DB 변화가 모두 0인가?
- monolith와 staged의 prompt/lineage가 독립 버전인가?
- deterministic specialist schedule과 pre-call hard budget이 있는가?
- Terra/Luna A/B가 task별 model 외 변수를 고정하는가?
- hard-6, representable sealed holdout, unlabeled operational corpus의 역할이 분리되는가?
- dirty worktree 변경이 clean checkpoint로 재현·격리되는가?
- 초기 shadow가 오프라인 전용이고 production side effect가 없는가?
- rollback이 환경변수 하나로 가능한가?

하나라도 fail이면 구현하지 않고 계획을 수정한다.

## 12. 리뷰 반영 기록

2026-09-18에 Opus 5 xhigh와 Sol xhigh의 설계 리뷰, Grok의 red-team 검토를 수행했다.
세 검토 모두 v1에는 구현 차단 이슈가 있다고 판단했다. v2는 다음을 수용했다.

- routing sidecar -> global native transform -> dependency-aware unit view 순서
- repeated anchor occurrence resolution과 metadata-aware alias/conflict 계약
- Component/delivery-role/relation/facet 내부 schema 보강
- coverage ledger와 13개 falsification fixture
- finalizer의 attempt/success/assembly 계수 분리
- initial `off|shadow`, offline-only shadow, active 별도 gated commit
- clean checkpoint baseline과 repo 내 비민감 trust-root manifest
- task별 model client/config, deterministic specialist schedule, pre-call hard budget
- hard-6 개발 진단, representable sealed semantic holdout, unlabeled operational corpus 분리
- Profile 본문 외 metadata/lineage/artifact/error/운영 동작 compatibility matrix

v2는 Opus 5 xhigh, Sol xhigh, 코드 표면 감사와 Grok red-team의 최종 정합성 재검토에서
모두 `PASS`, 구현 차단사항 0건 판정을 받았다. 따라서 Phase 0부터 시작할 수 있다.
