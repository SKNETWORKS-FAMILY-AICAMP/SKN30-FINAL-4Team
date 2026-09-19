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
만든다. 원본·페이지 이미지 hash, 상속된 MediaBox/CropBox/Rotate와 leaf-page UserUnit,
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

## OpenDataLoader 좌표 캘리브레이션(오프라인 평가 전용)

OpenDataLoader의 `bounding box`를 곧바로 PDF 절대좌표로 간주하면 안 된다. 아래 도구는
Rotate 0/90/180/270, CropBox 오프셋, UserUnit 2 및 복합 조건의 합성 PDF를 만들고,
OpenDataLoader 2.5.7 OCR-off 결과를 두 번씩 실행해 좌표 convention과 결정성을 검증한다.
Common IR·selector·DB·Storage는 변경하지 않는다.

OpenDataLoader는 기본 패키지 의존성이 아니다. 평가용 격리 venv와 JRE 17을 별도로
준비한다. proof와 case별 run manifest에는 실제 JAR·package metadata·Java executable,
제공된 JRE package 파일, config, source PDF 및 raw output hash를 기록한다. JRE package
파일 hash는 executable의 package ownership을 주장하지 않는 재현 입력 기록이다.

```bash
UV_CACHE_DIR=/tmp/prereview-uv-cache \
  uv venv /tmp/prereview-odl-venv --python 3.12

UV_CACHE_DIR=/tmp/prereview-uv-cache \
  uv pip install \
    --python /tmp/prereview-odl-venv/bin/python \
    opendataloader-pdf==2.5.7

backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/run_opendataloader_coordinate_calibration.py generate \
  --output-root /tmp/prereview-odl-coordinate-fixtures

backend/vendor/common_ir_pipeline/.venv/bin/python \
  backend/scripts/run_opendataloader_coordinate_calibration.py execute \
  --fixture-root /tmp/prereview-odl-coordinate-fixtures \
  --parser-jar /tmp/prereview-odl-venv/lib/python3.12/site-packages/opendataloader_pdf/jar/opendataloader-pdf-cli.jar \
  --parser-package-metadata /tmp/prereview-odl-venv/lib/python3.12/site-packages/opendataloader_pdf-2.5.7.dist-info/METADATA \
  --java-executable /abs/path/to/jre17/bin/java \
  --java-package /abs/path/to/pinned-jre17-package \
  --output-root /tmp/prereview-odl-coordinate-proof
```

중간에 scoring 코드만 고친 경우 Java를 다시 실행하지 않고 `evaluate` subcommand로 서로
다른 두 run 디렉터리를 재평가할 수 있다. 이때 각 case의 `run_manifest.json`이 source,
정규화된 argv/config, JAR/JRE identity와 raw JSON hash를 모두 검증해야 한다. 현재 동결 proof는
`backend/baselines/pdf_reconstruction/opendataloader_coordinate_calibration_v1/`에 있다.
확인된 convention은 `rotated_crop_relative_bottom_left_raw_units`이다. 즉 bbox는 회전이
적용된 CropBox-relative bottom-left 좌표이고, 숫자에는 UserUnit이 곱해지지 않았다.

이 결과는 별도 `opendataloader_coordinate_calibration/v1` proof다. 기존
`opendataloader_artifact/v1`의 좌표 상태는 계속 `odl_pdf_points_unverified`이며, 검토된
후속 계약 없이 production alignment key로 사용할 수 없다.
현재 합성 anchor는 동일 길이 Courier paragraph이므로 이 proof의 범위는 paragraph bbox
convention과 extent다. table/cell/image 등 다른 node type은 후속 corpus gate 대상이다.

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

This command requires `uv` on `PATH` and verifies that the packaged PDF
fusion JSON Schemas are present in the built wheel.

### Native-first reconstruction shadow plan

`pdf_reconstruction_plan/v1` is an evaluation-only bridge between strict
native capture and textless OpenDataLoader structure candidates. It does not
replace Common IR or change the production ODL coordinate contract.

- every valid substantive native occurrence owns exactly one
  `accepted/evidence_atomic` unit;
- ODL candidates never own occurrences and cannot create composite evidence;
- a projection that merely claims a strict parser binding is rejected in v1.
  Strict support requires the underlying parser artifact so JAR/config/source
  lineage can be revalidated at this boundary;
- legacy, producer-unbound candidates are at most `partial/context_only`;
- zero-hit, cross-column/row merge, invalid geometry and unsupported
  candidates are `rejected/diagnostic_only`;
- an unresolved multi-occurrence leaf that is not a proven wide same-line
  column merge is rejected as `multi_occurrence_leaf_unverified` rather than
  mislabeled as a row/column conflict;
- semantic text is not copied into the plan. Occurrence IDs resolve back to
  the source-bound native capture.

`validate_reconstruction_plan` and canonical serialization check internal
consistency only. Persisted or untrusted plans must cross
`validate_reconstruction_plan_against_inputs`, which revalidates the source
and every supplied artifact, rebuilds the plan deterministically, and requires
canonical byte identity. A plan with no substantive native text records
`native_text_status=requires_ocr_semantic_v2` and cannot pass the native
ownership gate.

The public coordinate bridge requires the reviewed proof's canonical JSON
SHA-256 (`f8c041e...`), not the pretty-printed file-byte SHA-256
(`b7b7a7e...`). Both proof fields in the reconstruction plan use the canonical
digest. It remains
`calibrated_shadow_only`: a caller-provided proof does not promote an old ODL
artifact or authorize production evidence.

The frozen, non-reproducible review attestation for the 121019 page-5
observation is in
`backend/baselines/pdf_reconstruction/native_odl_alignment_121019_p5.fingerprint.v1.json`.
It preserves all 60 native occurrences, rejects four candidates that merge
separate columns/rows, and creates zero composite evidence units. The source
PDF and semantic text are deliberately not copied into the repository.
It is not a substitute for replaying the external source-bound artifacts.

The full seven-page 114788 held-out replay is recorded separately in
`backend/baselines/pdf_reconstruction/native_odl_alignment_114788_full.fingerprint.v1.json`.
Artifact-bound deterministic replay preserved all 218 substantive native
occurrences with one atomic owner each and no loss or duplicate ownership.
The 268 legacy ODL candidates remained non-promotable: 101 are
`partial/context_only` and 167 are `rejected/diagnostic_only`, with zero ODL
evidence units. This attestation proves alignment safety and native ownership
only. It has no bound semantic Gold or structural Gold, does not approve
paragraph/table reconstruction quality, and cannot replace replay of the
external source-bound inputs.

Do not feed these reconstructed `list_item` context proposals into the
current staged selector unchanged. Its existing complete-list-run expansion
can pull an entire long list back into one model call. A later versioned
context policy must cap context to the claim leaf, at most one immediate
parent, and at most one nearest heading, with no sibling expansion.
