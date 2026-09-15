# Common IR Pipeline v1 handover

## Purpose and boundary

This package is the portable **document-to-Common-IR** layer. A single source
artifact produces one `common_ir_v1` document. It preserves source text,
headings, paragraphs, tables, cells, occurrences, explicit structural
relations and provenance. It deliberately does **not** contain semantic
profile extraction, Request/Existing comparison, LLM prompting, scoring,
normalization, OCR execution, or business-policy logic.

```text
HWP/HWPX ─┐
PDF       ├─> Common IR v1 ──> CandidatePack / Request or Existing Profile
Markdown  ┘
```

## Package layout

```text
src/common_ir_pipeline/
  schema.py                 Common IR v1 JSON schema + cross-reference validator
  shared.py                 lineage, hash and provenance helpers
  adapters/rhwp.py          raw rhwp IR -> Common IR for HWP/HWPX
  adapters/pdf_native.py    native PDF, optionally enriched by archived layout sidecars -> Common IR
  adapters/markdown_fixture.py
                            test-fixture Markdown -> Common IR
  pdf_fusion/native_capture.py
                            pinned pdf-inspector capture validation and source binding
  workers/pdf_inspector_capture.py
                            whole-document native capture child process
  pdf_fusion/              coordinate/render manifests and immutable byte binding
  pdf_fusion/schemas/      cross-language JSON Schemas for those manifests
  run_rhwp_e2e.py           original HWP/HWPX -> rhwp -> Common IR runner
tests/                      small portable regression tests
fixtures/                   tiny Markdown example and manifest shape
docs/COMMON_IR_V1_CONTRACT.md
                            consumer contract copied from the handoff version
```

## Installation and test

Requires Python 3.11+ and `jsonschema`.

```bash
uv venv
uv pip install -e .
python -m unittest discover -s tests -v
```

The HWP/HWPX route additionally needs a working `rhwp-python` installation.
The local runtime may require its FreeType compatibility preload; configure
that in the deployment environment before running `common-ir-rhwp`.

## Optional OCR/layout worker

The package includes `common-ir-pdf-ocr-layout`, an optional CPU
PaddleOCR/EasyOCR worker. Install it only where OCR is wanted:

```bash
uv pip install -e '.[ocr]'
common-ir-pdf-ocr-layout --pdf /abs/path/notice.pdf \
  --output /abs/path/notice.ocr-layout-diagnostic.json --engine both
```

The `ocr` extra includes the CPU `paddlepaddle` runtime that PaddleOCR needs.
For GPU deployments, install the platform/CUDA-compatible PaddlePaddle build
first, then install the remaining OCR libraries without replacing that runtime.
If the target platform lacks a compatible wheel, use PaddlePaddle's official
platform-specific installation procedure; the worker explicitly reports a
missing PaddlePaddle runtime before it attempts PaddleOCR.

It renders selected PDF pages and persists only page geometry, text-region
bboxes, confidence, engine/timing metadata, and source hash. Recognized OCR
strings are discarded before output. The sidecar is layout/diagnostic evidence
only: it is neither Common IR semantic text nor CandidatePack/exact-span input,
and it cannot make an image-only PDF eligible. It does not synthesize table
cells, row/column relations, diagram edges, or business facts. No OCR model
weights are included in this repository or the ZIP.

Pass the sidecar to `common-ir-pdf-native --ocr-layout-diagnostic ...` only to
retain its SHA-bound geometry as blank `layout_candidate` blocks with
`layout_region` occurrences. CandidatePack/semantic consumers must exclude
these blank blocks; they are for an explicit later layout gate or visual review.

The `common_ir_pipeline.pdf_fusion` module does not execute OCR or render a PDF. It
validates the source PDF and already-rendered PNG bytes, page count, page
rotation/UserUnit/scale, affine coordinate transforms and sidecar hashes. Its
initial renderer contract accepts only non-interlaced 8-bit RGB/RGBA PNGs and
boundedly validates concatenated IDAT/DEFLATE output and scanline filter bytes.
An
actual renderer integration must derive page metadata from its pinned PDF
engine; arbitrary caller-supplied page metadata is not an authority.

Its JSON Schemas are structural only. The Python semantic validator is the
authority for byte/hash/path/geometry invariants. The render schema uses the
coordinate schema's absolute `$id` in its `$ref`; register that resource under
that `$id` when validating externally. `/Rotate` is clockwise and must be
normalized modulo 360 before manifest construction. Validation is for trusted
single-owner artifact roots only: a production consumer uploads the same
verified FD/bytes or an immutable content-addressed object. Reopening paths in
multi-tenant storage is NO-GO pending a dirfd/openat2 boundary.

## Supported inputs

### HWP / HWPX

```bash
common-ir-rhwp \
  --notice-id PBLN_000000000125056 \
  --input /abs/path/notice.hwp \
  --source-kind hwp \
  --run-dir /abs/path/runs/PBLN_000000000125056_hwp
```

The runner saves a run-local `raw/rhwp_full_ir/`, `common_ir_v1/` and
`run_manifest.json`. It does not require a paired PDF and does not invoke
OCR, GPU work, network calls or an LLM.

For an already-created rhwp raw IR, invoke the adapter directly:

```bash
python -m common_ir_pipeline.adapters.rhwp \
  --notice-id PBLN_000000000125056 \
  --rhwp /abs/path/raw.rhwp.json \
  --source-kind hwp \
  --source-path /abs/path/notice.hwp \
  --source-sha256 "$(sha256sum /abs/path/notice.hwp | cut -d' ' -f1)" \
  --output /abs/path/notice.hwp.common_ir_v1.json
```

