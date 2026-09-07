# Common IR Pipeline v1 handover

## Purpose and boundary

This package is the portable **document-to-Common-IR** layer. A single source
artifact produces one `common_ir_v1` document. It preserves source text,
headings, paragraphs, tables, cells, occurrences, explicit structural
relations and provenance. It deliberately does **not** contain semantic
profile extraction, Request/Existing comparison, LLM prompting, scoring,
normalization, or business-policy logic. Optional OCR/Surya workers are
included only to produce SHA-bound visual-structure diagnostics.

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
  adapters/pdf_native.py    native PDF + SHA-bound explicit diagram-relation sidecar -> Common IR
  adapters/markdown_fixture.py
                            test-fixture Markdown -> Common IR
  run_rhwp_e2e.py           original HWP/HWPX -> rhwp -> Common IR runner
  workers/pdf_ocr_layout.py CPU OCR geometry-only diagnostic worker
  workers/pdf_inspector_capture.py
                            PDF -> immutable native pdf-inspector capture
  workers/pdf_render.py     PDF -> rendered-page coordinate manifest
  workers/pdf_surya_layout.py
                            Surya 2 GPU layout / optional block-scan diagnostic worker
  workers/pdf_surya_diagram_evidence.py
                            Surya Diagram HTML evidence sidecar (never semantic text)
  workers/promote_pdf_only_explicit_diagram_edges.py
                            literal arrows + unique native endpoints -> explicit relations
tests/                      small portable regression tests
fixtures/                   tiny Markdown example and manifest shape
docs/COMMON_IR_V1_CONTRACT.md
                            consumer contract copied from the handoff version
compatibility/pdf_structure_pipeline/
                            original PDF table/diagram gate, coverage and promotion chain
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

## Optional Surya 2 GPU worker and lifecycle boundary

The package includes the Surya 2 GPU setup plus all-page and targeted block
scan route used by the RunPod experiments. It is a diagnostic/layout path,
not a semantic OCR path.

| Script | Purpose |
|---|---|
| `scripts/setup_surya_gpu.sh` | Creates a package-local Surya client environment. The deployer selects the CUDA/Torch build. |
| `scripts/setup_surya_vllm_server.sh` | Creates a separate optional vLLM endpoint-server environment. |
| `scripts/surya_runtime_env.sh` | Sourceable, package-local Hugging Face / Datalab cache locations. |
| `scripts/download_surya_model.sh` | Explicit opt-in predownload of `datalab-to/surya-ocr-2`; scans never invoke it. |
| `scripts/start_surya_vllm.sh` / `scripts/stop_surya_vllm.sh` | Explicit local endpoint lifecycle. Stop validates the tracked PID's vLLM command before it signals it. |
| `scripts/status_surya_endpoint.sh` | Checks a caller-managed OpenAI-compatible `/v1` endpoint. |
| `scripts/run_surya_layout.sh` | Surya `LayoutPredictor` for selected pages; emits labels/bboxes/timing. |
| `scripts/run_surya_block_scan.sh` | Block scan for all or selected `PAGE:BLOCK_INDEX` crops; discards returned text/HTML. |
| `scripts/run_surya_diagram_evidence.sh` | High-accuracy Surya Diagram HTML evidence for the explicit-edge verifier only. |
| `scripts/run_pdf_explicit_diagram_promotion.sh` | Literal-arrow + unique-native-endpoint relation promotion. |

Both scan wrappers require `SURYA_INFERENCE_URL` and force
`SURYA_INFERENCE_AUTOSTART=false`; they never start Docker, vLLM or a local
model server. Local lifecycle is available only through the explicit
`start_surya_vllm.sh` / `stop_surya_vllm.sh` commands. The worker produces a SHA-bound
`common_ir_v1_pdf_surya_layout_diagnostic_v1` sidecar containing geometry,
labels, timing and completion status only. The PDF adapter can project it as
blank `layout_candidate`/`layout_region` provenance, without cells,
row/column relations or business facts. Native PDF text stays
the sole semantic source and image-only PDFs remain excluded.

### Explicit PDF Diagram relation route

The package also includes the separate, conservative relation route that was
missing from the prior handover. It is the only visual path allowed to create
`diagram_edge` relations in production Common IR v1:

```text
PDF render manifest
  + Surya high-accuracy Diagram HTML evidence (external diagnostic sidecar)
  + pdf-inspector native text occurrences
  → literal-arrow parser
  → require exactly one native occurrence for every endpoint
  → explicit, non-inferred diagram_edge relation
```

The needed commands are `common-ir-pdf-render`,
`common-ir-pdf-surya-diagram-evidence`, and
`common-ir-pdf-promote-explicit-diagram-edges`. The final
`common-ir-pdf-native --diagram-relations-pdf-base ...` call validates that
the relation sidecar belongs to the same original PDF via SHA-256. It rejects
inferred edges, ambiguous endpoints, LLM candidates and unbound sidecars.
Diagram HTML remains outside Common IR text and CandidatePack; only its
provenance plus the native-occurrence-backed relation is carried forward.

## Included PDF table gate / coverage / promotion bundle

The initial ZIP accidentally omitted the established PDF table production
chain. It is now included intact under
`compatibility/pdf_structure_pipeline/`, including the all-page and targeted
Surya producers, logical HTML grid resolver, structure gate, native coverage
check, explicit-table promotion, diagram promotion and the required v0.x
adapter/schema scripts. See that directory's `README.md` for the dependency
order and its compatibility status.

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

```bash
# Create the native artifact when a precomputed pdf-inspector JSON is absent.
uv pip install -e '.[pdf]'
common-ir-pdf-inspector-capture \
  --notice-id PBLN_000000000125056 \
  --pdf /abs/path/notice.pdf \
  --output /abs/path/native_pdf_inspector.json

common-ir-pdf-native \
  --notice-id PBLN_000000000125056 \
  --native /abs/path/native_pdf_inspector.json \
  --source-path /abs/path/notice.pdf \
  --source-sha256 "$(sha256sum /abs/path/notice.pdf | cut -d' ' -f1)" \
  --output /abs/path/notice.pdf.common_ir_v1.json
```

PDF semantic text is **native text only**. Archived OCR/Surya/Paddle outputs
may optionally be supplied through `--render-manifest` and
`--enriched-common-ir` only when they were already produced and
source-hash-bound;
they can preserve textless layout/table/diagram provenance but their OCR text
never becomes Common IR semantic text, a CandidatePack text basis, an exact
span or `value_raw`. The adapter never invokes OCR itself. Image-only PDFs
become `excluded_image_only` with no semantic blocks.

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
- CPU OCR and Surya layout sidecars are optional. When supplied, they must be
  textless and bound to the same source PDF. The package includes optional
  workers to create them, but neither worker is run by the PDF adapter and no
  scan/model download/server start happens implicitly. Without sidecars the
  adapter emits valid native-text-only Common IR.
- PDF cell/row/diagram relations are emitted only when existing native/layout
  evidence explicitly supports them; no HWP-like structural inference occurs.
- The schema has no global document registry. Cross-document joins require
  document ID plus source hash/version lineage.
