# PDF Primary Document View 설계

- 상태: 외부 설계 검토 반영 및 A4.0·A4.1·A4.2·A4.3a·A4.3b 구현, 실제 artifact human-confirmed replay 완료
- 기준일: 2026-09-19
- 대상: native PDF occurrence를 누락·중복 없이 배치하는 평가용 문서 구조
- 범위: Common IR, selector, worker queue, DB, 공개 API는 변경하지 않는다.

## 1. 배경

`pdf_context_groups/v1`은 원문을 임의로 합치지 않는 안전한 구조 가설이다. 그러나
114788 replay에서 101개 group 중 93개가 단일 occurrence였고, 엄격한 ODL·Surya
fragment consensus로 승인된 문단은 0개였다. 표 영역은 찾았지만 행·열 관계가 없고,
문단 연속성과 다이어그램 edge도 복원하지 못했다.

다음 단계의 목표는 이 가설을 바로 Common IR로 승격하는 것이 아니다. 사람 검토용
Gold, 자동 생성 candidate, 두 결과를 비교하는 evaluation을 분리하고, candidate에서
모든 substantive native occurrence를 정확히 한 번 배치하는 것이다.

## 2. 세 계약의 분리

### 2.1 `pdf_primary_structure_gold/v1`

이미지와 native occurrence를 사람이 함께 검토해 확정하는 부분 구조 정답이다.

- source PDF SHA-256, 사람이 검토한 canonical page render SHA-256, strict native capture의
  schema version·canonical SHA·extractor version과 reviewed page scope에 결속한다. replay는
  `pdf_render_manifest/v1`의 source·page·renderer·coordinate·PNG lineage 전체를 먼저
  검증한 뒤 Gold의 page render SHA와 교차 검증한다.
- reconstruction plan, ODL, Surya, fragment/context artifact digest에는 결속하지 않는다.
  occurrence namespace 때문에 strict native capture에는 결속하지만, ODL·Surya 같은 구조
  parser 결과와는 독립된 정답이어야 한다.
- semantic text나 raw separator를 저장하지 않고 occurrence ID, 순서, 구조 관계와
  `join_class`만 저장한다.
- ODL·Surya·context group ID를 정답 근거로 사용하지 않는다.
- 각 reviewed scope는 `pending_human_confirmation` 또는 `human_confirmed` 상태를 갖는다.
  pending fixture는 schema·canonical determinism과 artifact replay 진단에만 사용할 수 있고,
  candidate와의 구조 비교는 수행하지 않는다. evaluator 결과는 `not_evaluable`이며
  회귀·품질 gate를 통과시킬 수 없다.
- artifact 내부의 `human_confirmed`·reviewer·timestamp는 승인 주장일 뿐 trust anchor가
  아니다. 모든 scope가 확인되고 canonical Gold SHA가 코드 리뷰를 거쳐 별도 allowlist에
  고정되어야 한다. Python 내부 replay receipt는 협력 코드의 실수 방지용일 뿐 위조 불가능한
  capability가 아니다. 따라서 공개 quality-gate evaluator가 candidate와 Gold를 같은
  source/native/render 입력에서 매 호출마다 직접 replay한 경우에만 `evaluable`로 판정한다.
- 일부 region만 검토할 수 있으며, 이를 전체 문서 coverage로 해석하지 않는다.
- physical page index와 printed page label 또는 `printed_label_absent`를 구분한다.
- JSON Schema는 교환 형식의 1차 검증이며 단독 admission 경계가 아니다. 실제 날짜,
  canonical 정렬, occurrence ownership, artifact replay 같은 의미 불변식은 Python strict
  validator까지 통과해야 한다.

기존 `pdf_structural_gold/v1`은 121019 한 건의 semantic text와 상수를 포함하는 legacy
fixture다. 이를 수정하거나 일반화하지 않고 새 계약을 별도로 만든다.

### 2.2 `pdf_primary_document_view/v1`

Gold를 보지 않고 strict native occurrence와 검증된 구조 sidecar에서 결정적으로 생성하는
candidate다.

- builder의 입력·import graph·환경변수·디렉터리 탐색에 Gold 경로나 Gold node를 포함하지
  않는다. evaluator만 Gold를 열 수 있다.
- authoritative occurrence set은 source replay를 통과한 reconstruction plan ledger의
  `substantive`·`owned_atomic` occurrence다. 114788에서는 정확히 218개다.
- context group이나 ODL·Surya candidate에서 occurrence를 열거하지 않는다. 이 artifact는
  경계, veto, proposal 또는 독립 corroboration 신호로만 사용한다.
- 모든 authoritative occurrence는 하나의 leaf node에 정확히 한 번 배치한다.
- 구조를 확정하지 못한 occurrence는 1개당 하나의 `unclassified_text` fallback으로
  보존한다.
- 구조 leaf로 승격할 때 해당 occurrence의 atomic fallback을 제거하고 교체한다. merge
  leaf와 atomic leaf가 동시에 같은 occurrence를 가질 수 없다.
