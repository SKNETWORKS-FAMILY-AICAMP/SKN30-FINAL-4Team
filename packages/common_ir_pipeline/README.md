# Common IR Pipeline

Portable Common IR v1 generation package. It turns one source document into
one Common IR document; it does not extract business facts, compare documents,
or call an LLM. An optional OCR/layout diagnostic worker is included for PDF
visual-structure observation, but it never contributes OCR strings to Common
IR semantic text.

Read [HANDOVER.md](HANDOVER.md) before integrating it.

## Quick start

```bash
uv venv
uv pip install -e .
python -m unittest discover -s tests -v
```

The HWP/HWPX route additionally requires a working `rhwp-python` installation
and its FreeType runtime. The PDF route can create its own immutable native
`pdf-inspector` capture through the optional `pdf` extra. OCR/Surya workers
are separately invoked diagnostic producers.

## PDF production path and included scope

```text
PDF → pdf-inspector native capture → Common IR native adapter
    ├─ CPU OCR geometry sidecar (optional) → blank layout_candidate blocks
    └─ Surya layout / Diagram HTML (optional)
          → literal-arrow + unique native-endpoint verification
          → explicit diagram_edge relations only
```

The package includes each runtime stage of this path: native capture, PDF
render manifest creation, CPU OCR geometry, Surya layout, Surya Diagram HTML
evidence, explicit-edge promotion and final Common IR wiring. It intentionally
does **not** include LLM-generated diagram edges, OCR-text semantic evidence,
or OCR-derived table-cell promotion because they violate Common IR v1's
native-text and non-inference policy.

```bash
uv pip install -e '.[pdf]'
common-ir-pdf-inspector-capture --notice-id PBLN_... --pdf notice.pdf --output native.json
common-ir-pdf-render --pdf notice.pdf --output-dir rendered --scale 2
SURYA_INFERENCE_URL=http://127.0.0.1:8000 \
  common-ir-pdf-surya-diagram-evidence --pdf notice.pdf --output diagram-evidence.json --pages 2 --scale 2
common-ir-pdf-promote-explicit-diagram-edges \
  --source-pdf notice.pdf --native native.json \
  --render-manifest rendered/render_manifest.json \
  --surya-diagram-evidence diagram-evidence.json --output diagram-relations.json
common-ir-pdf-native --notice-id PBLN_... --native native.json --source-path notice.pdf \
  --diagram-relations-pdf-base diagram-relations.json --output notice.common_ir.json
```

## Optional PDF OCR/layout diagnostic worker

Install this only in an environment that is meant to render PDFs and run OCR
models. The base package and the handover ZIP do not include model weights.
The `ocr` extra includes the CPU `paddlepaddle` runtime required by PaddleOCR;
for a GPU deployment, install the platform/CUDA-compatible PaddlePaddle build
first and then install the remaining OCR dependencies without replacing it.

```bash
uv pip install -e '.[ocr]'
common-ir-pdf-ocr-layout \
  --pdf /abs/path/notice.pdf \
  --output /abs/path/notice.ocr-layout-diagnostic.json \
  --engine both \
  --pages 2,3 \
  --scale 1.5
```

If the target platform has no compatible `paddlepaddle` wheel, install its
supported CPU/GPU PaddlePaddle distribution using the official PaddlePaddle
installation instructions, then install `paddleocr`, `easyocr`, `PyMuPDF`, and
`numpy`. The worker checks for PaddlePaddle explicitly and fails before OCR if
it is missing.

`--engine` is `paddle`, `easy`, or `both` (default). The worker runs CPU
PaddleOCR/EasyOCR, records rendered-page geometry, per-engine timing,
confidence, and text-region bboxes, then **discards every recognized string**
before writing the sidecar. It does not classify table cells, reconstruct
row/column relations, infer diagram edges, or decide business facts.

The resulting `common_ir_v1_pdf_ocr_layout_diagnostic_v1` sidecar is not a
Common IR document and is never passed to CandidatePack/semantic extraction.
It can be attached to the native PDF adapter only as textless layout
provenance:

```bash
common-ir-pdf-native ... \
  --ocr-layout-diagnostic /abs/path/notice.ocr-layout-diagnostic.json
```

The adapter verifies the source SHA-256 and projects each region as a blank
`layout_candidate` containing only a `layout_region` occurrence/bbox. It
creates no cells or relations. CandidatePack consumers must exclude blank
`layout_candidate` blocks. Native PDF text remains the only semantic text
source; scan/image-only PDFs remain excluded even if this worker detects many
regions.

## Optional Surya 2 GPU layout / block worker

The package includes the RunPod-style Surya 2 route. It is an **explicit
endpoint client**, never an autostarting service: scan commands require
`SURYA_INFERENCE_URL` and force `SURYA_INFERENCE_AUTOSTART=false`.

```bash
# Creates a package-local Surya client environment. Does not start a server.
./scripts/setup_surya_gpu.sh

# Optional local vLLM endpoint lifecycle. Neither command is ever called by a scan.
./scripts/setup_surya_vllm_server.sh
./scripts/start_surya_vllm.sh
SURYA_INFERENCE_URL=http://127.0.0.1:8000 ./scripts/status_surya_endpoint.sh
# Stop only the package-tracked local vLLM server.
./scripts/stop_surya_vllm.sh

# Optional and explicit: predownload the endpoint model. A scan never calls it.
./scripts/download_surya_model.sh

# Checks an already-running endpoint; never starts/stops it.
SURYA_INFERENCE_URL=http://127.0.0.1:8000 ./scripts/status_surya_endpoint.sh

# All selected pages: LayoutPredictor -> labels/bboxes/timing only.
SURYA_INFERENCE_URL=http://127.0.0.1:8000 \
  ./scripts/run_surya_layout.sh --pdf /abs/path/notice.pdf \
  --output /abs/path/notice.surya-layout.json --pages 2,3

# Selected page/block crop scan. `2:3` means page 2, detected block 3.
# Any returned OCR text/HTML is discarded before output is written.
SURYA_INFERENCE_URL=http://127.0.0.1:8000 \
  ./scripts/run_surya_block_scan.sh --pdf /abs/path/notice.pdf \
  --output /abs/path/notice.surya-block.json --pages 2 --target 2:3
```

`common-ir-pdf-surya-layout` supports `--mode layout` and `--mode block`.
Both create a SHA-bound `common_ir_v1_pdf_surya_layout_diagnostic_v1`
sidecar. The native PDF adapter accepts it through
`--ocr-layout-diagnostic`, but only projects empty `layout_candidate` blocks
with layout provenance. Surya output can therefore never enter CandidatePack,
exact-span resolution or `value_raw`; the layout worker does not create table
cells, rows, columns or diagram edges. The separate Diagram-evidence route
retains raw Diagram HTML only in an external sidecar and promotes an edge only
when a literal arrow and two uniquely matched native endpoints are present.
