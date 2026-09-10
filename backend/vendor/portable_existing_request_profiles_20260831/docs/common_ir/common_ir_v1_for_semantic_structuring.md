# Common IR v1 구조화 입력 안내

이 문서는 공고문과 사전협의 Request 구조화 JSON을 만드는 담당자를 위한 Common IR v1 입력 계약이다. **2026-08-30 기준 이 계약은 고정**이며, Request를 위해 별도 IR dialect를 만들지 않는다.

## 입력 범위

동일 공고의 HWP/HWPX와 PDF는 **서로 독립된 Common IR**이다. 두 문서를 합치거나 한 포맷의 내용을 다른 포맷 결과에 보충하지 않는다. 포맷별로 구조화 결과를 만들고, 비교는 이후 평가 단계에서 한다.

대응쌍 입력 루트:

```text
/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/runs/common_ir_v1_pair_expansion_20260828/hwp

/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/runs/common_ir_v1_pair_expansion_native_only_20260828/pdf
```

## 최상위 구조

```json
{
  "schema_version": "common_ir_v1",
  "document": {},
  "blocks": [],
  "conflicts": [],
  "relations": []
}
```

### `document`

문서 식별·재현성 정보다.

| 필드 | 의미 |
|---|---|
| `document_id` | 예: `hwp:PBLN_...`, `pdf:PBLN_...` |
| `source_kind` | `hwp`, `hwpx`, `pdf`, 또는 초기 fixture 입력용 `markdown_fixture` |
| `artifact_role` | 현재 생산용 결과는 `production` |
| `page_count` | PDF는 페이지 수, HWP/HWPX는 `null` 가능 |
| `raw_artifact_ids` | 이 결과를 만든 원시/보강 증거 파일 목록 |
| `provenance` | 생성 방법·원본 위치·SHA-256·생성기 버전 |

`document_id`는 Common IR 문서의 식별자이며 business ID와 같은 뜻이 아니다. 같은 공고 ID라도 HWP/HWPX/PDF는 각각 독립 source document와 독립 Common IR을 가진다. `document.provenance`에는 최소 아래 계보가 있어야 한다.

`markdown_fixture`는 Request 초기 검증처럼 실제 원본 포맷이 아직 없는 경우에만 쓰는 **일반 source-format capability**다. Request 전용 IR dialect나 업무 의미를 추가하지 않으며, HWP/HWPX/PDF production 경로를 대체하지 않는다.

| provenance 필드 | 의미 |
|---|---|
| `source_location`, `source_sha256` | 실제 원본 artifact 위치와 SHA-256. raw parser JSON이 아닌 원본 HWP/HWPX/PDF가 기준이다. |
| `generator`, `generator_version`, `schema_version` | Common IR adapter 및 계약 버전 |
| `parser`, `parser_version` | 원본을 읽어 raw IR을 만든 upstream parser. HWP/HWPX는 `rhwp`, PDF는 `pdf_inspector` |
| `parser_core_version` | rhwp native/core 버전. 원본 parser가 제공한 경우만 HWP/HWPX에 기록하며 PDF에는 만들지 않는다. |

`raw_artifact_ids`는 재생성 가능한 Format IR·layout evidence의 위치 목록이다. 이 중간 artifact의 hash나 독립 schema/version은 Common IR document lineage의 필수 계약이 아니다. 재현의 기준점은 **원본 artifact의 SHA-256**과 원본 parser/version, Common IR generator/version, `schema_version`이다.

## `blocks`: 구조화의 기본 입력 단위

각 block은 한 문단, 제목, 표 또는 도식 후보이며, 구조화 모델은 새 원문을 만들지 말고 이 block/cell의 ID를 근거로 선택해야 한다.

```json
{
  "block_id": "hwpx:t131.c0.b1",
  "kind": "table",
  "structure_status": "explicit",
  "text": "블록의 정규 원문 텍스트",
  "text_occurrence_ids": ["occ:..."],
  "reading_order": 144,
  "page": null,
  "section_path": "section[0]/para[131]",
  "occurrences": [],
  "cells": [],
  "boundary_markers": [],
  "provenance": {}
}
```

