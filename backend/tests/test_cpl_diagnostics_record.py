"""CPL 진단이 로컬 기록기까지 도달하는지 고정한다.

``CplResult.diagnostics`` 는 결과 payload 에 실리지 않는다. 프론트 계약을 늘리지
않기로 했기 때문이다. 그러면 재검 탈락 사유처럼 "무엇이 왜 빠졌는가" 가 어디에도
남지 않는다 — 실제 라이브 실행에서 기대효과 한 행이 좌표 불일치로 거절됐는데
``03_cpl.json`` 에도 ``07_result.json`` 에도 그 사실이 없었다.

그래서 엔진이 진단을 밖으로 흘려보내고, 로컬 기록기가 받아 적는다. 공개 응답은
그대로다. 분석이나 LLM 을 다시 돌려 진단을 만들어 내지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from worker.analysis_job import CoreAnalysisEngine
from worker.contracts.profile_snapshot import StageDiagnostic


_RUNNER = (
    Path(__file__).resolve().parents[1] / "scripts" / "run_local_live_e2e.py"
)


def _runner() -> Any:
    spec = importlib.util.spec_from_file_location("_e2e_runner", _RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Dead:
    async def generate_structured(self, **_: Any) -> Any:
        raise RuntimeError("이 테스트는 LLM 을 타지 않는다")


_REGION = "○ (사업목적) 부산 관내 제조 중소기업의 기술경쟁력을 강화"


def _ir() -> dict[str, Any]:
    return {
        "document": {"document_id": "hwpx:d1"},
        "blocks": [{
            "block_id": "hwpx:t4",
            "occurrences": [{"occurrence_id": "occ:p0", "text": _REGION}],
        }],
    }


def _empty_purpose_profile() -> dict[str, Any]:
    """구역은 있는데 값이 없다. 재검이 돌고 실패하면 진단이 남는다."""

    return {
        "comparison_profile": {"purpose_goal": []},
        "field_states": [{"field_name": "purpose_goal", "status": "not_found"}],
    }


def _engine() -> CoreAnalysisEngine:
    return CoreAnalysisEngine(
        _Dead(),
        cpl_model_profile="cpl",
        fit_model_profile="fit",
        sim_model_profile="sim",
    )


def test_the_engine_hands_cpl_diagnostics_to_its_sink() -> None:
    received: list[tuple[str, list[StageDiagnostic]]] = []
    engine = _engine()
    engine.set_diagnostics_sink(lambda stage, rows: received.append((stage, list(rows))))

    engine.build_payload(
        profile=_empty_purpose_profile(), common_ir=_ir(), candidates=[]
    )

    assert [stage for stage, _rows in received] == ["cpl"]
    codes = [row.reason_code for _stage, rows in received for row in rows]
    # 재검이 전송에 실패했다는 사실이 진단으로 나온다.
    assert codes


def test_cpl_diagnostics_never_reach_the_public_payload() -> None:
    """FIT 관계 진단은 예전부터 실린다. CPL 것은 실리지 않는다."""

    payload = _engine().build_payload(
        profile=_empty_purpose_profile(), common_ir=_ir(), candidates=[]
    )

    axes = payload["axes"]
    cpl_axes = [row for row in axes if row["axis_type"] == "CPL"]
    assert cpl_axes                                   # 볼 대상이 있다
    assert all("diagnostics" not in row["result_data"] for row in cpl_axes)
    # 그래서 sink 가 없으면 CPL 진단은 어디에도 남지 않는다.
    received: list[Any] = []
    engine = _engine()
    engine.set_diagnostics_sink(lambda stage, rows: received.append(rows))
    engine.build_payload(
        profile=_empty_purpose_profile(), common_ir=_ir(), candidates=[]
    )
    assert received


def test_the_recorder_writes_the_diagnostics_beside_the_stages(tmp_path: Path) -> None:
    runner = _runner()
    rows = [
        {
            "stage": "cpl_recheck",
            "unit": "request_context.expected_effect",
            "reason_code": None,
            "message": "기대효과 구역 4개: 추가 2건, 탈락 1건 (좌표가 인용문과 맞지 않는다)",
            "attempt": 1,
        }
    ]

    runner._write_trace(
        tmp_path / "trace",
        upload={},
        common_ir={},
        structured_profile={},
        result={"cpl": {}, "fit": {}, "sim": {}, "ml": {}},
        cpl_diagnostics={"analysis_run_id": "run-1", "diagnostics": rows},
    )

    saved = json.loads(
        (tmp_path / "trace" / "cpl_diagnostics.json").read_text(encoding="utf-8")
    )
    assert saved["analysis_run_id"] == "run-1"
    assert saved["diagnostics"] == rows
    # 사유가 개수만이 아니라 문구까지 남는다.
    assert "좌표가 인용문과 맞지 않는다" in saved["diagnostics"][0]["message"]


def test_a_run_without_diagnostics_still_writes_the_file(tmp_path: Path) -> None:
    runner = _runner()

    runner._write_trace(
        tmp_path / "trace",
        upload={},
        common_ir={},
        structured_profile={},
        result={},
    )

    saved = json.loads(
        (tmp_path / "trace" / "cpl_diagnostics.json").read_text(encoding="utf-8")
    )
    assert saved == []