- evidence ownership은 reconstruction plan에 그대로 두고, document view placement와
  구분한다.
- semantic text를 저장하지 않으며, native capture를 다시 검증한 소비자만 occurrence
  ID를 원문과 조인한다.
- native capture의 `markdown`과 `pages_markdown_result`는 파생 구조이므로 입력으로
  읽지 않는다. strict `text_items`와 ledger만 원문 입력으로 허용한다.
- leaf 사이의 연결은 raw 문자열이 아니라 `join_class`로 기록한다. 첫 계약은
  `intra_word_wrap`, `inter_token_space`만 허용하고 unresolved 경계는 merge하지 않는다.
- 구조용 image/arrow occurrence는 semantic ownership이 아닌 별도
  `structural_evidence_occurrence_ids`로만 참조한다.
- `evaluation_only=true`, `non_promotable=true`를 유지한다.

### 2.3 `pdf_primary_view_evaluation/v1`

candidate와 Gold를 동시에 읽을 수 있는 유일한 계약이자 실행 경계다.

- 공개 evaluator는 caller가 전달한 replay receipt를 신뢰하지 않는다. raw/standalone
  candidate와 Gold 및 동일한 source/native/render/parser/calibration 입력을 받아 두 replay를
  매 호출마다 직접 수행한다.
- 두 replay 뒤에도 전달받은 digest를 신뢰하지 않고 canonical bytes에서 SHA를 다시 계산한다.
- schema version, notice ID, source PDF SHA, native capture SHA와 reviewed page scope가
  일치하지 않으면 fail-closed 한다.
- reviewed scope 안에서 kind, occurrence 순서, tree, grid, span, continuation, graph edge를
  비교한다.
- 누락, 중복, over-merge, under-merge와 구조 mismatch만 기록한다.
- partial Gold의 scope 안팎 결과를 분리한다. scope 밖은 pass나 fail이 아니며 부분 Gold로
  전체 문서 점수를 만들지 않는다.
- verdict는 최소 `passed`, `failed`, `not_evaluable_gold_pending`,
  `blocked_scope_mismatch`를 구분한다.
- candidate 생성에 Gold 내용을 되돌려 주지 않는다.

이 분리는 Gold를 복사해 정답 candidate를 만드는 평가 누수를 방지한다.

## 3. 공통 불변식

1. source·page·native capture·artifact lineage가 일치하지 않으면 실패한다.
2. replay된 ledger의 authoritative occurrence set과 primary leaf occurrence multiset이
   정확히 같아야 한다. missing·duplicate는 모두 0이어야 한다.
3. container node는 child ID만 소유하며 descendant occurrence를 다시 복사하지 않는다.
4. 한 leaf 안의 occurrence는 같은 페이지에 있고 native source order가 증가해야 한다.
5. 자동 생성 text, native markdown, OCR 대체 문자열, Unicode 정규화, trim, 공백 보정은
   저장하거나 판정 입력으로 사용하지 않는다.
6. 표는 평문으로 flatten하지 않고 row·column·row span·column span을 보존한다.
7. cross-page 구조는 page-local node를 relation으로 연결하며 한 node가 여러 페이지의
   occurrence를 직접 소유하지 않는다.
8. 다이어그램 node와 directed edge는 분리하고, arrow image는 구조 근거로만 참조한다.
9. standalone validation은 내부 정합성일 뿐 source replay나 신뢰 승격을 대신하지 않는다.
10. 모든 persisted JSON은 exact-key, duplicate-key 거절, byte/depth/node/string/work cap,
    canonical UTF-8 serialization을 적용한다.
11. bbox 판정은 build와 replay에서 동일한 정밀도·좌표를 사용한다. unrounded 값으로
    판정한 뒤 rounded 값으로 재검증하는 비대칭을 허용하지 않는다.
12. production builder에는 notice ID, 특정 occurrence ID, 원문 문자열을 하드코딩하지
    않는다.

## 4. 114788 구조 Gold 검토 초안

Gold는 처음에는 `pending_human_confirmation`으로 생성했다. 아래 판정은 parser 결과가
아니라 canonical page image를 기준으로 사용자가 2026-09-19에 직접 확인했다.

### 4.1 3페이지 목적 문단

- paragraph ordered members: `occ:inspector:p3:t19`, `occ:inspector:p3:t20`
- 두 occurrence 사이의 boundary class는 `intra_word_wrap`이다. Gold에는 결합 문자열이나
  raw separator를 저장하지 않는다.
- 첫 vertical slice의 positive ordered group은 이 paragraph 하나다. `t17`은 해당
  paragraph와의 over-merge를 판정하기 위한 hard negative로만 scope에 포함한다.
- `occ:inspector:p3:t17`은 별도 제목이며 이 paragraph에 포함되는 것은 hard negative다.
  A4.1/v1은 제목과 본문 source leaf를 분리하고 `relations=[]`을 유지한다. A4.2에서는
  versioned document view 또는 별도 relation sidecar로 heading→body 관계를 표현하고,
  context composer만 두 leaf를 함께 제공한다. source ownership이나 leaf 원문은 합치지
  않는다. heading의 positive Gold node는 별도 micro-slice로 분리한다.

