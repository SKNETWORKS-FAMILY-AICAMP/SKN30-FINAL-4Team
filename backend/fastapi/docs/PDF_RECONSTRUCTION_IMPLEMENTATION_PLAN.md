# PDF 구조 복원 구현 계획

- 상태: Opus 5 xhigh 아키텍처 리뷰와 Grok 4.6 xhigh 레드팀, 후속 독립 코드 감사 반영 계획 v1.0.
  A0 입력·좌표 계약, A1 human-confirmed Gold, A2 native-first reconstruction plan과
  held-out safety replay를 완료했다. A2.5 `pdf_fragment_groups/v1`의 합성 계약·단위 회귀를
  구현하고 114788 전체 7페이지 strict Surya replay를 완료했다. A3 textless context view와
  114788 replay, A4.1~A4.3의 좁은 paragraph·heading·table grid·between-rows 평가 계약을
  완료했다. A4.5의 6건 immutable split을 동결했고, A4.6 공개 case exporter와 네 case의
  native/render preflight를 완료했다. 실제 strict Surya artifact와 held-out Gold 생성이
  다음 단계다.
- 기준일: 2026-09-20
- 대상: native PDF text, Surya layout/OCR, OpenDataLoader 구조 결과를 이용한 PDF Common IR 전처리
- 원칙: 기존 Common IR 및 Profile 산출물을 변경하기 전에 독립적인 shadow sidecar와 오프라인 회귀로 구조 품질을 증명한다.
- 외부 판정: Opus `GO_WITH_CHANGES`, Grok `REJECT as written`. 공통 blocking 지적을
  반영했고, 문단 복원 자체를 연기하라는 권고는 목표와 맞지 않아 production과 격리한
  offline context-only vertical slice로 축소해 진행한다.

## 1. 문제와 관찰 근거

현재 PDF 파이프라인은 native text를 근거 문자열로 보존하지만, 문단·목록·표 관계를
충분히 복원하지 못한다. 그 결과 문자열 자체는 존재해도 작은 LLM이 독립적으로 이해할
수 없는 조각이나 페이지 전체를 합친 거대한 블록이 만들어진다.

`PBLN_000000000121019` 5페이지는 세 소스가 모두 존재하는 대표 사례다.

- pdf-inspector native에는 정확한 문자열 조각이 존재한다.
- OpenDataLoader는 51개의 list item과 중첩 list 관계를 제안한다.
- Surya layout은 `Text`, `SectionHeader`, `ListGroup` 영역을 제공하고, legacy 표 결과는
  high-accuracy HTML과 TableRec 모두 외형을 `43 x 2` grid로 판정한다. 다만 HTML의
  `세제 지원`/`자금 지원` rowspan은 각각 `10`/`8`로, 화면 판독 Gold의 `12`/`9`와 다르다.
- legacy table gate artifact는 해당 표를 `EXPLICIT_CANDIDATE`로 판정한다. 이 artifact는
  현재 strict `surya_layout_artifact/v1`이 아니므로 production 승격 근거로 사용할 수 없다.
- 그러나 frozen Common IR에는 table/cell/relation이 모두 0개이고, 표 전체가 하나의 큰
  Surya block으로 남아 있다.
- Surya HTML에는 `익금불산입`을 `의금불산입`으로 읽은 사례가 있어 OCR 문자열을 문자
  권위로 사용할 수 없다.

따라서 병목은 OCR 모델 하나의 품질보다 세 결과를 안전하게 정렬하고 native occurrence로
재결속하는 fusion/materialization 단계에 있다.

## 2. 목표와 비목표

### 2.1 목표

1. native occurrence를 잃거나 고쳐 쓰지 않고 문단·제목·목록·표 단위로 재구성한다.
2. Surya OCR 문자열을 사용하지 않아도 layout label·bbox·reading order를 문단 복원에
   사용한다.
3. OpenDataLoader의 문단·목록·표 관계를 구조 후보로 사용한다.
4. 모든 복원 단위를 실제 native occurrence와 역추적 가능하게 결속한다.
5. 작은 LLM에 전달할 context가 기존 selector의 UTF-8 byte budget 안에 들어가도록 만들고,
   tokenizer가 고정된 뒤 별도 token budget을 정의할 수 있게 한다.
6. Existing과 Request가 공유할 수 있는 물리·구조 계층으로 구현한다.
7. 새 selector wire/anchor를 만들지 않고 기존 `SourceBlock`, `StructuralWorkUnitV1`,
   `claim_source_block_ids`, `context_source_block_ids` 계약을 재사용한다.

### 2.2 이번 단계의 비목표

- OCR 문자열을 Common IR v1의 semantic evidence로 승격하지 않는다.
- image-only PDF의 최종 구조화를 지원하지 않는다. native text가 없으면
  `requires_ocr_semantic_v2`로 명시한다.
- 페이지 간 표를 production explicit table로 합치지 않는다. continuation proposal만
  진단 정보로 허용한다.
- 생성형 모델로 원문을 교정하거나 누락 문장을 생성하지 않는다.
- 최초 shadow 단계에서는 기존 `adapters/pdf_native.py`, 저장된 Common IR, Profile JSON을
  변경하지 않는다.
- 최초 shadow 단계에서는 CandidatePack, 공개 DTO, DB/Storage artifact·lineage도 변경하지
  않는다. 산출물은 평가 디렉터리에만 기록한다.
- OpenDataLoader 실행 환경을 먼저 배포하지 않는다. 확보된 cached JSON으로 설계를 검증한
  뒤 별도 CPU runtime을 고정한다.
- strict Surya layout 계약과 별도 schema가 없는 legacy HTML/TableRec를 혼합하지 않는다.
  cached legacy 결과는 좌표·구조 가설을 검증하는 `unverified` 평가 입력일 뿐이다.

## 3. 소스별 권한

| 소스 | 허용 역할 | 금지 역할 |
| --- | --- | --- |
| pdf-inspector native | 최종 문자, occurrence, 근거 | 구조가 없다는 이유로 원문 삭제 |
| strict Surya layout v1 | 문단·제목·목록·표의 textless 영역과 reading order | HTML/TableRec grid가 포함된 것으로 간주 |
| legacy Surya HTML/TableRec | cached 평가에서 표 grid·rowspan·colspan 가설 | 별도 artifact 계약 없이 runtime·evidence에 사용 |
| OpenDataLoader | 문단·제목·목록 계층과 논리적 표 후보 | spacing 보정 문자열을 근거로 승격 |
| reconstruction plan | 결정적 결속·소유권·충돌 ledger | 새로운 semantic text 생성 |

## 4. 목표 데이터 흐름

