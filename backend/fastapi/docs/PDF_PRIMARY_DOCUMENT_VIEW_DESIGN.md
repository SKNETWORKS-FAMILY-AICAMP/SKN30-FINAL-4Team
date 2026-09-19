# PDF Primary Document View 설계

- 상태: 외부 설계 검토 반영 및 A4.0·A4.1 구현, 사람 확인·실제 artifact replay 완료
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
versioned document view 또는 별도 relation sidecar를 추가한다. 수용 조건은 관계 방향이
heading→body이고 양 끝이 존재하는 서로 다른 leaf를 참조하며, 관계를 추가해도 두 leaf의
occurrence ownership과 본문 paragraph 경계가 바뀌지 않는 것이다. LLM context 결합은 이
관계를 소비하는 후속 composition 단계에서만 수행한다.

### A4.3 table grid와 cross-page continuation

ODL grid는 정답이 아니라 proposal이다. source-bound native coverage와 strict Surya table
region replay, reviewed topology gate를 통과한 경우에만 candidate grid로 투영한다. 현재
`pdf_context_groups/v1`은 Surya table/list/diagram region 전체를 표현하지 않으므로, strict
Surya artifact를 직접 검증해 입력받거나 별도 textless region sidecar를 먼저 추가한다.
ODL `table_row` bbox, table/cell-kind 좌표 calibration, grid·rowspan 계약과
cell-to-occurrence partition Gold가 준비되기 전에는 cross-page table을 primary leaf로
승격하지 않는다.

### A4.4 diagram

node, directed edge, structural arrow evidence를 별도 계약으로 생성한다. bbox 근접만으로
edge를 만들지 않고, ambiguous edge는 unresolved로 둔다. Surya의 단일 `diagram` bbox는
내부 edge 근거가 아니므로 별도 diagram-structure evidence source가 없으면 승격하지 않는다.

### A4.5 corpus gate와 model-input A/B

114788은 정책을 고치는 tuning fixture로 취급한다. selected regions를 통과한 뒤 121019와
추가 PDF를 포함한 최소 4건 structural corpus, 그리고 threshold 선택 전에 봉인한 held-out
Gold에서 같은 정책을 검증한다. 그 전에는 Common IR, selector 또는 작은 LLM 입력에
연결하지 않는다.

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