### 4.2 3→4페이지 지원내용 표

- 하나의 logical table, 6 rows × 2 columns
- header 1행과 지원항목 5행
- 페이지 3과 4에는 각각 page-local segment를 두고 continuation relation으로 연결한다.
- `기타` 다섯 번째 항목을 버리지 않는다.

### 4.3 5페이지 제출서류 표

- 8 rows × 6 columns
- empty cell은 이웃 값을 복사하지 않고 명시적 empty로 둔다.
- row span 후보: 구분의 `필수`, 수량의 `기업별 각 1부`, 유형의 `스캔본 *.pdf`
- 모든 span과 cell occurrence는 사람 확인 전까지 Gold로 확정하지 않는다.

### 4.4 6페이지 평가절차도와 평가표

- 절차도는 4개 node와 좌→우 3개 edge다.
- arrow image occurrence 3개는 non-substantive이지만 structural evidence로 보존한다.
- 1차 평가표는 3 × 3, 2차 평가표는 9 × 3 후보 grid다.
- 표 아래 별표 설명은 cell에 합치지 않고 note relation으로 연결한다.

## 5. 구현 단계

### A4.0 Gold 계약

1. `primary_structure_gold.py`
2. `pdf_primary_structure_gold_v1.schema.json`
3. 114788 p3 목적 문단의 textless draft fixture
4. strict loader, validator, canonical serializer와 mutation tests
5. portable wheel schema 포함 검증

이 단계는 자동 candidate를 만들지 않는다. draft fixture는 사람 확인 전까지 항상
`not_evaluable`이며 quality-gate 결과를 만들 수 없다. 기존 `pdf_structural_gold/v1`은
변경하지 않는다.

2026-09-19 구현·replay 결과:

- human-confirmed fixture:
  `backend/baselines/pdf_reconstruction/primary_structure_gold_114788_p3_purpose.v1.json`
- canonical SHA-256:
  `2e80529bb500c3947a1121a592483164ce50365e321f352891fdf660d1f362ea`
- reviewer ref: `reviewer:user-confirmation-20260919`
- confirmed at: `2026-09-19T10:36:43Z`
- 실제 114788 source PDF, strict native capture와 전체 `pdf_render_manifest/v1`을 replay해
  source/page/renderer/coordinate/PNG lineage 및 physical page 3 결속 검증 통과
- raw fixture quality status: `not_evaluable_input_replay_required`
- 공개 evaluator가 같은 입력에서 strict replay를 직접 수행한 경우 Gold
  `quality_status`: `evaluable`

결속값은 source PDF `466994bb...f98c`, native capture `09acdc71...596b`, full render
manifest `de0c6dc7...b296`, physical page 3 PNG `fb4513c9...f7dd3`이다. 실제 artifact가
있는 환경에서는 다음 gated test로 persisted fixture 자체를 다시 검증한다.

```bash
PRIMARY_STRUCTURE_GOLD_114788_SOURCE_PDF=<source.pdf> \
PRIMARY_STRUCTURE_GOLD_114788_NATIVE_CAPTURE=<native_capture.v1.json> \
PRIMARY_STRUCTURE_GOLD_114788_RENDER_MANIFEST=<render_manifest.json> \
PRIMARY_STRUCTURE_GOLD_114788_RENDER_ROOT=<render-artifact-root> \
PYTHONPATH=backend/vendor/common_ir_pipeline/src \
backend/vendor/common_ir_pipeline/.venv/bin/python -m unittest \
  backend.vendor.common_ir_pipeline.tests.test_primary_structure_gold.PrimaryStructureGoldTests.test_actual_114788_replay_when_artifact_environment_is_configured
```

### A4.1 atomic candidate와 목적 문단 vertical slice

이 단계의 선행조건인 p3 목적 문단 `human_confirmed`와 strict artifact replay가 완료됐다.

1. Gold를 입력받거나 import하지 않는 candidate builder를 만든다.
2. ledger의 218개 authoritative occurrence를 먼저 atomic fallback에 정확히 한 번
   배치한다.
3. native `source_item_index`·adjacency·geometry 기반의 보수적 paragraph proposal
   policy를 별도로 정의한다. ODL·Surya는 단독 승인 근거가 아니라 경계·veto 또는
   독립 corroboration으로만 사용한다.
4. proposal을 primary paragraph로 승격하는 기준은 Gold를 참조하지 않으며, offline
   evaluator만 p3 `t19+t20` 정답과 비교한다.
5. safety gate는 coverage 218/218, duplicate 0, missing 0, canonical replay 일치다.
   Gold scope exact match와 over-merge 0은 confirmed Gold에 대해서만 quality gate로
   활성화한다.

