# Request Markdown fixture → Common IR v1 handoff

## Contract

- Schema/version: `common_ir_v1`
- Source format: `markdown_fixture` (initial test-fixture input capability; not a Request-specific Common IR dialect or permanent representative source path)
- Parser: `markdown_fixture_parser` `1.1.0`
- Generator: `semantic_structuring.request_markdown_common_ir_v1` `1.1.0`
- No LLM, OCR, PDF processing, Gold/score input, or Request business-semantic extraction was used.

## Input/output mapping

| document ID | Markdown source | Common IR output | reference notice ID (sidecar only) |
|---|---|---|---|
| `request:PREREVIEW-TEST-2027-01` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input/request_01_scale_period.input.md` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir/PREREVIEW-TEST-2027-01.common_ir_v1.json` | `bizinfo:PBLN_000000000125016` |
| `request:PREREVIEW-TEST-2027-02` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input/request_02_target_eligibility.input.md` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir/PREREVIEW-TEST-2027-02.common_ir_v1.json` | `bizinfo:PBLN_000000000125056` |
| `request:PREREVIEW-TEST-2027-03` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input/request_03_support_content.input.md` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir/PREREVIEW-TEST-2027-03.common_ir_v1.json` | `bizinfo:PBLN_000000000125612` |
| `request:PREREVIEW-TEST-2027-04` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input/request_04_new_program.input.md` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir/PREREVIEW-TEST-2027-04.common_ir_v1.json` | `None` |
| `request:PREREVIEW-TEST-2027-05` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input/request_05_component_change.input.md` | `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/common_ir/PREREVIEW-TEST-2027-05.common_ir_v1.json` | `bizinfo:PBLN_000000000125056` |

`reference_notice_id` is stored only in `request_common_ir_manifest.json`; it is not inserted into Common IR blocks, occurrences, relations, or document text.

## Markdown mapping

- ATX headings become `heading` blocks; heading level/order is preserved in `source_block_label` and `section_path`.
- Ordinary paragraphs and list items become `paragraph` blocks. List marker syntax is structural; each list item's text occurrence points to its exact source substring.
- Markdown tables become `table` blocks with explicit `cells`. Header and data rows preserve row/column indexes, spans, cell occurrence IDs, and exact source-text spans. The Markdown divider row is syntax only and is not a semantic cell.
- All occurrence provenance uses Python Unicode code-point offsets, start-inclusive/end-exclusive, against the cleaned `.input.md` source copies.


## Cleaned semantic-input lineage

- Purpose: `remove inline Markdown presentation markers from semantic text`
- Cleaning version: `markdown_inline_presentation_clean_v1`
- Original fixture input root (read only): `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/docs/PreReview_Request_Profile/test_request_corpus_v0.1/semantic_input`
- Cleaned source copies: `/home/hyseo/SKN_30_Works/Projects/SKN30_T4_final_work/hwp_parsing/exploratory_study/results/request_profile_test_corpus_common_ir_v1_presentation_clean_20260831/cleaned_semantic_input`
- Rules: inline `**` / `__` presentation delimiters are removed; heading, list,
  and table syntax is retained so explicit structure remains available.
- Gold and the original corpus were not changed. Common IR occurrence offsets
  refer to each cleaned source copy, whose path and SHA-256 are in the manifest.


## Table summaries

- `request:PREREVIEW-TEST-2027-01`: Markdown 표 없음
- `request:PREREVIEW-TEST-2027-02`: Markdown 표 없음
- `request:PREREVIEW-TEST-2027-03`: Markdown 표 없음
- `request:PREREVIEW-TEST-2027-04`: Markdown 표 없음
- `request:PREREVIEW-TEST-2027-05`: Markdown 표 없음

## Validation

Every output passed Common IR v1 schema/cross-reference validation, source SHA-256 verification, occurrence source-span round-trip verification, and lineage join through `document_id` plus source hash. Only the declared fixture semantic input and its request manifest were read.

## Remaining limitation

This adapter preserves Markdown's explicit structure only. It does not infer merged cells, checkbox meaning, business fields, Request type canonical codes, comparison facts, or Gold/assessment results.