| 필드 | 구조화 시 사용법 |
|---|---|
| `block_id` | 사실의 직접 근거 block ID |
| `kind` | `paragraph`, `heading`, `table`, `table_candidate`, `diagram_candidate` |
| `text` | block의 정규 원문. 표는 셀의 직접 텍스트를 조합한 검색·표시용 값이며, 안정적인 row/column 직렬화 형식은 아니다. 조건–값 관계는 반드시 `cells`의 row/column/span과 occurrence를 사용한다. |
| `structure_status` | `explicit`: 구조가 근거로 확인됨. `partial`: 텍스트는 있으나 표/도식 관계는 확정하지 않음 |
| `reading_order` | 문서 내 정렬 순서. PDF는 페이지 내 순서를 포함함 |
| `page` | PDF 페이지 번호(1부터), HWP/HWPX는 `null` 가능 |
| `section_path` | HWP/HWPX 원본 구조 경로. PDF에서는 빈 문자열일 수 있음 |
| `boundary_markers` | `서식`, `붙임`, `별첨` 표식. 범위 제외 여부를 판단하는 단서일 뿐 자동 제외 명령은 아님 |
| `source_block_label` | HWP/HWPX에서 문단으로 투영된 원래 타입(예: `list_item`, `field`, `caption`) |

## `occurrences`: 원문 근거와 출처

`occurrences`는 block/cell 텍스트가 어디서 왔는지 기록한다. 사실에는 `block_id`와 필요한 `occurrence_id`를 함께 근거로 남긴다.

| `role` | 의미 |
|---|---|
| `rhwp_text`, `rhwp_cell` | HWP/HWPX 원본 구조 파싱 결과 |
| `native_text` | PDF 텍스트 레이어 추출 결과 |
| `layout_region` | PDF 표·도식 등의 위치·구조 근거. 텍스트는 없음 |

모든 occurrence에는 `provenance`가 있으며, PDF는 보통 `page`, `bbox`, `coordinate_space`를 가진다.

### ID와 provenance 범위

- `block_id`, `cell_id`, `occurrence_id`, `relation_id`는 **Common IR document-local** ID다. 서로 다른 PDF/HWP/HWPX 문서 간 global namespace를 만들지 않는다.
- `cell_id`는 해당 문서의 table 구조 안에서만 의미가 있으며, `table_contains`는 parent `cell_id`에서 child table `block_id`를 가리킨다.
- `occurrence_id`는 원문 occurrence/provenance ID다. block/cell과 문자열 prefix가 비슷할 수 있으므로, 소비 측은 문자열 모양이 아닌 node type으로 해석한다.
- 후단 evidence의 `source_block_ids`, `source_cell_ids`, `occurrence_ids`는 Common IR node ID를 참조한다. 반면 `value_source.source_block_id`는 CandidatePack block ID다. 두 문자열이 우연히 같을 수 있어도 서로 다른 namespace이며, Common IR에는 Structured Fact ID를 넣지 않는다.
- HWP/HWPX의 block/cell/occurrence locator는 동일한 `source_sha256`, `source_kind`, parser/version, Common IR generator/version에서 재현되는 것을 목표로 한다. 원본·parser·adapter 버전 중 하나라도 달라지면 기존 locator를 이식하지 않고 새 Common IR과 새 CandidatePack을 생성한다. CandidatePack block locator까지 재현하려면 동일 CandidatePack generator/version도 필요하다.

## `cells`: 명시 표의 셀

`kind: table` block만 `cells`를 가질 수 있다. 표의 조건–금액, 항목–배점처럼 행/열 관계가 분명할 때는 표 전체 `text`가 아니라 해당 cell을 근거로 사용한다.

```json
{
  "cell_id": "hwpx:t131.c0.b1:c6",
  "row_index": 2,
  "col_index": 6,
  "row_span": 1,
  "col_span": 1,
  "text_occurrence_ids": ["occ:..."],
  "evidence_ids": ["occ:..."],
  "provenance": {}
}
```

`table_candidate`에는 신뢰 가능한 cell grid가 없을 수 있다. 이 경우 값은 텍스트 근거로 보존하되, 행·열 대응을 새로 확정하지 않는다.

행은 별도 node나 ID가 아니다. 명시 table에서 cell이 점유하는 행 범위는 `[row_index, row_index + row_span)`이고, 열 범위는 `[col_index, col_index + col_span)`이다. 후단이 동일 표 행의 구조적 동거 여부를 확인해야 한다면 같은 table `block_id` 안에서 이 범위를 사용한다. actor/role/action 등의 업무 의미 판정은 이 정보를 소비하는 Structured Profile의 책임이다.

## CandidatePack과 exact-span 인터페이스

Common IR은 원문 구조의 기준점이고, CandidatePack은 Common IR의 문단 또는 명시 table의 **셀 문단**을 구조화 입력으로 결정적으로 투영하는 별도 소비 artifact다. CandidatePack은 원문을 요약하거나 공백을 정규화해서는 안 된다.