paragraph policy가 114788의 특정 ID나 문자열에 의존하면 실패다. 범용 승격 기준이 아직
충분하지 않으면 proposal relation만 생성하고 primary ownership은 atomic으로 유지한다.

현재 ODL paragraph bbox는 native em box와 ink box 차이 때문에 p3 `t19`를 95% area
threshold에서 누락한다. threshold를 낮춰 우회하지 않는다. A4.1의 positive 판정에서는
ODL coverage를 사용하지 않았으며, 향후 `partial_native_coverage`와 dropped-reference
진단이 필요하면 새 버전 계약 또는 별도 sidecar로 추가한다.

2026-09-19 구현·replay 결과:

- Gold-blind boundary policy: `native_continuity_intra_word/v1`
- 전체 native source index가 실제로 `+1`인 같은 페이지 occurrence만 비교한다.
- font subset 이름·tag는 줄 중간에서 달라질 수 있어 동일성을 요구하지 않고, size·height·
  style·수직 간격·들여쓰기·본문 폭·한글 한 음절 orphan 조건을 모두 통과한 pair만
  `intra_word_wrap`으로 승격한다.
- qualified edge가 한 occurrence에서 겹치면 모두 atomic으로 보존한다.
- 114788 결과: authoritative `218/218`, missing `0`, duplicate `0`, paragraph pair `4`,
  atomic fallback `210`이다.
- p3에서는 `t19+t20`만 paragraph가 되었고 `t17`은 별도 `unclassified_text` leaf로
  유지됐다. confirmed Gold 평가 결과는 `passed`, hard-negative 위반은 `0`이다.
- candidate builder에는 Gold import·경로·환경변수 채널이 없다. 공개 evaluator만 candidate와
  Gold를 함께 읽으며, caller가 만든 receipt가 아니라 동일 artifact 입력에서 두 결과를 직접
  replay한 뒤 비교한다.
- 이 결과는 확인된 p3 목적 문단 scope의 판정이며 제목·표·다이어그램 또는 전체 문서
  구조 품질을 일반화하지 않는다.

실제 artifact가 있는 환경에서는 아래 환경변수를 모두 지정해 gated replay·evaluation을
재현한다.

```bash
PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF=<source.pdf> \
PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE=<native_capture.v1.json> \
PRIMARY_DOCUMENT_VIEW_114788_STRUCTURE_CANDIDATES=<structure_candidates.v1.json> \
PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST=<render_manifest.json> \
PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT=<render-artifact-root> \
PRIMARY_DOCUMENT_VIEW_114788_RECONSTRUCTION_PLAN=<reconstruction_plan.v1.json> \
PRIMARY_DOCUMENT_VIEW_114788_CALIBRATION_PROOF=<calibration_proof.json> \
PYTHONPATH=backend/vendor/common_ir_pipeline/src \
backend/vendor/common_ir_pipeline/.venv/bin/python -m unittest \
  backend.vendor.common_ir_pipeline.tests.test_primary_document_view.PrimaryDocumentViewTests.test_actual_114788_replay_when_artifact_environment_is_configured \
  backend.vendor.common_ir_pipeline.tests.test_primary_view_evaluation.PrimaryViewEvaluationTests.test_actual_114788_evaluator_replay_when_artifact_environment_is_configured
```

### A4.2 heading과 section

Surya `section_header`, native font/geometry, 번호 패턴을 독립적으로 검증한다. paragraph
slice와 같은 gate에 섞지 않는다. A4.1/v1의 `relations=[]` 계약을 조용히 바꾸지 않고,
별도 `pdf_primary_heading_relations/v1` sidecar를 추가한다. 이 sidecar는 existing v1
leaf ID 사이의 `heading_to_body` 관계만 저장하며 occurrence를 소유하거나 leaf kind를
변경하지 않는다. 수용 조건은 관계 방향이 heading→body이고 양 끝이 존재하는 서로 다른
leaf를 참조하며, 관계를 추가해도 두 leaf의 occurrence ownership과 본문 paragraph 경계가
바뀌지 않는 것이다. LLM context 결합은 이 관계를 소비하는 후속 composition 단계에서만
수행한다.

첫 버전의 positive admission 근거는 strict source/render/model replay를 통과한 Surya
`section_header`, bounded ASCII numbered-heading shape, native typography·geometry·순서다.
현재 ODL은 `legacy_claim_unbound`, `legacy_odl_non_promotable`,
`calibration_kind_unverified` 상태이므로 positive 승인 또는 corroboration에 사용하지 않는다.
ODL을 정식 신호로 쓰려면 source-bound heading-kind calibration을 별도 버전으로 먼저
도입해야 한다.

v1은 의도적으로 다음 범위만 다룬다.

- singleton atomic source leaf에서 바로 다음 same-page paragraph leaf로 연결
- unique Surya `section_header` containment와 non-heading region veto
- 번호가 붙은 single-line heading만 판정
- heading level, 전체 section span, unnumbered/multiline heading, cross-page 관계는 제외
- source/target/region의 fan-in·fan-out과 ambiguity는 relation 미생성으로 fail closed

