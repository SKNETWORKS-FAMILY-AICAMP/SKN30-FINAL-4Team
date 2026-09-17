#!/usr/bin/env python3
"""Print the exact Model 1 service identity to pin in backend configuration."""

from __future__ import annotations

import json
from pathlib import Path

from prereview_model1_service.runtime import (
    EXPECTED_WEIGHT_SHA256,
    verified_runtime_manifest_sha256,
)


RUNTIME = Path("/workspace/project/prereview-model1/model1")
HELPER = Path("/workspace/project/prereview-model1/worker/ml/pipelines/model1/dl07_m1_apply.py")


def main() -> int:
    print(json.dumps({
        "weight_sha256": EXPECTED_WEIGHT_SHA256,
        "runtime_manifest_sha256": verified_runtime_manifest_sha256(RUNTIME, HELPER),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
