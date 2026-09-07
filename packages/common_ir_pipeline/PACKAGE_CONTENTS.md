# 전달 패키지 포함 범위

## 현재 Common IR v1 실행 경로

- HWP/HWPX: `rhwp` adapter, E2E runner, schema/validator
- PDF native: `pdf_inspector_capture.py` → `pdf_native.py`
- CPU OCR: PaddleOCR/EasyOCR geometry sidecar worker
- Surya 2: setup, endpoint lifecycle, layout scan, block scan, Diagram evidence scan
- PDF 도식: explicit Diagram edge promotion + final adapter wiring
- Markdown fixture: 테스트 전용 adapter와 fixture

## 기존 PDF 표·도식 구조 생산 체인

`compatibility/pdf_structure_pipeline/`에 다음 원본 호환 체인을 포함한다.

```text
render → all-page Surya blocks → targeted HTML/TableRec
→ table structure gate → logical-cell native coverage → explicit table promotion
```

포함 producer/gate/promotion:

- `render_document_pages.py`
- `run_surya_all_pages_layout_blocks.py`
- `run_surya_targeted_structure_pages.py`
- `table_html_grid.py`
- `evaluate_pdf_only_table_structure_gate.py`
- `check_pdf_only_explicit_cells.py`
- `promote_pdf_only_explicit_tables.py`
- `promote_pdf_only_explicit_diagram_edges.py`
- 관련 PDF base/v0.1/v0.2/v0.3 adapter/schema 및 diagram helper/test
- `config/common_ir_v0_1.schema.json` (v0.1/v0.2 validator의 실제 schema dependency)

## 의도적 제외

- 원본 PDF/HWP/HWPX, 모델 가중치, OCR/Surya 대용량 결과
- Gold 채점 자료와 LLM prompt 실험
- 특정 공고 ID에만 고정된 수동 보정 산출물

원본과 모델은 실행 환경에서 제공하며, 구조 생산 코드는 이 ZIP에 모두 포함한다.