positive Gold는 기존 paragraph Gold를 수정하지 않고 별도
`pdf_primary_heading_relation_gold/v1`로 둔다. Gold endpoint는 candidate leaf ID가 아니라
heading occurrence ID와 기존 paragraph Gold group ID를 사용한다. 114788의 첫 micro-slice는
`occ:inspector:p3:t17 → p3.purpose.paragraph`이며, 기존 `forbidden_same_leaf` hard negative가
그 선행조건이다. 실제 positive 평가는 full source-bound Surya artifact가 있는 환경에서만
gated replay로 실행한다.

2026-09-19 구현·replay 결과:

- `pdf_primary_heading_relations/v1`, `pdf_primary_heading_relation_gold/v1`,
  `pdf_primary_heading_relation_evaluation/v1`을 서로 분리된 textless sidecar로 구현했다.
- candidate는 `primary_numbered_heading_relation/v1` 정책, A4.1 view SHA, strict Surya artifact
  SHA와 각 `section_header` region을 관계 ID에 결속한다. 관계 수는 strict Surya 상한과 같은
  10,000건으로 제한한다.
- 114788 full replay에서 `t17` leaf → `t19+t20` paragraph leaf 관계가 정확히 1건 생성됐다.
  Surya region은 `p0003-section_header-0002`, A4.1 view SHA는
  `e1f80ebdca586acde6fb68bc75f00566f1fae50b66dd1c4e0d72e6816d38fa4e`로 A4.1 구현 때와
  동일하다.
- 별도 evaluator는 A4.1 paragraph 평가의 `passed`를 선행조건으로 요구한다. 기대 관계 누락,
  잘못된 target, reviewed scope 안의 false positive는 실패하고, scope 밖 관계만 unscored로
  분리한다. 114788 결과는 기대 관계 1/1 일치, 실패·unscored 0건으로 `passed`였다.
- 이 결과 역시 `evaluation_only=true`, `non_promotable=true`다. A4.1 source ownership과
  `relations=[]`은 바뀌지 않으며 production Common IR 승격 근거로 단독 사용하지 않는다.
- standalone `validate_*`와 JSON Schema는 `internal_consistency_only` 검사다. 임의 digest
  claim을 source replay 증거로 인정하지 않으므로, 품질 판정 경계에서는 반드시 원시 입력을
  받는 `evaluate_pdf_primary_heading_relations`를 호출해야 한다. JSON Schema만 통과한 결과를
  admission 또는 승격 근거로 사용하지 않는다.

실제 114788 회귀는 A4.1의 `PRIMARY_DOCUMENT_VIEW_114788_*` 환경 변수 7개와
`PRIMARY_HEADING_RELATIONS_114788_SURYA_LAYOUT_ARTIFACT`를 설정한 뒤 다음 테스트로 재현한다.

```bash
PYTHONPATH=backend/vendor/common_ir_pipeline/src:backend/vendor/common_ir_pipeline/tests \
backend/vendor/common_ir_pipeline/.venv/bin/python -m unittest \
  test_primary_heading_relations.PrimaryHeadingRelationsTests.test_actual_114788_replay_when_artifact_environment_is_configured \
  test_primary_heading_relation_gold.PrimaryHeadingRelationGoldTests.test_actual_114788_gold_replay_when_artifact_environment_is_configured \
  test_primary_heading_relation_evaluation.PrimaryHeadingRelationEvaluationTests.test_actual_114788_full_evaluator_when_artifacts_are_configured
```

### A4.3 table grid와 cross-page continuation

ODL grid는 정답이 아니라 proposal이다. source-bound native coverage와 strict Surya table
region replay, reviewed topology gate를 통과한 경우에만 candidate grid로 투영한다. 현재
`pdf_context_groups/v1`은 Surya table/list/diagram region 전체를 표현하지 않으므로, strict
Surya artifact를 직접 검증해 입력받거나 별도 textless region sidecar를 먼저 추가한다.
ODL `table_row` bbox, table/cell-kind 좌표 calibration, grid·rowspan 계약과
cell-to-occurrence partition Gold가 준비되기 전에는 cross-page table을 primary leaf로
승격하지 않는다.

A4.3은 다음 세 단계로 분리한다.

1. **A4.3a page-local grid 평가**: `pdf_primary_table_grid/v1`은 ODL의 table→row→cell
   topology를 proposal로 받고, strict Surya outer-table region과 Native occurrence geometry가
   같은 페이지 표 외곽을 독립적으로 지지할 때만 평가용 segment를 만든다. Native atomic
   ownership은 변경하지 않으며, 첫 slice는 `rowspan=colspan=1`만 허용한다. 대응되는
   `pdf_primary_table_grid_gold/v1`은 candidate·ODL·Surya ID와 원문 text를 저장하지 않고
   reviewer-defined row/column/cell ID와 Native occurrence partition만 기록한다. evaluator는
   모든 원시 입력을 다시 replay하고 missing, wrong partition, duplicate, orphan, negative
   anchor attachment를 fail-closed 처리한다. Native occurrence가 표 전체에 0개인 일치
   후보는 skip하고, table/cell partition의 부분 공집합이나 불일치는 malformed 입력으로
   fail-closed 처리한다.