```text
immutable source PDF
  ├─ pdf-inspector native atoms ──────────────── 문자 권위
  ├─ cached OpenDataLoader JSON ──────────────── 논리 구조 후보
  └─ canonical render + Surya artifact ───────── 시각 구조 후보
                         │
                         ▼
          pdf_reconstruction_plan/v1 evaluation sidecar
             ├─ accepted + evidence_atomic
             ├─ partial + context_only
             └─ rejected + diagnostic_only
                         │
                         ▼
             pdf_fragment_groups/v1 evaluation sidecar
          (A2.5 exact fragment consensus, IDs only)
                         │
                         ▼  strict disposition 확인; 구조 Gold 전에는 context-only
             pdf_context_groups/v1 evaluation sidecar
             (A3 textless hypothesis index; no materialization)
                         │
                         ▼
           existing SourceBlock/context envelope projection
                         │
                         ▼
             existing exact-span anchor validation
                         │
                         ▼
              original native occurrence IDs
```

원자 evidence와 복원 view를 분리한다. native occurrence 하나는 정확히 하나의 leaf unit만
primary owner로 가질 수 있다. table row, list group, section 같은 상위 container는 leaf를
참조할 수 있지만 소유하지 않는다. context view의 중복 참조는 허용하되 Fact evidence
소유권으로 해석하지 않는다. 초기 구현은 sidecar까지만 생성하며 selector에는 연결하지
않는다. 후속 연결에서도 context-only block은 기존 admission 계약에 따라 claim, coverage,
value anchor가 될 수 없다.

최소 v1 구현에서는 native occurrence 자신이 `evidence_atomic` unit의 유일 소유자다.
ODL/Surya unit은 occurrence를 문맥으로 참조할 수 있을 뿐 소유하지 않으며,
`evidence_composite`는 비활성화한다. 이 구조는 parser 계층이 틀려도 원문 occurrence가
사라지거나 다른 구조 unit에 중복 소유되는 것을 막는다.

## 5. Artifact 계약

### 5.1 OpenDataLoader binding envelope

ODL raw JSON에는 source SHA와 parser config가 없으므로 raw JSON을 직접 신뢰하지 않는다.
호출자가 다음 envelope를 생성하고 검증한다.

```json
{
  "schema_version": "opendataloader_artifact/v1",
  "notice_id": "PBLN_000000000121019",
  "source_pdf_sha256": "...",
  "raw_json_sha256": "...",
  "canonical_json_sha256": "...",
  "parser": {
    "name": "opendataloader-pdf",
    "version": "2.5.7",
    "config_sha256": "...",
    "ocr_enabled": false
  },
  "source_page_count": 5,
  "odl_page_count": 5,
  "page_scope": [5],
  "odl_page_to_source_page": [
    {"odl_page": 5, "source_page": 5}
  ],
  "enumeration_mode": "recursive_structure_candidates/v1",
  "coordinate_space": "odl_pdf_points_unverified"
}
```

이 strict envelope는 **새로 실행하는 ODL부터 생성**한다. 현재 확보된 과거 ODL JSON은
README에 parser/version/OCR-off 기록은 있지만 실행 config의 실제 byte/hash가 남아 있지 않다.
따라서 임의 config hash를 만들어 strict artifact로 승격하지 않고, 별도의
`legacy_opendataloader_evaluation/v1` non-promotable envelope에서 manifest/baseline의
`notice_id + source_pdf_sha256`, raw/canonical hash, 명시적 page mapping만 결속한다.

ODL 좌표의 origin·rotation·CropBox 규칙이 fixture로 검증되기 전에는
`odl_pdf_points_unverified` 상태를 벗어나지 못한다. `page_scope`, ODL 객체의
`page number`, native `provenance.page`는 위 전단사 mapping을 통해서만 비교한다. ID 문자열에서
page를 파싱하지 않는다.

`opendataloader_artifact/v1`이 허용하는 좌표 상태는
`odl_pdf_points_unverified` 하나뿐이다. 좌표 승격은 이 envelope의 문자열만 바꾸는 작업이
아니며, 별도의 calibration proof를 담는 후속 schema/version과 리뷰가 필요하다.

`raw_json_sha256`은 디스크의 실제 byte SHA다. `canonical_json_sha256`은 duplicate key와
NaN/Infinity를 거절한 뒤 UTF-8, `ensure_ascii=False`, `sort_keys=True`,
`separators=(",", ":")`, `allow_nan=False`로 직렬화한 byte SHA다. 두 값은 서로 다른 목적이며
하나를 다른 하나 대신 사용하지 않는다.

### 5.2 Reconstruction plan

```json
{
  "schema_version": "pdf_reconstruction_plan/v1",
  "evaluation_only": true,
  "non_promotable": true,
  "standalone_validation_scope": "internal_consistency_only",
  "native_text_status": "native_text_available",
  "notice_id": "PBLN_000000000121019",
  "source_pdf_sha256": "...",
  "page_scope": [5],
  "input_artifacts": {
    "native_capture_sha256": "...",
    "structure_candidates_sha256": "...",
    "coordinate_calibration_sha256": "f8c041e..."
  },
  "coordinate_projection": {
    "status": "calibrated_shadow_only",
    "parser_binding_status": "legacy_claim_unbound"
  },
  "units": [
    {
      "unit_id": "unit-native-<sha256>",
      "origin": "native",
      "kind": "native_text",
      "page": 5,
      "alignment_status": "accepted",
      "use_policy": "evidence_atomic",
      "primary_occurrence_ids": ["occ:inspector:p5:t8570"],
      "reference_occurrence_ids": []
    },
    {
      "unit_id": "unit-odl-<sha256>",
      "origin": "opendataloader",
      "source_candidate_id": "odl-<sha256>",
      "kind": "list_item",
      "page": 5,
      "alignment_status": "partial",
      "use_policy": "context_only",
      "primary_occurrence_ids": [],
      "reference_occurrence_ids": ["occ:inspector:p5:t8570"],
      "reason_codes": [
        "calibration_kind_unverified",
        "legacy_odl_non_promotable"
      ]
    }
  ],
  "native_occurrence_ledger": [],
  "rejected_candidates": [],
  "metrics": {
    "native_ownership_gate_status": "passed"
  }
}
```

위 JSON은 필드 역할을 보여 주기 위해 일부 필수 하위 필드를 생략한 예시다. 실행 계약의
정확한 required field와 상한은 `pdf_reconstruction_plan_v1.schema.json`을 기준으로 한다.

sidecar에는 lineage·좌표·충돌 정보를 보존한다. 같은 native set에 대해 table과 list처럼
서로 다른 구조 유형이 충돌하면 두 후보를 동시에 accepted evidence로 만들지 않고 대안
hypothesis와 충돌 reason을 기록한다.

standalone validator와 canonical serializer는 plan 내부 정합성만 검사하며 입력 artifact를
인증하지 않는다. 저장되거나 외부에서 받은 plan은
`validate_reconstruction_plan_against_inputs`로 원본 PDF와 native/render/structure/proof를
재검증하고, 결정적으로 재생성한 plan과 canonical byte identity가 같아야 다음 단계에서
사용할 수 있다. substantive native text가 0건이면
`native_text_status=requires_ocr_semantic_v2`이고 native ownership gate는 실패한다.

## 6. 결정적 복원 알고리즘

### 6.1 입력 검증과 좌표 정규화

