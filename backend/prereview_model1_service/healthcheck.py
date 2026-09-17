#!/usr/bin/env python3
"""Authenticated, identity-pinned healthcheck for the resident Model 1 service."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .runtime import EXPECTED_WEIGHT_SHA256
from .settings import Model1ServiceConfigurationError, load_settings


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RESPONSE_BYTES = 16_384


def main() -> int:
    """Return success only for the configured service and immutable runtime."""

    try:
        expected_manifest = os.environ[
            "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256"
        ]
        if _SHA256.fullmatch(expected_manifest) is None:
            raise ValueError
        settings = load_settings()
        request = Request(
            "http://127.0.0.1:8791/v1/model1/ready",
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
        if not isinstance(value, dict) or set(value) != {
            "ready",
            "max_concurrency",
            "max_request_bytes",
            "device",
            "producer",
        }:
            raise ValueError
        producer = value.get("producer")
        if (
            value.get("ready") is not True
            or value.get("max_concurrency") != 1
            or value.get("max_request_bytes") != settings.max_request_bytes
            or value.get("device") != settings.device
            or not isinstance(producer, dict)
            or producer
            != {
                "service": "prereview-model1",
                "weight_sha256": EXPECTED_WEIGHT_SHA256,
                "runtime_manifest_sha256": expected_manifest,
            }
        ):
            raise ValueError
    except (KeyError, ValueError, OSError, HTTPError, URLError, json.JSONDecodeError,
            Model1ServiceConfigurationError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