2. **A4.3b cross-page continuation 평가**: page-local grid 평가를 통과한 fragment만 별도
   continuation candidate와 Gold의 입력이 될 수 있다. v1 candidate는 독립 근거가 있는
   `between_rows`만 제안하고, `same_row`와 cell continuation은 후속 slice로 남긴다. 인접
   페이지의 유일한 하단·상단 경계 표, 동일한 column count와 정규화 column geometry를
   요구하며, predecessor/successor 각 1개와 cycle 없음이 필수다. 경계 후보가 모호하면
   임의 선택하지 않고 relation 0건으로 남긴다. 이 단계도 evaluation-only다.
3. **production admission**: source-bound ODL producer identity와 table/table-cell 전용 좌표
   calibration, 그리고 독립적인 textless TableRec/grid artifact가 같은 topology를 확인하기
   전에는 page-local grid와 continuation 모두 Common IR이나 primary leaf로 승격하지 않는다.

114788의 첫 review 대상 slice는 physical page 3과 4 상단이다. page 3은 header 1행과 body
2행, page 4 상단은 반복 header가 없는 body 3행으로, human-confirmed Gold에 동결한 논리 구조는 전체
6행×2열(header 1 + body 5)이다. page 4 하단의 별도 2행×2열 `우대사항` 표는 continuation
hard negative다. A4.3a에서는 별도 표로 candidate에 보존하되 reviewed scope 밖이므로
unscored로 남기고, A4.3b continuation Gold에서 잘못 연결하면 실패하도록 판정한다. p3 마지막
cell의 `t49`와 p4 첫 cell의 `t51`은 같은 cell 또는 row로 합치지 않는다. 현재 두 page-local
외곽에서 Surya↔ODL IoU는 각각 0.9631과 0.9702지만, 두 엔진 모두 명시적인 page-span
relation을 제공하지 않으므로 이 수치만으로 continuation을 확정하지 않는다.

A4.3a 구현은 candidate·Gold·evaluator를 분리했다. 실제 114788 replay는 p3 3×2, p4 상단
3×2, p4 하단 2×2 후보를 생성했다. p3/p4 상단 Gold는 2026-09-20(KST)에 프로젝트
소유자가 원문 렌더와 셀 내용을 함께 확인했으며, human-confirmed canonical SHA-256
`548f3fd6ce803e15439f4c4e0e5abaa7fa295db76b6ceb71aa61dcc77bf6d725`를 신뢰 allowlist에
등록했다. 실제 evaluator verdict는 `passed`이고 2개 scope·12개 cell이 일치하며, p4 하단
별도 표 1개는 A4.3a에서 unscored로 보존한다. 실제 artifact를 포함한 회귀는 다음 환경 변수와
테스트로 재현한다.

```bash
PRIMARY_DOCUMENT_VIEW_114788_SOURCE_PDF=/path/to/source.pdf \
PRIMARY_DOCUMENT_VIEW_114788_NATIVE_CAPTURE=/path/to/native_capture.v1.json \
PRIMARY_DOCUMENT_VIEW_114788_STRUCTURE_CANDIDATES=/path/to/structure_candidates.v1.json \
PRIMARY_DOCUMENT_VIEW_114788_RENDER_MANIFEST=/path/to/render_manifest.json \
PRIMARY_DOCUMENT_VIEW_114788_RENDER_ROOT=/path/to/render-root \
PRIMARY_DOCUMENT_VIEW_114788_RECONSTRUCTION_PLAN=/path/to/reconstruction_plan.v1.json \
PRIMARY_DOCUMENT_VIEW_114788_CALIBRATION_PROOF=backend/baselines/pdf_reconstruction/opendataloader_coordinate_calibration_v1/calibration_proof.json \
PRIMARY_HEADING_RELATIONS_114788_SURYA_LAYOUT_ARTIFACT=/path/to/surya_layout_artifact.v1.json \
PYTHONPATH=backend/vendor/common_ir_pipeline/src:backend/vendor/common_ir_pipeline/tests \
backend/vendor/common_ir_pipeline/.venv/bin/python -m unittest \
  test_primary_table_grid test_primary_table_grid_gold test_primary_table_grid_evaluation \
  test_primary_table_continuation test_primary_table_continuation_gold \
  test_primary_table_continuation_evaluation
```

A4.3b도 candidate·Gold·evaluator를 분리했다. v1 candidate는 원문 text나 Gold를 보지 않고
인접 페이지 경계와 column geometry만으로 `between_rows` 관계를 제안한다. 실제 114788에서는
p3 하단 표→p4 상단 표 관계 1건만 생성됐고, predecessor bottom gap 97,834 ppm, successor
top gap 68,674 ppm, outer-x IoU 1,000,000 ppm, column edge drift 0 ppm이었다. p4 하단
`우대사항` 표는 상단 경계 조건과 column geometry를 통과하지 못해 제외된다.

