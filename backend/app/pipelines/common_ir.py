"""Common IR v1 runner wrapper.

This layer does not vendor-copy the parser. It probes the handed-off
common_ir_pipeline package and rhwp/pdf runtimes. When they are missing it
returns UNAVAILABLE and NEEDS_REVIEW instead of synthesizing IR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.outcomes import ErrorCode, OutcomeCode
from app.models.pipeline import PipelineKind
from app.pipelines.availability import module_available, probe_modules
from app.pipelines.formats import FormatDecision, validate_format
from app.pipelines.hashing import sha256_file, verify_source_sha256

VENDOR_COMMON_IR_SRC = (
    Path(__file__).resolve().parents[2] / "vendor" / "common_ir_pipeline" / "src"
)

DECLARED_CONSTRAINTS = {
    "ocr_called": False,
    "pdf_processed": False,
    "gpu_used": False,
    "network_access": False,
    "model_download": False,
}


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    common_ir_package: bool
    rhwp: bool
    vendor_src_present: bool
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommonIrRunResult:
    outcome: OutcomeCode
    review_status: OutcomeCode
    error_code: str | None
    message: str
    source_sha256: str | None
    format: FormatDecision | None
    document: dict[str, Any] | None = None
    constraints: dict[str, bool] = field(default_factory=lambda: dict(DECLARED_CONSTRAINTS))
    runtime: RuntimeProbe | None = None


def probe_common_ir_runtime() -> RuntimeProbe:
    vendor_src_present = (VENDOR_COMMON_IR_SRC / "common_ir_pipeline").is_dir()
    package = module_available("common_ir_pipeline") or vendor_src_present
    rhwp = module_available("rhwp")
    details = []
    if not vendor_src_present and not module_available("common_ir_pipeline"):
        details.append("common_ir_pipeline package is not importable")
    if not rhwp:
        details.append("rhwp-python is not importable")
    probes = probe_modules("rhwp", "fitz")
    details.extend(f"{item.name} available={item.available}" for item in probes)
    return RuntimeProbe(
        common_ir_package=package,
        rhwp=rhwp,
        vendor_src_present=vendor_src_present,
        details=tuple(details),
    )


def run_common_ir(
    kind: PipelineKind,
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    content: bytes | None = None,
) -> CommonIrRunResult:
    source = Path(path)
    try:
        decision = validate_format(kind, source, content=content)
    except ValueError as exc:
        code = getattr(exc, "error_code", ErrorCode.UNSUPPORTED_FORMAT)
        return CommonIrRunResult(
            outcome=OutcomeCode.FAILED,
            review_status=OutcomeCode.NEEDS_REVIEW,
            error_code=str(code),
            message=str(exc),
            source_sha256=None,
            format=None,
            runtime=probe_common_ir_runtime(),
        )

    if not source.is_file():
        return CommonIrRunResult(
            outcome=OutcomeCode.FAILED,
            review_status=OutcomeCode.NEEDS_REVIEW,
            error_code=ErrorCode.UNSUPPORTED_FORMAT,
            message=f"source file does not exist: {source}",
            source_sha256=None,
            format=decision,
            runtime=probe_common_ir_runtime(),
        )

    digest = verify_source_sha256(source, expected_sha256) if expected_sha256 else sha256_file(source)
    runtime = probe_common_ir_runtime()

    if decision.source_kind in {"hwp", "hwpx"} and not runtime.rhwp:
        return CommonIrRunResult(
            outcome=OutcomeCode.UNAVAILABLE,
            review_status=OutcomeCode.NEEDS_REVIEW,
            error_code=ErrorCode.PARSER_UNAVAILABLE,
            message="rhwp runtime is unavailable; Common IR is not synthesized",
            source_sha256=digest,
            format=decision,
            runtime=runtime,
        )

    if decision.source_kind == "pdf":
        return CommonIrRunResult(
            outcome=OutcomeCode.UNAVAILABLE,
            review_status=OutcomeCode.NEEDS_REVIEW,
            error_code=ErrorCode.PARSER_UNAVAILABLE,
            message=(
                "native PDF Common IR requires a precomputed native extraction; "
                "this wrapper does not run OCR or invent semantic text"
            ),
            source_sha256=digest,
            format=decision,
            runtime=runtime,
        )

    if not runtime.common_ir_package:
        return CommonIrRunResult(
            outcome=OutcomeCode.UNAVAILABLE,
            review_status=OutcomeCode.NEEDS_REVIEW,
            error_code=ErrorCode.PARSER_UNAVAILABLE,
            message="common_ir_pipeline is unavailable; Common IR is not synthesized",
            source_sha256=digest,
            format=decision,
            runtime=runtime,
        )

    # A live rhwp parse is a deployment concern. This wrapper never starts it
    # from unit tests or an offline host: absence is explicit, presence still
    # requires an operator-invoked vendor runner.
    return CommonIrRunResult(
        outcome=OutcomeCode.UNAVAILABLE,
        review_status=OutcomeCode.NEEDS_REVIEW,
        error_code=ErrorCode.PARSER_UNAVAILABLE,
        message=(
            "external Common IR runner is not executed by this wrapper; "
            "use the vendor common-ir-rhwp entrypoint in a provisioned runtime"
        ),
        source_sha256=digest,
        format=decision,
        runtime=runtime,
    )