1. source PDF, native capture, render manifest, Surya stage record를 SHA-256으로 결속한다.
2. cached legacy Surya와 현재 strict `surya_layout_artifact/v1`을 구분한다. strict artifact만
   trusted render manifest와 결속된 좌표로 취급한다. legacy artifact는 변환 bridge가 검증될
   때까지 `legacy_surya_unverified`다.
3. strict Surya top-left rendered-pixel bbox를 canonical render manifest의 affine으로 PDF user
   space에 변환한다.
4. ODL 좌표 승인은 단순 containment로 결정하지 않는다. unrotated 표의 row 번호에 따른 y
   내림차순, 인접 row 공유 경계, column 번호에 따른 x 오름차순을 확인하고, 별도 합성
   fixture에서 Rotate 90/270, CropBox != MediaBox, UserUnit != 1 왕복을 검증한 뒤에만
   후속 좌표 승인 계약으로 승격한다.
5. `odl_pdf_points_unverified`인 동안 bbox는 production alignment key가 아니다. cached
   평가에서는 좌표 가설별 결과를 비교하되 accepted evidence를 만들지 않는다.
6. NaN, 무한대, 음수 크기, page bounds 초과, page mapping 불일치는 fail-closed한다.

### 6.2 구조 후보 평탄화

- strict Surya: `text`, `section_header`, `list_group`, `table`, `caption`, `footnote`,
  `page_header`, `page_footer`, `diagram`, `picture` region을 읽는다.
- legacy Surya: PascalCase label과 HTML/TableRec를 별도 unverified reader로만 읽고 strict
  artifact인 것처럼 변환하지 않는다.
- ODL: recursive `kids`, `list items`, `rows`, `cells`를 stable page/object ID를 가진 flat
  candidate로 변환한다.
- PageHeader/PageFooter는 삭제하지 않고 `non_semantic` candidate로 보존한다.
- Table/Diagram 영역은 일반 paragraph candidate가 가로질러 소유할 수 없는 hard boundary다.

### 6.3 Geometry-first native alignment

기존 imported ODL adapter처럼 문자열을 먼저 전역 검색하지 않는다.

1. 명시적 page mapping으로 얻은 source page가 같은 native occurrence만 후보로 삼는다.
2. 좌표가 승인된 뒤에만 geometry를 쓴다. text-tight paragraph/list region과 grid-wide table
   cell에 같은 overlap 규칙을 적용하지 않는다. crop 내부, visible/substantive native,
   center·coverage·reading order를 유형별 predicate로 검증한다.
3. column/region과 native source order를 함께 사용해 bounded reading-order sequence를 만든다.
4. evidence composite는 기존 `NativeSourceSpan.separator_after` 계약과 같은 `""` 또는
   `" "`만 허용한다. 둘 다 source index, 동일 line/region과 geometry로 결정되어야 한다.
   `"\n"`과 `"\t"`는 context-only 표시 렌더링에만 허용하며 evidence text가 아니다.
5. materialized evidence text는 항상 native 문자열의 순서 보존 조합이어야 한다.
6. NFKC·공백 제거·OCR 유사도는 후보 탐색과 진단에는 사용할 수 있지만 accepted evidence를
   만들 수 없다.
7. 같은 native occurrence를 여러 leaf가 요구하거나 유일한 결속을 찾지 못하면 accepted가
   될 수 없다. 후보는 충돌 reason과 함께 `partial/context_only` 또는 `rejected/diagnostic_only`로
   결정적으로 분기한다.
8. rowspan/colspan cell의 큰 bbox가 다른 row의 문자열을 흡수하지 않도록 logical text band와
   source order를 별도로 검증한다.

### 6.4 유형별 판정

#### 문단과 제목

- Surya `text`/`section_header` bbox는 outer region 후보로 사용한다.
- ODL paragraph/heading이 같은 native set과 경계를 제안하면 high-confidence accepted다.
- 한 parser만 제안해도 native line spacing·column·hard boundary가 일관되면 context-only
  accepted가 될 수 있다.
- 하나의 Surya Text가 여러 ODL paragraph를 포함하면 ODL 경계로 분할한다.
- ODL paragraph가 여러 Surya region/column/table을 가로지르면 분할 또는 reject한다.

현재 A2.5 구현에서 ODL paragraph와 Surya `text` region의 native 집합 합의는
`pdf_fragment_groups/v1`의 fragment 후보로만 기록한다. 이를 accepted paragraph,
heading 또는 context parent로 해석하지 않는다.

#### 목록

- Surya `list_group`을 외곽 영역으로, ODL list/list item nesting을 논리 계층으로 사용한다.
- bullet marker와 본문은 native occurrence로 결속한다.
- 목록 도입문은 직전 heading/paragraph를 context parent로 참조할 수 있지만 소유하지 않는다.
- 한 행에 병치된 서로 다른 항목을 ODL이 합친 경우 Surya/table geometry로 분할한다.
- 같은 native set을 Surya는 table, ODL은 list로 분류하면 구조 유형은 확정하지 않는다.
  exact native leaf를 보존한 alternative context hypothesis만 만들고 production evidence/table로
  승격하지 않는다. `121019 page 5`는 이 충돌을 검증하는 hard-negative다.

#### 표

- compact context용 table row/cell 후보와 Common IR explicit table 승격을 구분한다.
- strict layout v1에는 table grid가 없으므로 row/cell을 만들지 않는다. cached legacy
  HTML/TableRec은 별도 table-structure artifact 계약을 설계·검증하기 전까지 진단과 Gold
  작성 보조에만 사용한다.
- production explicit table 승격은 ODL·Surya HTML·TableRec의 grid, span, native ownership이
  합의하는 기존의 강한 gate를 유지한다.
- 합의하지 않으면 atomic native evidence는 유지하고 row/header context만 partial로 제공한다.
- OCR cell 문자열은 비교 진단에만 사용한다.

#### native 누락 영역

- Surya/ODL region에 substantive visual text가 있으나 native occurrence가 없으면
  `requires_ocr_semantic_v2`로 기록한다.
- Common IR v1 evidence를 생성하지 않는다.

## 7. 기존 selector 계약으로의 projection

```json
{
  "claim_source_block_ids": ["b17"],
  "context_source_block_ids": ["b15", "b16"],
  "context_policy_version": "pdf-reconstruction-context/v1"
}
```

- 새 local unit/cell anchor wire를 만들지 않는다.
- 복원 leaf 중 기존 SourceBlock exact-span 계약을 만족하는 항목만 claim 후보가 될 수 있다.
- heading/header/list parent와 `context_only` unit은 `context_source_block_ids`에만 들어간다.
- context-only ID가 model output, claim, coverage target, value anchor로 반환되면 기존
  `CONTEXT_ONLY_ANCHOR` 계열 admission 오류로 fail-closed한다.
- SHA, bbox, parser/model version과 full provenance는 model wire에 보내지 않는다.
- 크기는 기존 `canonical-selector-payload-utf8-bytes/v1` 기준으로 측정한다. 2,048-token이라는
  표현은 tokenizer/version을 별도로 고정하기 전에는 합격 수치로 사용하지 않는다.
- 상위 heading 확장은 같은 page/column/hard boundary 안에서 context-only로 한 번만 허용한다.
  cross-page sibling 확장은 이번 범위에서 금지한다.