프로젝트 소유자가 원문 렌더를 기준으로 페이지 간 관계와 별도 `우대사항` 표 경계를 확인했고,
continuation Gold canonical SHA-256
`36d58cf6c7fb3e85e429937a927fb4a1c5542e487736359aa760b2191c4b98eb`를 신뢰 allowlist에
등록했다. 공개 evaluator는 A4.3a grid candidate·Gold·evaluation과 A4.3b candidate·Gold를
같은 raw input에서 매번 replay한다. 실제 정상 관계는 `passed`이며, p3 표를 p4 하단
`우대사항` 표에 연결하는 변조는 missing expected relation과 잘못된 successor의
`unexpected_edge` false positive로 실패한다. 이 결과는 여전히 evaluation-only이며
production admission을 의미하지 않는다.

### A4.4 diagram

node, directed edge, structural arrow evidence를 별도 계약으로 생성한다. bbox 근접만으로
edge를 만들지 않고, ambiguous edge는 unresolved로 둔다. Surya의 단일 `diagram` bbox는
내부 edge 근거가 아니므로 별도 diagram-structure evidence source가 없으면 승격하지 않는다.

### A4.5 corpus gate와 model-input A/B

114788은 정책을 고치는 tuning fixture로 취급한다. selected regions를 통과한 뒤 121019와
추가 PDF를 포함한 최소 4건 structural corpus, 그리고 threshold 선택 전에 봉인한 held-out
Gold에서 같은 정책을 검증한다. 그 전에는 Common IR, selector 또는 작은 LLM 입력에
연결하지 않는다.

2026-09-20에 선정 역할과 검토 범위를
`backend/baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json`으로 먼저 동결했다.
canonical SHA-256은
`d6820fbe176df095cbd09e3faf56568167243b9db990982f295243d50f49fd51`다. 이 파일은
Gold나 실행 점수를 담지 않는 immutable split이며 `evaluation_only=true`,
`non_promotable=true`다. 실제 품질 판정은 이후 별도 gate report가 이 split digest와
trusted Gold, raw-input replay 결과를 함께 결속할 때만 수행한다.

| case | 역할 | 사전 고정한 physical page | 선정 당시 상태 |
| --- | --- | --- | --- |
| `114788` | tuning | 3, 4 | 좁은 paragraph·heading·grid·between-rows Gold 준비 |
| `121019` | known regression | 5 | 기존 특수 Gold를 신규 evaluator로 migration해야 함 |
| `104102` | public held-out | 1, 3, 5, 9 | Gold pending |
| `124791` | public held-out | 2, 3, 4 | Gold pending |
| `blind-x-01` | sealed blind held-out | tracked 파일에는 비공개 | salted commitment만 공개, Gold pending |
| `115310` | table/continuation clean-negative | 1, 2, 3 | 0-grid·0-edge Gold pending |

blind case의 notice/source/page identity와 salt는 Git에 넣지 않고
`.runtime/evaluations/pdf-primary-corpus-a45/blind-reveal.v1.json`에만 둔다. CLI는 그 값을
출력하지 않고 commitment와 source baseline membership의 성공 여부만 보고한다. 이 local
reveal을 분실하면 기존 commitment를 임의로 다시 만들지 말고 corpus split을 새 버전으로
폐기·재선정한다.

```bash
PYTHONPATH=backend/vendor/common_ir_pipeline/src \
backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/verify_pdf_primary_corpus_split.py \
  --split backend/baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json \
  --source-baseline backend/baselines/pdf_fusion/bizinfo_existing_100.v1.json \
  --expected-split-sha256 d6820fbe176df095cbd09e3faf56568167243b9db990982f295243d50f49fd51 \
  --reveal .runtime/evaluations/pdf-primary-corpus-a45/blind-reveal.v1.json
```

`--expected-split-sha256` 값은 검증할 코드와 함께 바뀌는 입력이 아니라, 검토가 끝난
release/commit 기록에서 별도로 가져와야 한다. 이 외부 anchor가 다르면 검증은 실패한다.
split 검증기는 선정 계약·source baseline·blind reveal의 결속만 확인하며 품질 상태를 만들지
않는다. 현재 코퍼스는 일부 Gold와 strict artifact가 아직 준비되지 않아 별도 품질 gate를
실행할 수 없는 상태다. v1이 주장할 수 있는 범위도 2-occurrence paragraph, 번호형 단일행 heading→다음 paragraph, span 없는
page-local grid, 인접 페이지 `between_rows` continuation뿐이다. clean-negative를 포함해
Gold가 없는 범위를 N/A나 평균 점수로 상쇄하지 않는다. split 검증에는 RunPod가 필요 없고,
공개 네 case의 source-bound canonical render는 다음 A4.6 준비 단계에서 생성했다. 실제
strict Surya layout artifact와 sealed blind artifact는 아직 생성해야 한다.

