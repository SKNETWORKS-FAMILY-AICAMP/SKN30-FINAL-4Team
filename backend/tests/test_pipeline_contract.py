"""Dependency-light checks for document gates, state progression, and SIM.

Run directly with ``PYTHONPATH=backend python backend/tests/test_pipeline_contract.py``.
They intentionally do not invoke an OCR engine, LLM, network, or Supabase.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import warnings
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from app.models.outcomes import OutcomeCode
from app.models.pipeline import PipelineKind, RunStatus, StageName, StageStatus
from app.models.sim import SimAxis, SimCandidate, SimEvidence, SimGraphState
from app.pipelines.formats import HWPX_REQUIRED_MEMBERS, FormatError, validate_format
from app.pipelines.sim_graph import build_sim_graph, sim_outcome, state_from_mapping, state_to_mapping
from app.pipelines.stages import mark_stage, new_run


HWP_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture"
HWPX_CONTENTS = {
    "mimetype": b"application/hwp+zip",
    "META-INF/container.xml": b"<container/>",
    "Contents/content.hpf": b"<opf/>",
    "Contents/header.xml": b"<head/>",
    "Contents/section0.xml": b"<section/>",
}


def make_hwpx(
    *,
    missing: frozenset[str] = frozenset(),
    mimetype: bytes = HWPX_CONTENTS["mimetype"],
    duplicate: str | None = None,
) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, default_content in HWPX_CONTENTS.items():
            if name in missing:
                continue
            content = mimetype if name == "mimetype" else default_content
            compression = ZIP_STORED if name == "mimetype" else ZIP_DEFLATED
            archive.writestr(name, content, compress_type=compression)
        if duplicate is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(duplicate, HWPX_CONTENTS[duplicate])
    return buffer.getvalue()


def mark_first_member_encrypted(content: bytes) -> bytes:
    encrypted = bytearray(content)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        header = encrypted.find(signature)
        assert header >= 0
        offset = header + flag_offset
        flags = int.from_bytes(encrypted[offset : offset + 2], "little") | 0x1
        encrypted[offset : offset + 2] = flags.to_bytes(2, "little")
    return bytes(encrypted)


HWPX = make_hwpx()


def test_format_matrix() -> None:
    assert validate_format(PipelineKind.REQUEST, "request.hwp", content=HWP_MAGIC).source_kind == "hwp"
    assert validate_format(PipelineKind.REQUEST, "request.hwpx", content=HWPX).source_kind == "hwpx"
    for name, content in (("existing.hwp", HWP_MAGIC), ("existing.hwpx", HWPX), ("existing.pdf", b"%PDF-1.7")):
        assert validate_format(PipelineKind.EXISTING, name, content=content).filename == name
    try:
        validate_format(PipelineKind.REQUEST, "request.pdf", content=b"%PDF")
    except FormatError:
        pass
    else:
        raise AssertionError("Request must reject PDF")


@pytest.mark.parametrize("content", [b"PK\x03\x04request", make_hwpx(missing=frozenset({"mimetype"}))])
def test_hwpx_rejects_invalid_or_incomplete_zip(content: bytes) -> None:
    with pytest.raises(FormatError):
        validate_format(PipelineKind.REQUEST, "request.hwpx", content=content)


def test_hwpx_rejects_an_unrelated_zip() -> None:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("unrelated.txt", "not an HWPX")

    with pytest.raises(FormatError, match="missing required members"):
        validate_format(PipelineKind.REQUEST, "request.hwpx", content=buffer.getvalue())


def test_hwpx_rejects_invalid_mimetype() -> None:
    with pytest.raises(FormatError, match="mimetype is invalid"):
        validate_format(
            PipelineKind.REQUEST,
            "request.hwpx",
            content=make_hwpx(mimetype=b"application/zip"),
        )


@pytest.mark.parametrize("missing", sorted(HWPX_REQUIRED_MEMBERS))
def test_hwpx_rejects_each_missing_required_member(missing: str) -> None:
    with pytest.raises(FormatError, match="missing required members"):
        validate_format(
            PipelineKind.REQUEST,
            "request.hwpx",
            content=make_hwpx(missing=frozenset({missing})),
        )


def test_hwpx_rejects_duplicate_members() -> None:
    with pytest.raises(FormatError, match="duplicate ZIP members"):
        validate_format(
            PipelineKind.REQUEST,
            "request.hwpx",
            content=make_hwpx(duplicate="Contents/header.xml"),
        )


def test_hwpx_rejects_encrypted_required_member() -> None:
    with pytest.raises(FormatError, match="invalid required member"):
        validate_format(
            PipelineKind.REQUEST,
            "request.hwpx",
            content=mark_first_member_encrypted(HWPX),
        )


@pytest.mark.parametrize(
    "sample",
    sorted((Path(__file__).resolve().parents[2] / "samples" / "hwpx").glob("*.hwpx")),
)
def test_repository_hwpx_samples_pass_structure_validation(sample: Path) -> None:
    assert validate_format(
        PipelineKind.REQUEST,
        sample.name,
        content=sample.read_bytes(),
    ).source_kind == "hwpx"


def test_failed_common_ir_skips_downstream() -> None:
    state = new_run(PipelineKind.REQUEST)
    for stage in (StageName.FORMAT_VALIDATE, StageName.SOURCE_HASH, StageName.PERSIST_SOURCE):
        state = mark_stage(state, stage, StageStatus.SUCCEEDED, outcome=OutcomeCode.SUCCEEDED)
    state = mark_stage(state, StageName.COMMON_IR, StageStatus.UNAVAILABLE, outcome=OutcomeCode.UNAVAILABLE)
    assert state.status is RunStatus.FAILED
    assert all(item.status is StageStatus.SKIPPED for item in state.stages[4:])


def test_sim_round_trip_is_evidence_bound() -> None:
    request_evidence = tuple(
        SimEvidence("request", axis, "cloud service support", f"request:{axis.value}")
        for axis in (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)
    )
    candidate = SimCandidate(
        candidate_id="existing-1",
        title="Cloud support",
        evidence=tuple(
            SimEvidence("candidate", axis, "cloud service support", f"candidate:{axis.value}")
            for axis in (SimAxis.PURPOSE, SimAxis.TARGET, SimAxis.CONTENT)
        ),
    )
    result = build_sim_graph(prefer_langgraph=False).invoke(
        SimGraphState(request_evidence=request_evidence, candidates=(candidate,))
    )
    restored = state_from_mapping(state_to_mapping(result))
    assert restored.ranked[0].candidate_id == "existing-1"
    assert sim_outcome(restored) is OutcomeCode.SUCCEEDED


def main() -> None:
    tests = [test_format_matrix, test_failed_common_ir_skips_downstream, test_sim_round_trip_is_evidence_bound]
    for test in tests:
        test()
        print(f"✓ {test.__name__}")
    print(f"{len(tests)} pipeline contract tests passed")


if __name__ == "__main__":
    main()