Raw Fact의 exact span은 Common IR의 임의 `blocks[].text`가 아니라 CandidatePack block의 `text`를 기준으로 한다.

```text
value_raw == candidate_pack_block.text[start_char:end_char]
text_basis = common_ir_v1_candidate_pack
```

- offset은 Python Unicode code-point offset이다.
- `start_char`는 inclusive, `end_char`는 exclusive다.
- 일반 문단 CandidatePack block은 원 Common IR paragraph/heading block을 투영한다.
- 표 값 CandidatePack block은 table 전체의 평탄 `blocks[].text`가 아니라 셀 문단 원문을 투영한다.
- CandidatePack lineage는 `candidate_pack_id`, `candidate_pack_generator`, `candidate_pack_generator_version`, `common_ir_document_id`, `common_ir_source_sha256`로 구성된다. 앞의 네 항목은 CandidatePack artifact에 보존한다. `common_ir_source_sha256`은 Common IR `document.provenance.source_sha256`에 바인딩되는 값이며, pack이 hash를 복제하지 않는 구현은 selection input/manifest 또는 resolver request에서 이를 보존하고 로드한 Common IR hash와 대조해야 한다. 각 CandidatePack block은 원 Common IR `block_id`, 필요 시 `cell_id`와 `occurrence_id` 목록을 보존해야 한다. CandidatePack block 자체의 ID와 Common IR block ID가 다른 경우에도 이 provenance 연결을 생략해서는 안 된다.
- Structured JSON은 `text_basis`와 CandidatePack block을 통해 값 span을 검증하고, Common IR `block_id`·`cell_id`·`occurrence_id`를 evidence/provenance로 함께 보존한다.

따라서 표 전체 `blocks[].text`는 검색·표시용이며 exact span 기준이 아니다. `cell_id`는 표 내부 위치·행열 관계의 근거이고, 값을 단독으로 복원하는 text basis가 아니다.

### 반복 anchor 해소: CandidatePack Anchor Occurrence Resolver v1

구조화 모델은 character offset이나 n번째 occurrence를 작성하지 않는다. 모델이 `source_block_id + anchor_text`를 선택하면 서버가 canonical CandidatePack block text에서 exact substring을 찾는다.

- 유일한 occurrence는 기존 exact-span 경로로 즉시 `value_source`를 복원한다.
- 같은 `anchor_text`가 두 번 이상 실제로 나타날 때만 resolver가 후보를 생성한다. 서버는 첫 occurrence를 임의로 선택하지 않는다.
- resolver 입력에는 `common_ir_document_id`, `common_ir_source_sha256`, `candidate_pack_id`, CandidatePack `generator`/`generator_version`, CandidatePack `source_block_id`, `anchor_text`가 모두 필요하다.
- 후보 ID는 Common IR 영구 node ID가 아니라 CandidatePack-local이며, 위 lineage와 block/span으로 결정적으로 재생성된다.
- 후보에는 서버 검증용 `start_char`/`end_char`, Common IR block/cell/occurrence provenance, 짧은 좌우 문맥이 포함된다. 보정 모델에는 candidate ID·anchor·문맥만 제공하며 offsets는 제공하지 않는다.
- 보정 모델이 ambiguous 후보 하나를 고르면 서버가 그 후보에서 `value_raw`와 `value_source`를 복원한다. 최종 Structured JSON에는 candidate ID를 저장하지 않는다.

이 resolver는 Common IR schema를 변경하거나 의미 Fact를 Common IR에 추가하지 않는다. 또한 PDF에도 같은 CandidatePack text 기준으로 동작하되, PDF Common IR에 실제로 없는 table/cell/relation을 생성하지 않는다.

## `relations`

현재 관계는 원문에서 확인 가능한 구조 관계만 담는다.

| `kind` | 의미 |
|---|---|
| `table_contains` | 부모 표의 `cell_id` 안에 자식 표 block이 존재함. HWP/HWPX에서는 원본 구조 기반, PDF에서는 bbox 포함 조건이 충족될 때만 생성 |
| `order`, `parent_child`, `diagram_edge`, `table_continuation` | 순서·계층·도식·표 연속 관계 |

`inferred: false`와 `structure_status: explicit`이면 직접 근거가 있는 관계다. PDF에 관계가 없다고 그 내용이 누락됐다는 뜻은 아니다. PDF는 독립 표/블록 텍스트만으로도 사실 구조화가 가능하다.

## PDF 운영 정책: native-only