- 구조 회귀와 별도 selector migration 승인이 통과하기 전에는 packet 생성 및 OpenAI 호출을
  수행하지 않는다.

## 8. 회귀 corpus와 비교 실험

선정 역할과 페이지 범위는
`backend/baselines/pdf_reconstruction/primary_corpus_split_a45.v1.json`으로 동결했다.
split 자체는 Gold·점수를 담지 않고 품질 PASS를 주장하지 않는다. 상세한 digest·검증 명령은
`PDF_PRIMARY_DOCUMENT_VIEW_DESIGN.md`의 A4.5 절을 따른다.

| 공고 | 우선 검증 범위 | 현재 가용성·목적 |
| --- | --- | --- |
| `114788` | page 3~4 | tuning 전용; paragraph·heading·grid·between-rows Gold 준비 |
| `121019` | page 5 | known regression; 기존 43x2 특수 Gold의 신규 evaluator migration 필요 |
| `104102` | full native capture, reviewed page 1/3/5/9 | public held-out; Gold pending |
| `124791` | full native capture, reviewed page 2~4 | public held-out; Gold pending |
| `blind-x-01` | tracked split에는 비공개 | salted commitment로 봉인한 held-out; Gold pending |
| `115310` | page 1~3 | table/grid 0건과 continuation 0건을 확인할 clean-negative; Gold pending |

각 fixture에 대해 다음 ablation을 동일 evaluator로 비교한다.

1. native only
2. native + ODL
3. native + Surya
4. native + ODL + Surya

프로필 Gold와 별개로 선택 페이지에 대한 작은 structural Gold를 **aligner 구현 전에**
동결한다. 최초 `121019 page 5` fixture는 이미지 우선 assisted adjudication 결과이므로
최초에는 `provisional + human_confirmation_required` 상태로 고정했고, 2026-09-19 프로젝트
검토에서 2열·43행·10개 분류/42개 하위 항목·표 아래 설명 3개·footer 분리·원문
`익금불산입`을 확인하여 `human_confirmed`로 전환했다. 다만 parser 결과 자체를 정답으로
간주하거나 production 승격 threshold를 튜닝하는 데 쓰지 않는다. 구현 튜닝에
사용하는 페이지와 최종 점수만 확인하는 held-out 페이지를 분리한다.

- heading span 및 hierarchy
- paragraph boundary
- list group, item, parent
- table bbox, row/column, header, rowspan/colspan
- non-semantic header/footer
- unresolved/cross-page continuation marker

`121019 page 5`의 table/list 충돌은 human-confirmed hard-negative로 동결했다. `114788`의
page 3→4 표는 프로젝트 검토로 page-local grid와 `between_rows` continuation Gold를
동결했지만, 정책을 고친 tuning fixture이므로 held-out 점수에는 포함하지 않는다. 반복
문자열과 subset page mapping도 Gold가 있는 fixture에서만 hard-negative로 판정한다.

## 9. 합격 gate

### 9.1 불변조건

- 같은 입력의 canonical sidecar가 byte-identical
- substantive native occurrence 유실 0
- leaf primary ownership 중복 0
- accepted unit의 native 역추적률 100%
- native에 없는 문자를 accepted materialized text에 추가한 사례 0
- source/artifact hash 또는 좌표 계약 불일치는 fail-closed
- feature off 상태에서 기존 Common IR 및 Profile byte 변화 0
- fixture별 expected candidate 수, 최소 accepted/context-only coverage와 reject reason 분포를
  Gold와 함께 동결한다. substantive native occurrence의 `evidence_atomic` owner가 0건이면
  ownership gate를 통과할 수 없다. 반면 legacy ODL candidate의 accepted 수가 0인 것은
  non-promotable 계약상 정상일 수 있으며, Gold 없는 fixture의 구조 품질 합격을 뜻하지 않는다.

### 9.2 구조 품질

- 선택한 structural Gold의 expected unit tree와 hard-negative disposition을 exact assertion한다.
- 기존 대비 고립된 단어·불릿 fragment 수가 감소해야 한다.
- 이미 올바른 native paragraph/table을 합치거나 분할하는 회귀가 없어야 한다.
- `label + value + 필요한 heading/header`가 같은 context view에 포함되는 Gold fact 수와
  over-merge 수를 함께 측정한다. 공동 출현 증가만으로 GO하지 않는다.
- parser 간 충돌을 억지로 확정한 단위가 없어야 한다.

### 9.3 입력 크기

- `canonical-selector-payload-utf8-bytes/v1` 기준 max/p95를 보고한다.
- 기존 selector budget을 초과하는 unit은 분할 또는 reject한다.
- provenance metadata는 model wire에 0 byte여야 한다.
- tokenizer가 고정되기 전에는 token 수치를 합격 gate로 사용하지 않는다.

## 10. 구현 단계와 변경 경계

### 단계 A0: 좌표·artifact·page identity 계약 고정

1. ODL raw/canonical hash, size/depth/node/string cap, duplicate-key 및 stable-file reader 정의
2. `source_page_count`, `odl_page_count`, `page_scope`, 전단사 page mapping 검증
3. config lineage가 없는 cached ODL과 신규 strict ODL을 구분하는 evaluation envelope
4. ODL 좌표 convention 판별 fixture와 Rotate/CropBox/UserUnit 합성 fixture
5. cached legacy Surya와 strict `surya_layout_artifact/v1`을 구분하는 evaluation envelope
6. HTML/TableRec는 별도 schema가 없음을 명시하고 production 입력에서 제외

이 단계가 통과하기 전에는 alignment code를 작성하지 않는다.

### 단계 A1: baseline 및 structural Gold 선동결

1. native-only fragment, paragraph/list/table candidate와 reject 분포를 동결
2. `121019 page 5` structure/count와 table/list 충돌 disposition 동결 및 사람 확인 완료
3. 114788 top-level root object 71개인 기존 enumerator와 recursive candidate enumerator를 별도 집계
4. 튜닝 페이지와 held-out 페이지 분리
5. PDF 47건 feature-off Common IR/Profile/CandidatePack digest baseline 확인

### 단계 A2: cached offline vertical slice

1. `opendataloader_artifact.py`
   - bounded stable JSON reader
   - source/parser/config/page binding envelope와 schema
2. `structure_candidates.py`
   - ODL recursive node를 textless·unverified traversal candidate로 평탄화
   - strict 신규 ODL binding과 별도 legacy evaluation binding을 구분
3. `reconstruction_plan.py`
   - 승인된 canonical geometry, exact native binding, ownership, reason ledger
4. JSON Schema 및 synthetic unit tests
5. `121019 page 5`와 `114788` fixture 회귀

이 단계에서는 `pdf_native.py`, selector, worker queue, DB, Storage를 변경하지 않는다.

### 단계 A2.5: strict paragraph-fragment consensus

1. `fragment_groups.py`
   - `multi_occurrence_leaf_unverified` ODL paragraph만 재검토
   - ODL과 strict Surya `text` region이 동일한 native occurrence 집합을 제안할 때만
     ID 기반 fragment group 생성
   - 동일 페이지·원자 소유권·연속 substantive source order·유일 rectangular region·
     material overlap 부재·competing paragraph 부재를 모두 요구