### Native PDF

새 Existing 원본에서 native artifact와 Common IR을 함께 만들 때는 저장소 루트의
오프라인 replay 명령을 사용한다. 이 경로만 `pdf-inspector==1.17.0` extra를
사용하며 Request worker에는 연결하지 않는다.

```bash
uv sync --locked --project backend/vendor/common_ir_pipeline --extra pdf
backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/replay_existing_pdf_native.py \
  --notice-id PBLN_000000000125056 \
  --pdf /abs/path/notice.pdf \
  --output-dir /abs/path/new-replay
```

아래 adapter 직접 호출은 `pdf_inspector_native_capture/v1`로 생성되어 원본
SHA-256·크기와 결속된 native artifact를 재조립할 때만 사용한다. schema marker가
없는 historical/unbound JSON은 production Common IR 생성에 사용할 수 없다.
replay 명령은 subprocess hard limit을 적용하므로 Linux/WSL(POSIX)에서 실행한다.

```bash
common-ir-pdf-native \
  --notice-id PBLN_000000000125056 \
  --native /abs/path/native_pdf_inspector.json \
  --source-path /abs/path/notice.pdf \
  --source-sha256 "$(sha256sum /abs/path/notice.pdf | cut -d' ' -f1)" \
  --output /abs/path/notice.pdf.common_ir_v1.json
```

PDF semantic text is **native text only**. The direct adapter still exposes
legacy `--render-manifest`, `--enriched-common-ir` and diagram-sidecar options,
but the new replay command deliberately does not. In particular, the current
adapter does not independently bind every enriched/render/diagram sidecar to
the source PDF, so those inputs must not be used for a new production replay
until the later fusion manifest gate is implemented. The separately supported
OCR-layout diagnostic verifies its source hash and remains textless. No OCR
string becomes Common IR semantic text, a CandidatePack text basis, an exact
span or `value_raw`. Image-only PDFs become `excluded_image_only` with no
semantic blocks.

### Markdown fixture (test-only adapter)

```bash
common-ir-markdown-fixture \
  --input-dir /abs/path/semantic_input \
  --output-root /abs/path/request_fixture_common_ir
```

Only `*.input.md` named by the input `request_manifest.json` are read.
`reference_notice_id` stays in the generated sidecar manifest and is never
inserted into Common IR document text. This adapter is not a permanent
production Request input format and does not add a Request-specific IR
dialect.

## Fixed contract

- Schema version: `common_ir_v1`
- One source file is one independent Common IR document; same business ID in
  HWP/HWPX/PDF does not authorize merging them.
- `document.provenance` carries `source_sha256`, source location, schema,
  generator and upstream parser versions. `artifact_role` is `production`.
- `block_id`, `cell_id`, `occurrence_id` and `relation_id` are document-local
  identifiers. Their source format prefix/node type distinguishes roles; they
  are not a global namespace.
- HWP/HWPX preserves explicit table cells and nested-table containment where
  rhwp exposes it. PDF never synthesizes missing table cells or relations.
- Common IR contains source structure, not canonical business facts.

The complete consumer-facing contract is in
[`docs/COMMON_IR_V1_CONTRACT.md`](docs/COMMON_IR_V1_CONTRACT.md).

## Exact-span / downstream provenance

Structured Profile exact spans are based on the **CandidatePack canonical
text**, not a flattened Common IR table string:

```text
value_raw == candidate_pack_block.text[start_char:end_char]
```

Offsets are Python Unicode code-point offsets, start-inclusive and
end-exclusive. A table value must use a cell-paragraph CandidatePack block.
Downstream artifacts must retain the CandidatePack lineage binding:

```text
candidate_pack_id
candidate_pack_generator
candidate_pack_generator_version
common_ir_document_id
common_ir_source_sha256
```

CandidatePack construction and semantic selection are intentionally outside
this package.

## Migration source map

This package was extracted without changing the existing experimental work.

| Portable module | Original source |
|---|---|
| `schema.py`, `shared.py`, `adapters/rhwp.py`, `adapters/pdf_native.py`, `run_rhwp_e2e.py` | `exploratory_study/input/common_ir_transfer_20260827/transfer_common_ir_20260827/study/results/scripts/` |
| `adapters/markdown_fixture.py` | `semantic_structuring/run_request_markdown_to_common_ir_v1.py` |
| `docs/COMMON_IR_V1_CONTRACT.md` | `docs/handoff/common_ir_v1_for_semantic_structuring.md` |

Package-specific changes are package-relative imports, portable CLI defaults
(explicit input/output required), a generic rhwp runtime check, and two
portability hardenings: the Markdown adapter accepts any non-empty validated
fixture manifest rather than exactly five documents, and the PDF adapter can
produce native-only Common IR without archived layout sidecars. Markdown
manifest entries are validated before any source read and reject traversal,
nested paths, duplicates, and non-`.input.md` inputs. No experimental source
file was moved or modified by this extraction.

## Known limitations

- No real Request HWPX sample was available when this package was prepared;
  the HWP/HWPX adapter is shared and Markdown is test-fixture-only.
- PDF layout sidecars are optional. When supplied, they must already exist
  and be bound to the same source PDF. This package does not run OCR or
  recreate missing sidecars; without them it emits valid native-text-only
  Common IR.
- PDF cell/row/diagram relations are emitted only when existing native/layout
  evidence explicitly supports them; no HWP-like structural inference occurs.
- The schema has no global document registry. Cross-document joins require
  document ID plus source hash/version lineage.