### A4.6 공개 case의 strict Surya export 경계

2026-09-20에 `104102`, `124791`, `121019`, `115310` 네 공개 case의 전체 native capture와
canonical render를 split/source baseline에 결속해 로컬 preflight했다. 각각 14, 8, 5, 3개
render page가 원문 SHA와 manifest에 맞는지 확인했다. 이 단계는 네트워크 호출을 하지 않았고
`surya/` 결과도 만들지 않았다.

`backend/scripts/export_pdf_primary_corpus_surya_artifact.py`는 이 preflight를 실제 persistent
RunPod E2E 직전과 결과 publish 직전에 반복하고, 콜백 artifact의 producer·logical key·전체
page·render lineage를 독립적으로 재검증한다. 결과는 case별
`surya/surya_layout_artifact.json`에 create-only로 저장한다. blind reveal은 받지 않으며,
Gold나 품질 판정도 읽지 않는다. 구현·보안 리뷰와 64개 단위·계약 테스트는 통과했지만,
RunPod이 꺼진 상태라 위 네 case의 실제 strict Surya artifact 생성은 아직 수행하지 않았다.
운영 명령과 cache/journal 제한은 `backend/prereview_runpod_worker/README.md`의
"A4.5 공개 평가 코퍼스 결과 저장" 절을 따른다.

## 6. 검증과 제한

- tree: parent 존재, 허용 kind, sibling index 연속, cycle 없음
- ownership: substantive set exact equality, missing/duplicate 0
- table: bounds, grid gap/overlap, span 충돌, cell occurrence 중복 없음
- continuation: page 증가, 같은 logical structure, fan-in/out 제한, cycle 없음
- diagram: endpoint 존재, 같은 diagram, self-edge·duplicate edge 없음
- 현재 artifact별 reader cap: Gold JSON node ≤ 50,000, reviewed occurrence/scope ≤ 4,096,
  candidate JSON node ≤ 4,000,000, native occurrence ≤ 500,000,
  evaluation JSON node ≤ 250,000, page ≤ 256, candidate artifact ≤ 128 MiB
- future tree/table/diagram 계약의 목표 cap: context groups ≤ 25,000,
  grid slots ≤ 1,000,000. 실제 A4.1 runtime 계약이 생기기 전에는 admission 수치로 간주하지 않는다.
- traversal: 재귀 대신 map과 iterative traversal을 사용해 O(nodes + occurrences + refs +
  grid slots)로 제한
- mutation: digest/page/source 교체, duplicate occurrence, parent cycle, grid overlap/gap,
  span overflow, cross-page fan-out, graph cycle, Gold leakage를 모두 fail-closed 처리
- trust boundary: standalone schema 통과만으로 admission하지 않고 source/native replay와
  Python semantic validator를 모두 통과해야 함
- leakage: builder production module의 Gold symbol·path 참조 금지, Gold 파일의 존재·부재와
  무관하게 candidate canonical bytes가 동일함을 검증
- pending Gold: schema·canonical·artifact replay 진단만 허용하고 candidate 구조 비교는
  수행하지 않으며 quality verdict는 항상 `not_evaluable_gold_pending`
- reader limits: artifact byte·wall-clock·work budget를 graph traversal 전에 확인

## 7. 현재 판정

- 기존 context hypothesis 보존: GO
- 새 textless partial Gold 계약과 첫 human-confirmed scope 구현: GO
- 기존 `pdf_structural_gold/v1` 일반화: NO-GO
- Gold를 candidate 입력으로 사용: NO-GO
- 자동 primary document view 목적 문단 vertical slice 구현·replay: GO
- 114788 한 건으로 구조 품질 일반화: NO-GO
- Common IR·selector·LLM 승격: 4건 corpus gate 전까지 NO-GO

## 8. 외부 검토 반영 결정

Claude Opus 5와 Grok 4.5는 모두 `GO WITH CHANGES`로 판정했다. 다음 권고를 수용했다.

- candidate의 기준 집합을 context candidate가 아니라 native ledger로 전환
- 기존 121019 Gold를 동결하고 새 textless partial Gold 계약 추가
- Gold를 native capture digest·extractor version에 결속
- pending Gold의 품질 PASS 금지와 partial-scope 결과 분리
- native markdown 입력 금지, Gold import/process firewall 추가
- ODL bbox에서 `t19`가 누락되는 em-box/ink-box 문제를 calibration과 진단으로 먼저 고정
- table·diagram 승격 전 topology·좌표·edge evidence 전제조건 명시
- 114788을 tuning fixture로 한정하고 별도 held-out Gold 요구

Surya `list_group`을 paragraph 승인 대상으로 즉시 넓히라는 선택지는 수용하지 않았다.
실제 affine 재검증 전에는 broad region을 단독 승인 근거로 사용하지 않으며, 첫 slice에서는
native continuity가 주 판단이고 ODL·Surya는 veto·corroboration으로만 사용한다.
