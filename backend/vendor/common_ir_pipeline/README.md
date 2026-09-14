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

`common-ir-pdf-ocr-layout` is a legacy EC2/local-renderer diagnostic CLI. It
is forbidden as a RunPod fusion worker; it remains present for local evidence
collection only.

## PDF fusion artifact boundary

`common_ir_pipeline.pdf_fusion` contains dependency-free, fail-closed contracts
used before any later ODL/Surya alignment:

- `pdf_coordinate_manifest/v1`: page box, rotation, UserUnit, render scale,
  affine/inverse, source hash and page-image hash
- `pdf_render_manifest/v1`: one complete PDF, all contiguous rendered PNGs,
  their byte hashes/sizes/dimensions and coordinate manifests

The package ships Draft 2020-12 JSON Schemas under `pdf_fusion/schemas/`.
They are structural/interoperability checks only; the Python semantic
validators are authoritative for hashes, page continuity, transform binding,
and filesystem bytes. Consumers resolving the render schema's coordinate
`$ref` must register the coordinate schema by its absolute `$id`
(as the package tests do), rather than treating a schema filename as a global
registry key.
`assemble_render_manifest` hashes actual local PDF/PNG files and
`validate_render_manifest_files` repeats byte, PNG-dimension, and bounded
IDAT/scanline verification
before a consumer may send images to a remote worker. These primitives do not
render a PDF and do not promote OCR/ODL text into Common IR.

The initial renderer contract intentionally accepts only non-interlaced 8-bit
RGB/RGBA PNGs. PNG inputs have conservative compressed-byte, dimension, total
pixel, and expected-decompressed-scanline caps; the validator boundedly checks
the concatenated IDAT zlib stream and each PNG filter byte without retaining
decoded pixels. These values must be recalibrated from trusted renderer-corpus
p99 measurements before production rollout. Validation assumes a trusted,
single-owner artifact root. It does not make later pathname reopening safe:
production must upload the exact verified FD/bytes or an immutable
content-addressed object. Multi-tenant reopening is NO-GO until a dirfd/openat2
boundary is implemented.

PDF `/Rotate` is clockwise; producers must normalize it modulo 360 to
`0/90/180/270` before constructing a coordinate manifest. `/UserUnit` is part
of canonical point dimensions and render-scale calculation.

Release packaging must also exercise the real wheel-content gate; the normal
source-tree test run intentionally skips this build-only check:

```bash
COMMON_IR_VERIFY_WHEEL=1 UV_CACHE_DIR=/tmp/common-ir-uv-cache \
  python -m unittest \
  tests.test_portable_package.PortablePackageTests.test_wheel_contains_pdf_fusion_json_schemas -v
```

This command requires `uv` on `PATH` and verifies that both PDF fusion JSON
Schemas are present in the built wheel.
