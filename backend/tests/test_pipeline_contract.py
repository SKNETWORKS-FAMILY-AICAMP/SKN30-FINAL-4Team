"""Dependency-light checks for document gates, state progression, and SIM.

Run directly with ``PYTHONPATH=backend python backend/tests/test_pipeline_contract.py``.
They intentionally do not invoke an OCR engine, LLM, network, or Supabase.
"""

from __future__ import annotations

from app.models.outcomes import OutcomeCode
from app.models.pipeline import PipelineKind, RunStatus, StageName, StageStatus
from app.models.sim import SimAxis, SimCandidate, SimEvidence, SimGraphState
from app.pipelines.formats import FormatError, validate_format
from app.pipelines.sim_graph import build_sim_graph, sim_outcome, state_from_mapping, state_to_mapping
from app.pipelines.stages import mark_stage, new_run


HWP_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture"


def test_format_matrix() -> None:
    assert validate_format(PipelineKind.REQUEST, "request.hwp", content=HWP_MAGIC).source_kind == "hwp"
    assert validate_format(PipelineKind.REQUEST, "request.hwpx", content=b"PK\x03\x04").source_kind == "hwpx"
    for name, content in (("existing.hwp", HWP_MAGIC), ("existing.hwpx", b"PK\x03\x04"), ("existing.pdf", b"%PDF-1.7")):
        assert validate_format(PipelineKind.EXISTING, name, content=content).filename == name
    try:
        validate_format(PipelineKind.REQUEST, "request.pdf", content=b"%PDF")
    except FormatError:
        pass
    else:
        raise AssertionError("Request must reject PDF")


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
