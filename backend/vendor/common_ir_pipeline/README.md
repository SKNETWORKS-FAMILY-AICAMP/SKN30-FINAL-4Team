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
and its FreeType runtime. The PDF route consumes precomputed native extraction.
The optional OCR worker is a separately invoked diagnostic producer.

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