2. `pdf_fragment_groups_v1.schema.json`
   - `evaluation_only=true`, `non_promotable=true`
   - semantic text, OCR 문자열, bbox, joiner를 저장하지 않음
3. synthetic mutation 및 artifact-bound deterministic replay 회귀

이 단계의 합의는 문단 복원 승인이 아니다. heading/parent/sibling/column/flow,
목록·표, cross-page continuation, Common IR materialization은 다루지 않는다.
실제 corpus 판정은 114788 전체 7페이지에서 source-bound strict Surya artifact로
재생성·재생했다. strict fragment consensus는 8개 후보를 모두 거절했으며, 이 결과를
문단 복원 실패가 아니라 A3가 별도 label-aware context 계약을 필요로 한다는 disposition으로
고정한다.

### 단계 A3: context view와 평가 CLI

독립 설계 검토와 레드팀 결과, A3 v1은 문단을 합쳐 쓰는 materializer가 아니라
**textless 문맥 가설 인덱스**로 한정한다.

1. `context_groups.py`와 `pdf_context_groups_v1.schema.json`
   - plan의 `partial/context_only` ODL unit을 각각 하나의 독립 group으로 투영
   - A2.5에서 accepted된 fragment가 있으면 각각 하나의 독립 group으로 투영
   - occurrence ID와 source unit/fragment ID만 보존하고 text, bbox, joiner, separator,
     claim/coverage/value anchor를 저장하지 않음
   - parent는 같은 페이지의 직접 parent가 `partial/context_only`일 때 구조 링크 하나만
     보존하며 parent occurrence를 child group에 합치지 않음
   - sibling, child, grandparent, nearest heading 자동 확장 금지
   - table/list/container는 leaf와 병합하지 않는 독립 가설이며 selector materialization 금지
2. deterministic replay
   - 두 입력의 notice/source/page scope와 canonical digest를 결속
   - persisted sidecar는 source/render/ODL/calibration/Surya까지 입력 replay를 통과한 뒤
     context projection을 다시 생성해 canonical byte identity를 비교
   - standalone validator는 계속 `internal_consistency_only`
3. `run_pdf_context_projection.py`
   - 검증을 마친 reconstruction plan·fragment groups와 create-only output 경로를 받는
     offline CLI
   - 상류 replay 완료에 대한 명시적 operator acknowledgement를 요구하지만, CLI 자체는
     replay나 인증을 대신하지 않음
   - 결과를 mode `0600`으로 생성하고 그룹 수, membership 수·중복도, parent suppression,
     canonical hash·크기만 보고
   - semantic text나 실제 selector packet, LLM 호출을 출력하지 않음

114788의 기대값은 plan group 101개, fragment group 0개, aggregate membership 214개,
unique native occurrence 129개, 최대 group membership 37개다. plan의 parent edge 78개 중
77개는 diagnostic parent라 억제하고, 허용 가능한 direct parent link는 1개뿐이다. 그
list item은 occurrence 한 개만 유지하며 16개 occurrence인 parent list 본문을 절대
끌어오지 않는다.

이 단계에서도 실제 selector packet과 LLM 호출은 생성하지 않는다.

### 단계 B: 4건 corpus gate

1. 선동결된 structural Gold를 변경하지 않고 4-way ablation report 생성
2. deterministic replay와 mutation/collision tests
3. context input-size report
4. held-out을 포함한 4건 gate 통과 여부 판정

### 단계 C1: runtime과 분리된 shadow 평가

- 기본 서비스 모드는 계속 `off`이며 feature flag를 추가하지 않는다.
- sidecar와 metric은 평가 디렉터리에만 저장한다.
- Common IR, CandidatePack, Profile, 공개 DTO, DB/Storage artifact·lineage digest가 변하지 않는지
  PDF 47건 전체에서 확인한다.

### 단계 C2: selector 연결과 generator migration — 별도 승인

- CandidatePack/context generator version을 올린다.
- `StructuralWorkUnitV1`, `context_source_block_ids`, existing exact-span anchor와 admission을
  재사용한다.
- unit ID, coverage target ID, call-plan digest, baseline과 Gold를 재동결한다.
- context-only block이 claim/coverage/value anchor가 되지 않는 통합 테스트를 선행한다.
- 이는 feature flag만 켜는 변경이 아니며 별도 사용자 승인 없이는 구현하지 않는다.
- production Common IR explicit table 승격도 별도 승인·migration 대상으로 남긴다.

### 단계 D: 신규 문서 및 OCR semantic v2

- pinned JRE 17 + OpenDataLoader 2.5.7 CPU runtime을 격리 배포한다.
- 신규 PDF에서 RunPod Surya artifact를 생성해 corpus 외 검증을 수행한다.
- image-only/garbled-native PDF는 Common IR v2 RFC 이후에만 OCR evidence를 허용한다.

## 11. 테스트 전략

### 단위 테스트

- malformed/oversized/symlink/swapped ODL artifact 거절
- duplicate JSON key, NaN/Infinity, depth/node/string cap 초과 거절
- duplicate page/object ID 거절
- missing/non-bijective ODL-to-source page mapping 거절
- bbox transform round trip
- wrong page/rotation/crop/coordinate space 거절
- exact native composition과 evidence/context joiner 분리
- normalized-only/OCR-only/ambiguous string 거절
- column/table/list hard boundary 보존
- duplicate leaf ownership 거절
- rowspan/colspan의 큰 grid bbox가 인접 row text를 흡수하지 못함
- table/list가 같은 native set을 요구할 때 혼합 accepted 금지
- deterministic canonical JSON

### fixture 테스트

- 121019 page 5: exact native leaf 보존, table/list conflict disposition, OCR 오자 비승격
- 114788: root object 71개 중 기존 44 exact bind 관측과 recursive 268개 후보를 분리해 비교
- 124791 source pages 2~4: heading/list/table 경계
- 104102 full/subset: 같은 page의 단위가 scope와 무관하게 동일

### property/fuzz 테스트

- JSON object key 순서 변화에는 canonical 결과가 불변
- ODL sibling 배열 순서는 parser traversal 입력으로 결속하되, 검증된 reading order로
  이름 붙이거나 해석하지 않음
- bbox perturbation 임계점에서 fail-closed
- duplicated text와 overlapping candidate에서 unique ownership 보장
- page/header/footer/table 경계를 넘는 조합 금지
- 같은 문자열이 2개 이상인 경우 geometry만으로 임의 확정 금지
- one-page extract의 object page가 원문 page 5를 유지하는 page-map fixture
- feature-off PDF 47건 Common IR/Profile/CandidatePack digest parity

## 12. 보안·운영 경계

- PDF 안 문자열은 신뢰할 수 없는 데이터이며 prompt instruction으로 취급하지 않는다.
- ODL은 OCR disabled로 시작하고 EC2/서버 CPU subprocess에서 실행한다.
- RunPod에는 DB URL, service-role key, 사용자 JWT를 전달하지 않는다.
- 현재 cached 회귀에는 RunPod가 필요하지 않으며 shutdown 상태를 유지할 수 있다.
- 원격 parser 결과는 source/artifact SHA, schema, size, coordinate contract를 검증한 뒤에만
  사용한다.

