# PDF 구조 생산 체인 — 원본 호환 스크립트

이 디렉터리는 이전 전달 ZIP에서 누락됐던 PDF 표·도식 구조 생산 체인의
**실제 원본 스크립트**를 함께 보관한다. `src/common_ir_pipeline/`의
`common_ir_v1` 패키지와 별개로, 기존 RunPod 실험에서 검증한 표 구조
gate/coverage/promotion 흐름을 재현·점검할 때 사용한다.

## 표 생산 체인

```text
render_document_pages.py
  → run_surya_all_pages_layout_blocks.py
  → run_surya_targeted_structure_pages.py
  → evaluate_pdf_only_table_structure_gate.py
  → check_pdf_only_explicit_cells.py
  → promote_pdf_only_explicit_tables.py
```

- `table_html_grid.py`: HTML의 rowspan/colspan을 logical cell grid로 복원하는 공통 helper
- structure gate: Surya HTML grid, TableRec grid, native PDF bbox coverage가 모두 맞는지 판정
- coverage: logical cell마다 native PDF occurrence가 실제로 있는지 판정
- promotion: `EXPLICIT` 통과 표만 table/cell로 승격

## 도식 생산 체인

```text
render_document_pages.py
  → run_surya_all_pages_layout_blocks.py 또는 run_surya_targeted_structure_pages.py
  → promote_pdf_only_explicit_diagram_edges.py
```

`diagram_direct_arrow.py`와 `diagram_native_node_match.py`는 literal arrow와
unique native endpoint만 허용한다. 중복·분할·미해결 node는 임의 선택하지
않는다.

## Common IR adapter 계보

아래 파일은 과거 PDF base/v0.1/v0.2/v0.3 호환 체인 전체를 보존한다.

```text
build_pdf_base_ir_from_surya_blocks.py
→ adapt_pdf_base_to_common_ir_v0_1.py
→ promote_pdf_common_ir_v0_2.py
→ build_common_ir_v0_3_all_in_one.py
```

이 체인은 과거 실험 artifact 경로와 v0.x schema를 전제한다. 따라서
`src/common_ir_pipeline/`의 현재 `common_ir_v1` production 경로를 대체하지
않는다. 누락 없이 재현·비교할 수 있도록 원본 그대로 동봉한 compatibility
bundle이다.

## 포함 범위와 제외 범위

포함: native PDF + Surya layout/HTML/TableRec 기반 표·도식 구조 생산,
gate, coverage, promotion, v0.x adapter/schema 의존성.

`config/common_ir_v0_1.schema.json`은 v0.1/v0.2 validator가 실제로 읽는
필수 schema 파일이다. 이 bundle 내부 경로를 우선 사용하므로, 과거
`exploratory_study/results/runpod_hybrid_ir_validation/config/` 경로가 없는
새 RunPod 환경에서도 표 gate·coverage·promotion 검증을 시작할 수 있다.

제외: Gold 채점, LLM prompt 실험, 특정 공고만을 위한 manual curation,
원본 PDF/HWP와 모델 가중치·OCR 결과물. 입력 artifact는 실행 환경에서
별도로 제공해야 한다.
