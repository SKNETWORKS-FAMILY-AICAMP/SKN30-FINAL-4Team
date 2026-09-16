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

## Offline Existing PDF replay

Existing 공고의 원본 PDF를 다시 Common IR로 만들 때만 고정된 PDF extra를
별도 환경에 설치한다. 이 extra는 Request 업로드 API나 기본 backend worker
이미지에 포함되지 않는다.

```bash
uv sync --locked --project backend/vendor/common_ir_pipeline --extra pdf

backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/replay_existing_pdf_native.py \
  --notice-id PBLN_000000000103645 \
  --pdf /abs/path/source.pdf \
  --output-dir /abs/path/new-replay
```

`pdf-inspector==1.17.0`은 이 패키지의 `uv.lock`에 고정된다. 명령은 전체
PDF만 읽으며 page-range를 받지 않는다. 출력 디렉터리는 새 경로여야 하고,
`source.pdf`, `native.json`, `common_ir.json`, `manifest.json`을 생성한다.
manifest에는 원본·native capture·Common IR의 상대 경로, 크기, SHA-256,
parser 버전, resource limit 및 native coverage가 기록된다. 이 경로는 DB,
Storage, OpenAI, OCR/ODL/Surya를 호출하지 않으며 Request PDF를 허용하지 않는다.
subprocess hard limit을 사용하는 Linux/WSL(POSIX) 환경에서 실행해야 한다.

### Canonical Existing PDF page render

Surya 같은 원격 visual worker에 보낼 페이지 이미지는 서버 CPU에서 한 번만
렌더한다. 이 경로는 Existing/shadow 검증 전용이며 Request 업로드 형식을
확장하지 않는다.

```bash
uv sync --locked --project backend/vendor/common_ir_pipeline --extra pdf-render

mkdir -p /abs/path/render-outputs
backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/render_existing_pdf_pages.py \
  --pdf /abs/path/source.pdf \
  --output-dir /abs/path/render-outputs/PBLN_000000000000000
```

호스트/전용 image에는 `util-linux`의 `/usr/bin/prlimit`이 반드시 있어야 한다.

`pdf-render` extra는 `pypdfium2==5.13.0`, `pypdf==6.18.1`,
`Pillow==12.3.0`을 고정한다. renderer는 전체 PDF를 200 DPI RGB PNG로
렌더하고 `rendered/page-XXXX.png`와 terminal `render_manifest.json`을
만든다. 원본·페이지 이미지 hash, 상속된 MediaBox/CropBox/Rotate/UserUnit,
PDF-user-space↔pixel affine, renderer/codec identity가 manifest에 결속된다.
공개 진입점은 위 wrapper뿐이다. wrapper는 renderer를 disposable subprocess로
격리하고 wall timeout·process-group kill·POSIX resource limit을 적용한 뒤, 부모
프로세스가 모든 hash와 좌표를 다시 검증한다. 성공 manifest가 없는 출력은
불완전한 실패 산출물이며 재사용하지 않는다.

이 subprocess 경계는 crash·hang·일반 descendant·자원 폭주를 제한하지만 악성 코드
실행을 막는 보안 sandbox는 아니다. `setsid()`로 process group을 이탈한 프로세스까지
격리해야 하는 운영 배포는 비특권 전용 container/PID namespace+cgroup에서 실행하고,
network·DB/Storage credential을 주지 않아야 한다.

이 명령은 OCR·Surya·OpenDataLoader·네트워크·DB를 호출하지 않는다. RunPod는
검증된 PNG를 입력으로만 받아야 하며 원본 PDF를 다시 열거나 렌더하면 안 된다.
GPU는 이 CPU 렌더 단계가 아니라 후속 Surya 추론에 사용한다.

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

`common-ir-pdf-native` also fails closed unless its native JSON conforms to
`pdf_inspector_native_capture/v1` and is bound to the supplied PDF bytes.
Unbound historical native JSON may be retained for audit, but cannot be used
to mint a new production Common IR document.

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
