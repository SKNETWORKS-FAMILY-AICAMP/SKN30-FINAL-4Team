"""Dependency-free wire constants for the resident Model 2/3 protocol."""

from __future__ import annotations

__all__ = [
    "MODEL23_DEFAULT_MAX_REQUEST_BYTES",
    "MODEL23_MAX_CONCURRENCY",
    "MODEL23_MAX_REQUEST_BYTES",
    "MODEL23_MIN_REQUEST_BYTES",
    "MODEL2_RESULT_SCHEMA_VERSION",
    "MODEL3_RESULT_SCHEMA_VERSION",
]


# Common IR evidence can legitimately exceed the Model 1 four-field request.
# The service still applies a hard streaming cap before JSON decoding.
MODEL23_MIN_REQUEST_BYTES = 65_536
MODEL23_DEFAULT_MAX_REQUEST_BYTES = 2_097_152
MODEL23_MAX_REQUEST_BYTES = 8_388_608

# The frozen serving modules own process-global import caches and reference
# frames.  One shared slot keeps Model 2 and Model 3 calls deterministic.
MODEL23_MAX_CONCURRENCY = 1

MODEL2_RESULT_SCHEMA_VERSION = "prereview.model2-predict-result/v1"
MODEL3_RESULT_SCHEMA_VERSION = "prereview.model3-predict-result/v1"
