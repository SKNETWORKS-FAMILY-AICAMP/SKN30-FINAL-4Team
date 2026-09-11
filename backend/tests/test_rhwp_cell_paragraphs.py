"""표 셀 안 문단이 하나로 뭉개지지 않는다는 것을 고정한다.

공문 서식은 한 셀에 사업기간·예산·지원조건·수행기관을 전부 넣는다. 이 목업은
한 셀이 26 문단 1000 자였고, 셀 전체가 후보 블록 하나가 되면서 LLM 이 그 안에서
20 여 개 필드의 정확한 문자 좌표를 한꺼번에 골라야 했다.

문단 경계는 원본 HWPX 가 이미 갖고 있다. 투영 코드(``common_ir_v1``)도
``cell["text_occurrence_ids"]`` 를 문단으로 열거한다. 어댑터만 ``"\n".join()``
으로 합치고 있어서 계약이 끊겼다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from worker import vendor
from common_ir_pipeline.schema import validation_errors


def _rhwp_payload() -> dict:
    def paragraph(text: str) -> dict:
        return {"kind": "paragraph", "text": text, "prov": {}}

    return {
        "version": "test", "core_version": "test", "page_count": 1,
        "ir": {"body": [{
            "kind": "table", "prov": {},
            "cells": [
                # 여러 문단을 담은 셀: 문단마다 따로 접지돼야 한다.
                {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "blocks": [
                    paragraph("○ (사업기간) 2026. 01. 01. ~ 2028. 12. 31."),
                    paragraph("○ (지원조건)"),
                    paragraph("- 업력: 업력 3년 이상 10년 이내"),
                ]},
                # 한 문단짜리 셀: 셀이 곧 문단이므로 출력이 바뀌지 않는다.
                {"row": 0, "col": 1, "row_span": 1, "col_span": 1, "blocks": [
                    paragraph("사업명"),
                ]},
            ],
        }]},
    }


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> dict:
    tmp = tmp_path_factory.mktemp("rhwp")
    source = tmp / "source.hwpx"
    source.write_bytes(b"not a real hwpx; only its digest is used")
    raw = tmp / "raw.json"
    raw.write_text(json.dumps(_rhwp_payload()), encoding="utf-8")
    output = tmp / "out.json"
    subprocess.run(
        [
            sys.executable, "-m", "common_ir_pipeline.adapters.rhwp",
            "--notice-id", "test", "--rhwp", str(raw),
            "--source-kind", "hwpx", "--source-path", str(source),
            "--source-sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
            "--output", str(output),
        ],
        check=True, capture_output=True,
        env={**__import__("os").environ, "PYTHONPATH": str(Path(vendor.VENDOR_PATHS[0]))},
    )
    return json.loads(output.read_text(encoding="utf-8"))


def _cell(document: dict, cell_id: str) -> dict:
    return next(
        row
        for block in document["blocks"]
        for row in block.get("cells", [])
        if row["cell_id"] == cell_id
    )


def _occurrence_text(document: dict) -> dict[str, str]:
    return {
        row["occurrence_id"]: row.get("text", "")
        for block in document["blocks"]
        for row in block.get("occurrences", [])
    }


def test_a_multi_paragraph_cell_addresses_each_paragraph(document: dict) -> None:
    ids = _cell(document, "hwpx:t0:c0")["text_occurrence_ids"]
    texts = _occurrence_text(document)

    assert len(ids) == 3
    assert [texts[row] for row in ids] == [
        "○ (사업기간) 2026. 01. 01. ~ 2028. 12. 31.",
        "○ (지원조건)",
        "- 업력: 업력 3년 이상 10년 이내",
    ]


def test_paragraphs_rejoin_to_the_untouched_cell_text(document: dict) -> None:
    texts = _occurrence_text(document)
    cell = _cell(document, "hwpx:t0:c0")

    # evidence_ids 는 셀 전체를 계속 가리킨다. 문단 분리는 추가지 대체가 아니다.
    whole = texts[cell["evidence_ids"][0]]
    assert "\n".join(texts[row] for row in cell["text_occurrence_ids"]) == whole


# 한 문단짜리 셀은 자기 자신이 그 문단이다. 같은 텍스트를 두 번 싣지 않는다.
def test_a_single_paragraph_cell_is_unchanged(document: dict) -> None:
    cell = _cell(document, "hwpx:t0:c1")

    assert cell["text_occurrence_ids"] == cell["evidence_ids"] == ["occ:rhwp:t0:c1"]
    assert _occurrence_text(document)["occ:rhwp:t0:c1"] == "사업명"


def test_the_split_document_still_validates(document: dict) -> None:
    assert validation_errors(document) == []