운영용 PDF Common IR에서 텍스트를 갖는 occurrence는 `native_text`뿐이다. OCR 결과와 native–OCR 비교는 진단 artifact에만 보관하며, Common IR의 semantic text·CandidatePack·구조화 입력·검색 인덱스·`value_raw` 근거에는 넣지 않는다. OCR/image 산출물은 표·도식·이미지 영역의 위치·layout 진단 보조 artifact로만 남을 수 있고, 해당 diagnostic node에는 semantic text가 없다.

PDF `document`에는 다음 운영 metadata가 항상 있다.

| 필드 | 값 |
|---|---|
| `pdf_semantic_eligibility` | `eligible_native_text` 또는 `excluded_image_only` |
| `pdf_semantic_reason` | `substantive_native_text_available` 또는 `no_substantive_native_text` |
| `native_text_page_count` | `[Image: ...]` placeholder와 빈 문자열을 제외하고 native 텍스트가 남은 페이지 수 |
| `page_count` | 전체 PDF 페이지 수 |

`excluded_image_only` PDF는 OCR fallback 없이 구조화 JSON·검색 적재 대상에서 제외한다. `[Image: Im1]` 같은 파서 placeholder는 production Common IR의 block/occurrence/cell 의미 텍스트에서 제거한다.

PDF는 `table_candidate` 등 layout/structure 후보를 보존할 수 있지만 HWP/HWPX처럼 cell grid, row/column 대응, nested-table relation을 보장하지 않는다. PDF에 cell/relation이 없다는 이유로 실패 처리하거나 추정 relation을 만들지 않는다.

## Request 문서 적용

사전협의 Request HWP/HWPX도 위와 같은 `document → blocks → occurrences/cells → relations` 계약을 사용한다. Request 전용으로 보존하는 것은 원문에 있는 구조뿐이다.

- `□`, `■`, `☑`, `▣`, `✓`, `` 등 checkbox/선택 기호와 주변 text
- 표의 cell/row/column/span 및 명시적인 nested-table 관계
- page/bbox/원본 경로 같은 format별 provenance

아래는 **Common IR에 넣지 않는다**. 이후 Request Structured Profile의 책임이다.

`request_type`, `program_hierarchy`, `implementation_plan`, `business_need`, `legal_basis`, `linked_policy`, `expected_effect`, `performance_indicator`, `comparison_profile`, `support_components`, `delivery_relations`, `delivery_methods`, `field_states`, `derived_projections`.

따라서 ` 세부사업 신설`은 원문 glyph와 text로만 보존하며, Common IR이 canonical request type을 판정하지 않는다.

## 계약 고정 회귀 기준

2026-08-30 회귀 산출물은 아래에 보관한다. 모두 원본 hash 일치와 `validation_errors: 0`을 확인했다.

```text
/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/common_ir_v1_contract_regression_20260830

# 실제 Request HWP 검증 산출물
/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_common_ir_v1_20260830
```

| 유형 | 실제 샘플 | 확인 범위 |
|---|---|---|
| HWP | `PBLN_000000000125056` | source hash, 40 tables/414 cells, 10 explicit nested `table_contains` relations |
| HWPX | `PBLN_000000000125612` | source hash, 38 tables/434 cells |
| native PDF | `PBLN_000000000125056` | source hash, `eligible_native_text`, OCR occurrence/conflict 없음, image placeholder 제거 |
| Request HWP | `2023년 중소기업지원사업 사전협의제도 안내서` | source hash, blank/filled form checkbox glyph, table/cell 보존, Request 업무 의미 미생성 |

자동 회귀 스크립트:

```text
/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/scripts/test_request_hwp_common_ir_v1.py
```

## 구조화 담당자 규칙

1. `blocks[].text` 또는 명시 table의 `cells`에서만 원문값을 선택한다.
2. 구조화 fact마다 최소 `value_raw`, `source_block_ids`, 필요하면 `source_cell_ids`·`occurrence_ids`, 상태를 남긴다.
3. `explicit` 표 cell만 행/열 관계를 사실로 확정한다.
4. `partial` 표·도식은 native 텍스트가 있는 범위에서만 사용하고 관계·수치 의미를 과도하게 추론하지 않는다. OCR 텍스트·OCR 충돌은 운영 Common IR에 없다.
5. HWP/HWPX와 PDF 결과를 합치지 않는다.
6. `붙임`·`별첨`·`서식`은 무조건 버리지 않는다. 핵심 지원 조건·평가·제출 요건이 들어 있는지 먼저 판단한다.

## 스키마의 실제 기준

이 문서는 사람이 읽는 안내이며, 기계적 최종 계약은 아래 코드다.

```text
/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/scripts/common_ir_v1_schema.py
```
