"""Offline document, Common IR, and SIM pipeline helpers."""

from app.pipelines.artifacts import (
    ObjectKeyError,
    build_artifact_metadata,
    existing_object_key,
    report_object_key,
    request_cleanup_prefix,
    request_object_key,
)
from app.pipelines.common_ir import CommonIrRunResult, probe_common_ir_runtime, run_common_ir
from app.pipelines.document import SourcePreflight, preflight_source
from app.pipelines.formats import FormatDecision, FormatError, validate_format
from app.pipelines.hashing import SourceHashError, normalize_sha256, sha256_bytes, sha256_file, verify_source_sha256
from app.pipelines.sim_graph import CompiledSimGraph, build_sim_graph, sim_outcome
from app.pipelines.stages import mark_stage, new_run, stage_map

__all__ = [
    "CommonIrRunResult",
    "CompiledSimGraph",
    "FormatDecision",
    "FormatError",
    "ObjectKeyError",
    "SourceHashError",
    "SourcePreflight",
    "build_artifact_metadata",
    "build_sim_graph",
    "existing_object_key",
    "mark_stage",
    "new_run",
    "normalize_sha256",
    "preflight_source",
    "probe_common_ir_runtime",
    "report_object_key",
    "request_cleanup_prefix",
    "request_object_key",
    "run_common_ir",
    "sha256_bytes",
    "sha256_file",
    "sim_outcome",
    "stage_map",
    "validate_format",
    "verify_source_sha256",
]