## 13. 외부 리뷰 수용 결정

### 13.1 수용

- A0 좌표·artifact·page identity gate를 alignment 구현보다 앞에 둔다.
- structural Gold와 native-only baseline을 aligner보다 먼저 동결하고 held-out을 분리한다.
- cached legacy Surya와 strict textless Surya v1을 구분하며 HTML/TableRec를 별도 계약 없이
  사용하지 않는다.
- ODL envelope에 explicit page mapping과 canonical JSON 알고리즘을 넣는다.
- evidence joiner는 기존 계약의 `""`/`" "`만, newline/tab은 context-only로 제한한다.
- 새 selector anchor를 만들지 않고 기존 SourceBlock/StructuralWorkUnit/admission을 재사용한다.
- context-only가 claim/coverage/value anchor로 승격되는 경로를 fail-closed한다.
- table/list 충돌과 rowspan ownership을 hard-negative로 검증한다.
- accepted 0건으로 green이 되지 않도록 fixture별 coverage와 content assertion을 동결한다.
- runtime shadow와 selector generator migration을 C1/C2로 분리하고 C2는 별도 승인 대상으로
  둔다.

### 13.2 수정 수용

- Grok은 `121019 page 5`에서 table/list를 모두 reject하라고 권고했다. 구조 유형을 production
  evidence로 확정하지 않는 데 동의한다. 다만 exact native leaf와 두 alternative hierarchy는
  context-only/diagnostic 비교를 위해 보존한다.
- Grok은 unverified ODL 좌표를 전혀 쓰지 말라고 권고했다. production accepted alignment에는
  쓰지 않는다. A0의 cached coordinate hypothesis 비교에는 사용하되 결과 상태를 unverified로
  고정한다.
- Opus는 single parser + native geometry를 context-only로 허용했다. 좌표 계약과 unique
  binding을 모두 통과한 경우에만 허용하고 evidence composite에는 parser 간 합의를 요구한다.

### 13.3 보류·거절

- 문단·목록 복원 자체를 후속으로 미루라는 레드팀 권고는 이번 목표와 맞지 않아 거절한다.
  대신 production과 완전히 격리된 offline context-only vertical slice로 제한한다.
- 2,048/1,024 token gate는 현재 selector가 UTF-8 byte budget을 사용하므로 보류한다. 향후
  tokenizer/version을 명시할 때 다시 도입한다.
- cached legacy 4건의 기존 평가를 재현하는 데 RunPod 재실행은 필수가 아니다.
  114788 A2.5 corpus 검증에는 canonical rerender와 source-bound strict Surya rerun을
  수행했다. 추가 corpus와 production 승격에는 동일한 lineage 검증을 반복해야 한다.

## 14. 구현 시작 판정

A0~A2와 A2.5 합성 계약·단위 회귀, 114788 source-bound strict Surya corpus replay,
A3 textless context hypothesis 계약·replay, A4.1~A4.3 평가 vertical slice와 A4.5 split
동결, A4.6 공개 case exporter까지 구현했다. 다음은 실제 strict Surya artifact·Gold 생성과
corpus gate다.
C2와 production explicit table 승격은 이번 자동 구현 범위에 포함하지 않는다.

## 15. 2026-09-19 구현 결과와 다음 gate

### 15.1 완료

- 신규 ODL용 `opendataloader_artifact/v1`
  - source/parser config/page mapping, raw byte SHA와 canonical JSON SHA 분리
  - duplicate key, 비유한 수, surrogate, 크기·깊이·노드·문자열 cap, symlink 및 읽는 중
    파일 변경을 fail-closed
  - 좌표 상태는 `odl_pdf_points_unverified`만 허용
- 과거 cached ODL용 `legacy_opendataloader_evaluation/v1`
  - 실행 config가 없는 cache를 strict provenance로 가장하지 않음
  - 평가 전용·승격 불가 metadata envelope로 분리
- 과거 cached Surya용 `legacy_surya_layout_evaluation/v1`
  - geometry, label, 배열 순회 순서만 노출
  - 순서는 `legacy_traversal_order`와
    `ordering_status=legacy_surya_traversal_unverified`로 표시하고 `reading_order`로 주장하지 않음
  - OCR 문자열, HTML, cell text는 내보내지 않음
  - 직접 생성 객체와 envelope도 label 대응, page 상한, 중복 identity, dense traversal,
    bbox를 재검증하고 무시 필드의 비유한 수도 거절
- `pdf_structural_gold/v1`의 `121019 page 5` human-confirmed fixture
  - `image_first_assisted_review`, `review_status=human_confirmed`, `human_confirmation_required=false`
  - 2026-09-19 프로젝트 검토에서 구조·주석·footer·`익금불산입` 원문 확인
  - confidence 0.8은 표 bbox가 허용 오차를 가진 근사값임을 나타내며 확인 상태와 분리
  - 표의 구조·개수와 table/list 충돌만 동결하고 42개 항목 문구를 Gold로 위장하지 않음
  - 실제 존재하는 render manifest와 stage record SHA를 결속
  - 역사 PNG 본체가 없어 stage에 기록된 PNG SHA는
    `stage_recorded_not_locally_rehashed`로 표시하고 검증된 digest로 가장하지 않음
- `pdf_structure_candidates/v1`
  - ODL의 heading/paragraph/list/list item/table/row/cell을 재귀적으로 평탄화
  - 어떤 `content`나 OCR 문자열도 출력하지 않음
  - `ordering_status=odl_traversal_unverified`로 고정하고 `reading_order`를 주장하지 않음
  - bbox 역시 `bbox_odl_pdf_points_unverified`로만 보존
  - 모든 결과는 `evaluation_only=true`, `non_promotable=true`

실제 cached `PBLN_000000000121019` page 5를 replay한 결과는 총 63개 후보였다.

| 종류 | 개수 |
| --- | ---: |
| heading | 1 |
| list | 8 |
| list_item | 51 |
| paragraph | 3 |

외부 raw의 의미 문자열을 저장소에 복사하지 않고 구조 key만 남긴 textless 파생 fixture를
추가했다. CI는 이 fixture로 현재 projector를 실제 재실행하여 63개 분포, candidate ID 순서,
kind 순서와 canonical projection hash를 exact assertion한다. 외부 raw/projection의 관측 SHA와
파생 fixture/projection SHA는 별도 필드로 분리했다.

canonical sidecar에는 `content`와 `reading_order`가 모두 없음을 확인했다. 실제 cached legacy
Surya도 5페이지 45영역으로 replay하여 같은 금지를 확인했다.

