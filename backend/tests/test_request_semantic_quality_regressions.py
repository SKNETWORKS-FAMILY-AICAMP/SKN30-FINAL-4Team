"""Offline semantic invariants recovered from the mockup_08 live E2E.

This deliberately stops before an LLM call.  The checked-in HWPX is parsed by
the production Common-IR entrypoint, and only stable field/source locators are
asserted.  Generated IDs, timestamps, summaries, and a whole Profile snapshot
would make this a brittle test of unrelated implementation details.
"""

from __future__ import annotations

from importlib.util import find_spec
import os
from pathlib import Path
from typing import Any

import pytest

from worker.profiles import parse_to_common_ir

from semantic_structuring.request_completeness import (
    request_semantic_completeness_failures,
)
from semantic_structuring.request_profile_v012 import build_request_candidate_pack


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MOCKUP_08 = (
    _REPOSITORY_ROOT
    / "samples"
    / "hwpx"
    / "mockup_08_CPL전항목_스마트기술사업화.hwpx"
)


def _selected_row(block_id: str, text: str, selected: str | None = None) -> dict[str, Any]:
    value = text if selected is None else selected
    start = text.index(value)
    return {
        "value_source": {
            "source_block_id": block_id,
            "start_char": start,
            "end_char": start + len(value),
        }
    }


def test_mockup_08_semantic_completeness_rejects_workflow_only_then_closes(
    tmp_path: Path,
) -> None:
    freetype = os.environ.get("PREREVIEW_FREETYPE_LIB", "").strip()
    if find_spec("rhwp") is None or not freetype or not Path(freetype).is_file():
        pytest.skip(
            "run in the worker image with PREREVIEW_FREETYPE_LIB configured"
        )
    artifact = parse_to_common_ir(
        input_path=_MOCKUP_08,
        notice_id="PREREVIEW-SEMANTIC-REGRESSION-08",
        source_kind="hwpx",
        run_dir=tmp_path / "common-ir",
    )
    pack = build_request_candidate_pack(artifact.document)
    blocks = {block.block_id: block.text for block in pack.blocks}

    # This is the exact omission shape from the 2026-09-15 live run: only the
    # seven-step workflow was selected; annual rows, execution method, and
    # the two parenthesised organisation roles were absent.
    workflow_id = "hwpx:t4#r3c1p24"
    failures = request_semantic_completeness_failures(
        pack,
        request_context={
            "implementation_plan": [
                _selected_row(workflow_id, blocks[workflow_id])
            ]
        },
        comparison_profile={
            "delivery_methods": [],
            "delivery_relations": [
                {"role": None},
                {"role": None},
                {"role": None},
            ],
        },
    )

    assert len(failures) == 3
    assert all(
        block_id in failures[0]
        for block_id in (
            "hwpx:t4#r3c1p3",
            "hwpx:t4#r3c1p4",
            "hwpx:t4#r3c1p5",
        )
    )
    assert "hwpx:t4#r3c1p23" in failures[1]
    assert "hwpx:t4#r3c1p22" in failures[2]
    assert "부산테크노파크" not in " ".join(failures)

    plan_rows = [
        _selected_row(block_id, blocks[block_id])
        for block_id in (
            "hwpx:t4#r3c1p3",
            "hwpx:t4#r3c1p4",
            "hwpx:t4#r3c1p5",
        )
    ]
    method_id = "hwpx:t4#r3c1p23"
    roles_id = "hwpx:t4#r3c1p22"
    repaired = request_semantic_completeness_failures(
        pack,
        request_context={"implementation_plan": plan_rows},
        comparison_profile={
            "delivery_methods": [
                _selected_row(
                    method_id,
                    blocks[method_id],
                    "시 출연기관 위탁(보조)",
                )
            ],
            "delivery_relations": [
                {
                    "role": _selected_row(
                        roles_id, blocks[roles_id], "주관"
                    )
                },
                {
                    "role": _selected_row(
                        roles_id, blocks[roles_id], "협력"
                    )
                },
            ],
        },
    )

    # 이 문서의 내역사업명은 행정 계층이고, 1·2단계는 독립적인 수혜자·자격
    # 경계가 없는 추진계획이다. 세 계획 행을 모두 선택하면 가짜
    # support_component 없이도 의미 완결성 검사를 닫을 수 있어야 한다.
    assert len(plan_rows) == 3
    assert repaired == []
