#!/usr/bin/env python3
"""Authenticated, identity-pinned healthcheck for Model 2/3."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contract import MODEL23_MAX_CONCURRENCY
from .runtime import EXPECTED_MODEL2_BUNDLE_SHA256, EXPECTED_MODEL3_POOL_SHA256
from .settings import Model23ServiceConfigurationError, load_settings


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RESPONSE_BYTES = 16_384


def main() -> int:
    try:
        expected_manifest = os.environ[
            "PREREVIEW_MODEL23_REMOTE_RUNTIME_MANIFEST_SHA256"
        ]
        if _SHA256.fullmatch(expected_manifest) is None:
            raise ValueError
        settings = load_settings()
        request = Request(
            "http://127.0.0.1:8792/v1/model23/ready",
            headers={
                "Authorization": f"Bearer {settings.bearer_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlopen(request, timeout=3) as response:  # noqa: S310 - fixed loopback URL
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise ValueError
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError
        value = json.loads(body)
        expected = {
            "ready": True,
            "max_concurrency": MODEL23_MAX_CONCURRENCY,
            "max_request_bytes": settings.max_request_bytes,
            "device": "cpu",
            "producer": {
                "service": "prereview-model23",
                "runtime_manifest_sha256": expected_manifest,
                "model2_bundle_sha256": EXPECTED_MODEL2_BUNDLE_SHA256,
                "model3_pool_sha256": EXPECTED_MODEL3_POOL_SHA256,
            },
        }
        if value != expected:
            raise ValueError
    except (
        KeyError,
        ValueError,
        OSError,
        HTTPError,
        URLError,
        json.JSONDecodeError,
        Model23ServiceConfigurationError,
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