- ODL 좌표 convention calibration
  - 합성 fixture 7종으로 Rotate 0/90/180/270, `CropBox != MediaBox`, `UserUnit=2`,
    Rotate 270 + UserUnit 2 복합 조건을 검증
  - OpenDataLoader 2.5.7, OCR-off local Java 경로를 각 fixture에 2회씩 총 14회 실행
  - 14회 모두 raw byte와 canonical JSON repeat identity 통과
  - 35개 anchor를 모두 찾았고 선택된 단일 convention은
    `rotated_crop_relative_bottom_left_raw_units`
  - 의미: ODL bbox는 CropBox 원점 기준, `/Rotate` 적용 후, bottom-left y축이며 숫자는
    `/UserUnit`을 곱하지 않은 raw user unit
  - `/UserUnit`은 상속 속성이 아니므로 합성 PDF의 leaf `/Page`에 직접 기록하고 renderer도
    leaf 값만 읽도록 교정한 뒤 14회를 전부 재실행
  - 선택 convention의 최대 물리 중심 오차 0.340pt 미만, 최대 bbox 변 오차 0.285pt 미만,
    최소 IoU 0.9509 초과로 center/edge/IoU gate 통과
  - fixture/source/render/ODL output/proof와 ODL JAR·package config·OpenJDK 17.0.20
    executable/provided-package-file/version 출력을 별도 machine-bound baseline에 결속
  - 각 독립 실행마다 source/정규화 argv/JAR/JRE/raw output hash를 묶은 immutable
    `run_manifest.json`을 저장하고 cached 재평가도 이를 검증
  - calibration 범위는 동일 길이 Courier paragraph bbox이며 table/cell/image 등 다른 ODL
    node type까지 검증했다는 주장은 하지 않음
  - 이 proof는 별도 `opendataloader_coordinate_calibration/v1`이며 기존
    `opendataloader_artifact/v1`의 `odl_pdf_points_unverified` 값을 변경하지 않음

Common IR 패키지 표준 소스 회귀는 JUnit 기준 243 testcase 중 242건 통과, build-capable
wheel 검증 1건 정상 skip으로 완료했다. pytest 수집 기준은 146개 test function이며 unittest
subtest가 JUnit testcase로 각각 집계된다. 같은 wheel 검증을 `COMMON_IR_VERIFY_WHEEL=1`인
빌드 가능 환경에서 별도로 실행해 신규 PDF fusion schema가 wheel에 포함되는 것도 통과했다.

독립 코드 리뷰·레드팀에서 잘못 상속한 `/UserUnit`, cached-run producer identity, source
binding, path traversal, runtime/schema parity, 중심점-only gate와 무제한 duplicate collapse를
찾았다. 모두 fail-closed 검증과 회귀로 보강한 뒤 실제 ODL 14회를 다시 실행했다.

- native-first `pdf_reconstruction_plan/v1`
  - source-bound strict native capture와 canonical render, textless ODL candidate, reviewed
    coordinate proof의 canonical SHA를 하나의 평가 sidecar에 결속
    - 여기서 proof SHA는 pretty JSON 파일 byte SHA(`b7b7a7e...`)가 아니라 canonical JSON
      SHA(`f8c041e...`)이며 plan의 두 proof SHA 필드는 모두 후자를 저장
  - page scope 안의 유효 native occurrence마다 정확히 하나의 `accepted/evidence_atomic`
    primary owner 생성
  - ODL candidate는 occurrence를 context로만 참조하며 primary ownership과
    `evidence_composite` 생성 금지
  - legacy ODL은 producer/config가 machine-bound가 아니므로 최대
    `partial/context_only`; strict라고 주장하는 projection은 underlying parser artifact 없이
    JAR/config/source lineage를 재검증할 수 없으므로 v1 경계에서 거절
  - 중심 포함과 native bbox 95% 이상 coverage를 함께 요구하고, 같은 후보가 다른 열/행의
    native atom을 삼키면 `cross_column_or_row_merge`로 거절
  - 여러 native atom을 참조하지만 명백한 열 병합도 안전한 bullet split도 아닌 leaf는
    `multi_occurrence_leaf_unverified`로 거절해 근거 없이 cross-row/column으로 단정하지 않음
  - bare `□`와 바로 뒤 본문이 같은 baseline·연속 source index·12pt 이내일 때만
    `split_bullet_marker` context로 보존하며 checked 상태는 추론하지 않음
  - semantic text는 plan에 복사하지 않고 occurrence ID로 strict native capture를 역참조
  - standalone 검증은 `internal_consistency_only`로 명시하고, 신뢰 경계에서는 모든 입력을
    재검증·재생성하는 `validate_reconstruction_plan_against_inputs`의 byte identity를 요구
  - native text 0건은 `requires_ocr_semantic_v2`로 표시하고 ownership gate 실패 처리
- exact fragment consensus용 `pdf_fragment_groups/v1`
  - `multi_occurrence_leaf_unverified` ODL paragraph와 strict Surya `text` region이 동일한
    native occurrence 집합을 제안할 때만 ID 기반 후보를 생성
  - 동일 페이지, 원자 소유권, 연속 source order, 직사각형 region, material overlap·경쟁
    proposal 부재를 모두 fail-closed로 요구
  - textless·evaluation-only·non-promotable이며 문단/Common IR로 승격하지 않음
  - 합성 fixture 집중 회귀 8개와 artifact-bound deterministic replay 검증을 통과

실제 `121019 page 5` 외부 원본을 strict native capture와 current canonical renderer로 다시
처리한 결과는 다음과 같다. 비재현 review attestation fingerprint만 저장소에 두고 원문
PDF/텍스트는 복사하지 않았다. 따라서 이 fingerprint는 외부 source-bound artifact 재실행을
대체하지 않는다.

| 지표 | 결과 |
| --- | ---: |
| scoped/substantive native occurrence | 60 / 60 |
| atomic evidence owner | 60 |
| unowned / duplicate primary owner | 0 / 0 |
| ODL structure candidate | 63 |
| partial context-only | 59 |
| rejected diagnostic-only | 4 |
| evidence composite | 0 |

plan의 `native_ownership_gate_status`는 substantive native occurrence가 최소 1건 존재하고,
그 occurrence에 유실·중복 소유가 없는지를 판정한다.
ODL 정렬 품질이나 fixture coverage의 합격을 뜻하지 않는다. 후보 coverage와 reason 분포는
9절의 별도 fixture/corpus gate에서 판정한다.

거절된 traversal order는 `2, 20, 54, 55`로, 두 열 header 또는 분류명과 옆 칸 자식을 한
leaf로 합친 경우다. order `44`의 분리된 `□`+본문만 context로 보존했다. 기존 selector의
complete-list-run 확장에 이 unit을 그대로 연결하면 긴 목록 전체가 다시 prompt에 들어가므로,
A3에서는 `claim leaf + immediate parent 최대 1 + nearest heading 최대 1`, sibling 확장 금지인
별도 versioned context policy가 필요하다.

같은 판정 규칙을 `114788` 전체 7페이지에 held-out으로 적용했다. 원문 PDF와 raw ODL은
저장소 밖에 있으므로 결과는 non-reproducible review attestation으로만 동결했다. 외부 입력이
있는 환경에서 `validate_render_manifest_files`와
`validate_reconstruction_plan_against_inputs`를 실행한 artifact-bound deterministic replay는
모두 통과했다.

