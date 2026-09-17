#!/usr/bin/env bash
# One local, PDF-free GPU smoke for the real Surya adapter.
# This does not exercise signed storage, RunPod delivery, or EC2 result acceptance.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

resolve_worker_layout
ensure_deployment_tree
read_config_into_environment
"$SCRIPT_DIR/status_surya_vllm.sh" >/dev/null

[[ -x "$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" ]] || die "client venv is not ready; run setup_runpod.sh"

"$PREREVIEW_RUNPOD_CLIENT_VENV/bin/python" - <<'PY'
from io import BytesIO

from PIL import Image

from prereview_runpod_worker.surya_layout_worker.settings import load_worker_settings
from prereview_runpod_worker.surya_layout_worker.surya_inference import (
    SuryaLayoutInferenceAdapter,
    load_deployment_attestation,
)

settings = load_worker_settings()
image = Image.new("RGB", (32, 32), "white")
buffer = BytesIO()
image.save(buffer, format="PNG")
adapter = SuryaLayoutInferenceAdapter(
    producer=settings.producer,
    endpoint=settings.endpoint,
    endpoint_policy=settings.endpoint_policy,
    attestation=load_deployment_attestation(),
    max_png_bytes=min(
        settings.http.hard_max_bytes,
        settings.dispatch_policy.max_capability_bytes,
        settings.dispatch_policy.max_total_input_bytes,
    ),
    max_image_pixels=settings.dispatch_policy.max_total_rendered_pixels,
    max_regions_per_page=settings.output.max_regions_per_page,
)
result = adapter.layout(
    page_number=1,
    png_bytes=buffer.getvalue(),
    pixel_width=32,
    pixel_height=32,
)
if result.error not in (False, None):
    raise SystemExit("Surya adapter returned a page error")
if tuple(result.image_bbox) != (0, 0, 32, 32):
    raise SystemExit("Surya adapter returned an unexpected image canvas")
print(f"Surya adapter smoke passed: {len(result.regions)} textless regions")
PY
