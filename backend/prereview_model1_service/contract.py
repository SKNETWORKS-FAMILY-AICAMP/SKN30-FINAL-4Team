"""Shared wire limits for the resident Model 1 HTTP protocol."""

from __future__ import annotations

__all__ = [
    "MODEL1_DEFAULT_MAX_REQUEST_BYTES",
    "MODEL1_MAX_REQUEST_BYTES",
    "MODEL1_MIN_REQUEST_BYTES",
]


# The worker serializes the exact four-field request before sending it and the
# sidecar applies the same cap while streaming the body.  Keep this value in a
# dependency-free module so both processes use one protocol contract.
MODEL1_DEFAULT_MAX_REQUEST_BYTES = 65_536
MODEL1_MIN_REQUEST_BYTES = 512
MODEL1_MAX_REQUEST_BYTES = 1_048_576