| 지표 | 결과 |
| --- | ---: |
| scoped / substantive native occurrence | 230 / 218 |
| atomic evidence owner | 218 |
| unowned / duplicate primary owner | 0 / 0 |
| recursive ODL structure candidate | 268 |
| partial context-only | 101 |
| rejected diagnostic-only | 167 |
| evidence composite | 0 |

구형 ODL shadow의 `71개 중 exact bind 44개`는 root object만 순회한 기존 enumerator 관측값이다.
현재 recursive projector에는 table cell 79개를 포함한 typed candidate 268개가 있으므로 두 수치를
같은 분모의 품질 지표로 비교하지 않는다. 명백한 열·행 병합 후보는 traversal order `63, 88`
두 건이며 모두 diagnostic-only로 유지됐다.

이 결과의 `native_ownership_gate_status=passed`는 218개 native 근거가 유실·중복 없이 보존됐다는
뜻일 뿐이다. 이 attestation에는 semantic Gold를 결속하거나 의미 품질을 평가하지 않았고,
human-confirmed structural Gold도 없다. 특히 page 3과 page 4 표의 cross-page continuation은
아직 context 관계로 정의하지 않았다. 따라서 문단·표 복원 품질과 production 승격은 모두
미승인 상태다.

같은 114788 canonical render 7페이지를 RunPod Surya 0.22.1에 전달해 strict
`surya_layout_artifact/v1`을 생성한 뒤 A2.5를 artifact-bound replay했다. 64개 region과
모든 page/sidecar/hash/좌표 결속은 검증을 통과했지만, exact fragment consensus는 다음처럼
fail-closed했다.

| A2.5 지표 | 결과 |
| --- | ---: |
| eligible ODL paragraph | 8 |
| accepted fragment group | 0 |
| rejected: non-contiguous substantive order | 3 |
| rejected: no unique Surya `text` region | 5 |

연속 순서인 5개 후보의 가장 가까운 region label은 `text` 1개, `list_group` 3개,
`table` 1개였다. 경계 부족분은 0~11.002px였으나, 유일한 `text` 후보도 다른 native
occurrence를 함께 포함했다. 따라서 tolerance만 늘려 승인하면 안 된다. `text` label과
exact native-set veto는 A2.5에 유지하고, `list_group`·`table`은 A3의 context-only
가설로 분리한다.

나머지 non-contiguous 3개는 장식·빈 atom이 아니라 표의 좌·우 sibling cell 문자열이
native source order에서 교대로 나타난 경우였다. 각 ODL paragraph는 자기 cell의 첫 줄도
누락한 under-complete 후보였다. global contiguity를 완화하지 않고, 향후 별도 table-aware
계약에서 cell 전체 membership과 좌측 label 관계가 검증될 때까지 diagnostic-only로 둔다.

이 실행의 producer config는 평가 config와 일치하지만 `worker_image_digest` 값은 실제 OCI
manifest digest임이 증명되지 않은 legacy digest-shaped identifier다. 그러므로 결과는
`evaluation_only=true`, `non_promotable=true` 범위에서만 사용하며 production provenance나
Common IR 승격 근거로 사용하지 않는다. 원문 없는 frozen disposition은
`strict_fragment_consensus_114788_full.fingerprint.v1.json`에 기록했다.

A3 `pdf_context_groups/v1`은 이 입력에서 다음과 같이 재생됐다.

| A3 지표 | 결과 |
| --- | ---: |
| plan context group | 101 |
| accepted fragment context group | 0 |
| aggregate / unique occurrence membership | 214 / 129 |
| group당 최대 reference | 37 |
| 허용 direct parent link | 1 |
| diagnostic parent link 억제 | 77 |
| canonical JSON size | 44,496 bytes |

이 결과는 101개 unit을 병합한 것이 아니다. 각 unit을 별도 textless 가설로 보존했고,
parent는 ID link만 남겼으며 parent의 occurrence는 child에 추가하지 않았다. 원문 문자열,
bbox, joiner, heading 추론, sibling 확장, selector/LLM payload는 생성하지 않았다. canonical
SHA와 지표는 같은 frozen fingerprint에 함께 기록했다.

### 15.2 아직 구현하지 않은 것

- 104102·124791·121019·clean-negative의 실제 strict Surya artifact 생성·replay
- sealed blind의 reveal 이후 source-bound native/render/strict artifact 생성·replay
- heading/list/table/section, column·flow·hard-boundary conflict 결합
- held-out과 clean-negative의 human-confirmed partial structural Gold
- 문단/context materialization과 selector byte/work budget gate
- Common IR block 생성 또는 교체
- Existing/Request selector와 LLM 입력 연결
- worker queue, DB, Storage, 공개 API 변경
- OpenDataLoader 운영용 격리 runtime 배포

현재 구현은 shadow alignment·ownership ledger, exact-fragment consensus와 textless context
hypothesis 계약, 114788 실제 7페이지 replay, 공개 case의 안전한 export 경계까지 만든
상태다. 104102·124791·121019·115310은 native/render preflight만 통과했고 실제 Surya 결과는
아직 없다. 문단·표 materialization 품질과 production 승격은 여전히 미승인이다.

### 15.3 다음 진행 조건

1. 완료: ODL 좌표 convention을 Rotate/CropBox/UserUnit fixture로 calibration했다.
2. 완료: native-first alignment와 ownership ledger를 121019에서 검증했다.
3. 완료: 114788 held-out에서 같은 판정 규칙과 artifact-bound deterministic replay를 검증했다.
4. 완료(합성 fixture): strict ODL paragraph와 Surya `text` region의 exact fragment
   consensus 계약을 구현했다.
5. 완료(114788): strict Surya artifact를 재생성하고 `pdf_fragment_groups/v1`
   deterministic replay와 fail-closed disposition을 동결했다.
6. 완료(114788): `partial/context_only` unit과 accepted fragment를 병합 없이 투영하는
   bounded `pdf_context_groups/v1`을 구현·재생했다.
7. 완료(공개 4건): 104102·124791·121019·115310의 source-bound native capture와 canonical
   render를 split에 결속해 preflight하고, create-only strict Surya exporter를 구현했다.
8. 대기: deterministic Storage cache miss이면 RunPod을 기동해 공개 4건 strict artifact를
   실제 생성한다.
9. 대기: 승인된 blind reveal 환경에서 sealed case의 source-bound native/render/strict
   artifact를 생성하고, 공개·봉인 case의 선언 scope에 필요한 Gold를 확정한다.
10. 대기: 모든 6건의 artifact와 Gold가 준비되면 exact 구조 평가, context coverage,
   over/under-merge와 byte/work budget을
   판정한다. 이를 통과하기 전에는
   Common IR/selector/runtime에 연결하지
   않는다.

114788 strict artifact는 확보했으므로 이 문서의 A3 설계·로컬 replay 동안 RunPod는 꺼도
된다. 추가 corpus artifact에 유효한 deterministic Storage cache가 없을 때만 다시 기동하면
된다. cached legacy Surya 결과는 strict 입력을 대신할 수 없다.
