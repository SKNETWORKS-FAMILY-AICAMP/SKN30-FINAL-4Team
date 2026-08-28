import asyncio
import io
import json
import os
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.core.config import Settings
from app.core.security import hash_password
from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.ports.document_parser import FileSource
from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.schemas.cpl import (
    CPL_FIELDS,
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplSemanticResponse,
    CplSourceRole,
    CplStatus,
)
from app.schemas.parsed_document import DocumentBlock, ParsedDocument
from app.services.analysis_pipeline import (
    _complete_semantic_review,
    _semantic_messages,
    load_cpl_prompt,
)
from app.services.cpl.logic_validator import (
    evaluate_cpl_rules,
    ground_llm_response,
    merge_llm_result,
    semantic_fragments,
)


JWT_SECRET = "test-secret-that-is-at-least-32-bytes"
PASSWORD = "correct-horse"


def parsed_document() -> ParsedDocument:
    return ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="CPL fixture",
        blocks=[
            DocumentBlock(
                block_id="request:1",
                block_type="paragraph",
                text="사전협의 요청유형: ■ 세부사업 신설  □ 내역사업 신설  □ 내내역사업 신설  □ 사업내용 변경",
                page_no=1,
                section_path=["신청 개요"],
                source_locator={"paragraph_index": 1},
            ),
            DocumentBlock(
                block_id="purpose:1",
                block_type="paragraph",
                text="사업 목적: 지역 기업의 디지털 전환 지원",
                page_no=2,
                section_path=["사업 개요", "목적"],
                source_locator={"paragraph_index": 2},
            ),
            DocumentBlock(
                block_id="purpose:2",
                block_type="paragraph",
                text="사업 목적: 생산성 향상",
                page_no=3,
                section_path=["사업 개요", "목적"],
                source_locator={"paragraph_index": 3},
            ),
            DocumentBlock(
                block_id="need:1",
                block_type="paragraph",
                text="사업 필요성: 지역 기업의 설비 노후화가 심각함",
                page_no=4,
                section_path=["필요성"],
                source_locator={"paragraph_index": 4},
            ),
            DocumentBlock(
                block_id="period:1",
                block_type="paragraph",
                text="사업기간: 2026년 ~ 2028년",
                page_no=5,
                section_path=["사업 개요"],
                source_locator={"paragraph_index": 5},
            ),
            DocumentBlock(
                block_id="budget:1",
                block_type="paragraph",
                text="사업예산: 3억원",
                page_no=6,
                section_path=["예산"],
                source_locator={"paragraph_index": 6},
            ),
        ],
    )


def table_document() -> ParsedDocument:
    cells = [
        {"row": 0, "col": 0, "text": "지원내용"},
        {"row": 0, "col": 1, "text": "기업당 최대 3천만원, 100개사"},
        {"row": 1, "col": 0, "text": "지원규모"},
        {"row": 1, "col": 1, "text": "기업당 최대 3천만원, 100개사"},
    ]
    return ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원내용 및 지원규모",
        blocks=[
            DocumentBlock(
                block_id="table:1",
                block_type="table",
                text="지원내용 기업당 최대 3천만원, 100개사",
                page_no=7,
                section_path=["지원계획"],
                source_locator={"table_index": 1, "cells": cells},
            )
        ],
    )


def implementation_plan_document(
    period: str,
    plan_lines: list[str],
) -> ParsedDocument:
    return ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="추진계획 fixture",
        blocks=[
            DocumentBlock(
                block_id="period:plan",
                block_type="paragraph",
                text=f"사업기간: {period}",
                source_locator={"paragraph_index": 1},
            ),
            *[
                DocumentBlock(
                    block_id=f"plan:{index}",
                    block_type="paragraph",
                    text=line,
                    source_locator={"paragraph_index": index + 2},
                )
                for index, line in enumerate(plan_lines)
            ],
        ],
    )


def result_with_statuses(statuses: list[CplStatus]) -> CplResult:
    return CplResult(
        ruleset_version="cpl-alpha-v0.1",
        items=[
            CplItem(field_code=field_code, status=status)
            for field_code, status in zip(CPL_FIELDS, statuses, strict=True)
        ],
    )


def test_cpl_contract_has_exact_fields_and_confirmation_rate() -> None:
    assert [field.value for field in CPL_FIELDS] == [
        "REQUEST_TYPE",
        "PURPOSE_GOAL",
        "IMPLEMENTATION_PLAN",
        "BUSINESS_PERIOD",
        "NEW_OR_CHANGED_CONTENT",
        "BUSINESS_NEED",
        "LEGAL_BASIS",
        "LINKED_POLICY",
        "BUDGET",
        "TARGET_AND_CONDITIONS",
        "SUPPORT_CONTENT_AND_SCALE",
        "DELIVERY_SYSTEM",
        "EXPECTED_EFFECTS_AND_PERFORMANCE",
    ]
    result = result_with_statuses(
        [
            CplStatus.PRESENT,
            CplStatus.NOT_APPLICABLE,
            CplStatus.PRESENT,
            *([CplStatus.MISSING] * 10),
        ]
    )
    assert result.confirmed_count == 3
    assert result.total_count == 13
    assert result.confirmation_rate == 3 / 13 * 100


def test_rules_preserve_every_occurrence_and_parser_lineage() -> None:
    result = evaluate_cpl_rules(parsed_document())
    purpose = next(
        item for item in result.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    assert purpose.status == CplStatus.NEEDS_CONFIRMATION
    assert [occurrence.raw_text for occurrence in purpose.occurrences] == [
        "지역 기업의 디지털 전환 지원",
        "생산성 향상",
    ]
    assert [occurrence.page_no for occurrence in purpose.occurrences] == [2, 3]
    assert [occurrence.block_id for occurrence in purpose.occurrences] == [
        "purpose:1",
        "purpose:2",
    ]
    for occurrence in purpose.occurrences:
        assert occurrence.normalized_value
        assert occurrence.section_path == ["사업 개요", "목적"]
        assert occurrence.source_locator
        assert occurrence.extraction_method == "RULE"
    assert all(item.status != CplStatus.PARSE_FAILED for item in result.items)


def test_llm_failure_keeps_rule_evidence_and_marks_only_unresolved_semantics() -> None:
    rule_result = evaluate_cpl_rules(parsed_document())
    result = merge_llm_result(rule_result, llm_error="LLM_TIMEOUT")
    purpose = next(
        item for item in result.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    need = next(
        item for item in result.items if item.field_code == CplFieldCode.BUSINESS_NEED
    )
    plan = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert purpose.status == CplStatus.NEEDS_CONFIRMATION
    assert purpose.reason_code == "LLM_TIMEOUT"
    assert len(purpose.occurrences) == 2
    assert need.status == CplStatus.NEEDS_CONFIRMATION
    assert need.reason_code == "LLM_TIMEOUT"
    assert need.occurrences[0].raw_text.startswith("지역 기업")
    assert plan.status == CplStatus.NEEDS_CONFIRMATION
    assert plan.reason_code == "LLM_TIMEOUT"
    assert plan.occurrences == []
    assert "CPL semantic extraction incomplete: LLM_TIMEOUT" in result.warnings


def test_llm_evidence_is_grounded_from_parser_metadata() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "BUSINESS_NEED",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "need:1",
                            "raw_text": "지역 기업의 설비 노후화가 심각함",
                            "axis_code": "NEED_PROBLEM",
                        }
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
    )
    occurrence = grounded[0].occurrences[0]
    assert occurrence.page_no == 4
    assert occurrence.section_path == ["필요성"]
    assert occurrence.source_locator == {"paragraph_index": 4}
    assert occurrence.extraction_method == "LLM"
    assert occurrence.axis_code == CplAxisCode.NEED_PROBLEM
    assert occurrence.normalized_value == {
        "text": "지역 기업의 설비 노후화가 심각함"
    }

    # 원문에 없는 인용은 그 근거만 버린다. 항목은 남되 PRESENT 를 잃는다.
    response.items[0].occurrences[0].raw_text = "문서에 없는 문장"
    regrounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
    )
    assert regrounded[0].occurrences == []
    assert regrounded[0].status == CplStatus.NEEDS_CONFIRMATION


def test_table_cells_have_unique_refs_and_deterministic_roles() -> None:
    fragments = semantic_fragments(table_document())
    values = [fragment for fragment in fragments if fragment.text.startswith("기업당")]
    assert [fragment.evidence_ref for fragment in values] == [
        "table:1:cell:0:1",
        "table:1:cell:1:1",
    ]
    assert [fragment.source_role for fragment in values] == [
        CplSourceRole.SUPPORT_CONTENT,
        CplSourceRole.SUPPORT_SCALE,
    ]

    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "SUPPORT_CONTENT_AND_SCALE",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "table:1:cell:0:1",
                            "raw_text": "3천만원",
                            "axis_code": "PER_COMPANY_LIMIT",
                        },
                        {
                            "evidence_ref": "table:1:cell:1:1",
                            "raw_text": "3천만원",
                            "axis_code": "PER_COMPANY_LIMIT",
                        },
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        table_document(), response, {CplFieldCode.SUPPORT_CONTENT_AND_SCALE}
    )
    assert len(grounded[0].occurrences) == 2
    assert [item.source_role for item in grounded[0].occurrences] == [
        CplSourceRole.SUPPORT_CONTENT,
        CplSourceRole.SUPPORT_SCALE,
    ]
    assert all(
        item.normalized_value == {"amount_won": 30_000_000, "unit": "KRW"}
        for item in grounded[0].occurrences
    )


def test_flattened_table_inline_segments_become_separate_rule_fragments() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="inline segment fixture",
        blocks=[
            DocumentBlock(
                block_id="table:segments",
                block_type="table",
                text="원문은 붙어 있음",
                source_locator={
                    "table_index": 1,
                    "cells": [
                        {
                            "row": 0,
                            "col": 1,
                            "text": "○ 사업목적 : 해외시장 진출○ 사업필요성 : 판로 다변화",
                            "segments": [
                                {
                                    "segment_index": 0,
                                    "text": "○ 사업목적 : 해외시장 진출",
                                },
                                {
                                    "segment_index": 1,
                                    "text": "○ 사업필요성 : 판로 다변화",
                                },
                            ],
                            "structure_status": "unresolved",
                        }
                    ],
                },
            )
        ],
    )

    fragments = semantic_fragments(document)
    assert [fragment.evidence_ref for fragment in fragments] == [
        "table:segments:cell:0:1:segment:0",
        "table:segments:cell:0:1:segment:1",
    ]
    result = evaluate_cpl_rules(document)
    purpose = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    assert [occurrence.raw_text for occurrence in purpose.occurrences] == [
        "해외시장 진출",
    ]
    need = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.BUSINESS_NEED
    )
    assert [occurrence.raw_text for occurrence in need.occurrences] == [
        "판로 다변화",
    ]


def test_quantitative_axis_values_are_normalized_by_rule() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="numeric fixture",
        blocks=[
            DocumentBlock(
                block_id="numeric:1",
                block_type="paragraph",
                text=(
                    "기업당 최대 3천만원, 100개사, 보조율 50%, "
                    "성과목표 30%, 기준연도 2026년"
                ),
                source_locator={"paragraph_index": 1},
            )
        ],
    )
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "SUPPORT_CONTENT_AND_SCALE",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "numeric:1",
                            "raw_text": "3천만원",
                            "axis_code": "PER_COMPANY_LIMIT",
                        },
                        {
                            "evidence_ref": "numeric:1",
                            "raw_text": "100개사",
                            "axis_code": "COMPANY_COUNT",
                        },
                        {
                            "evidence_ref": "numeric:1",
                            "raw_text": "50%",
                            "axis_code": "SUBSIDY_RATE",
                        },
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                },
                {
                    "field_code": "EXPECTED_EFFECTS_AND_PERFORMANCE",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "numeric:1",
                            "raw_text": "30%",
                            "axis_code": "KPI_TARGET_VALUE",
                        },
                        {
                            "evidence_ref": "numeric:1",
                            "raw_text": "2026년",
                            "axis_code": "KPI_BASE_YEAR",
                        },
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                },
            ]
        }
    )
    grounded = ground_llm_response(
        document,
        response,
        {
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
        },
    )
    values = {
        occurrence.axis_code: occurrence.normalized_value
        for item in grounded
        for occurrence in item.occurrences
    }
    assert values[CplAxisCode.PER_COMPANY_LIMIT] == {
        "amount_won": 30_000_000,
        "unit": "KRW",
    }
    assert values[CplAxisCode.COMPANY_COUNT] == {"count": 100, "unit": "COMPANY"}
    assert values[CplAxisCode.SUBSIDY_RATE] == {"ratio": 0.5, "unit": "PERCENT"}
    assert values[CplAxisCode.KPI_TARGET_VALUE] == {"number": 30, "unit": "NUMBER"}
    assert values[CplAxisCode.KPI_BASE_YEAR] == {"year": 2026, "unit": "YEAR"}


@pytest.mark.parametrize(
    "occurrence",
    [
        {
            "evidence_ref": "missing:ref",
            "raw_text": "지역 기업",
            "axis_code": "NEED_PROBLEM",
        },
        {
            "evidence_ref": "need:1",
            "raw_text": "문서에 없는 문장",
            "axis_code": "NEED_PROBLEM",
        },
        {
            "evidence_ref": "need:1",
            "raw_text": "지역 기업",
            "axis_code": "PURPOSE_DIRECTION",
        },
    ],
)
def test_ungrounded_llm_evidence_is_dropped(occurrence: dict) -> None:
    """접지에 실패한 근거만 버리고 나머지 항목은 살린다.

    예전에는 예외를 올려 13개 항목 전체를 버렸다. 축을 많이 태깅할수록
    하나가 어긋날 확률이 커져 커버리지를 올리려는 시도가 실패율을 올렸다.
    """
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "BUSINESS_NEED",
                    "status": "PRESENT",
                    "occurrences": [occurrence],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
    )

    assert len(grounded) == 1
    item = grounded[0]
    assert item.occurrences == []
    # 근거가 남지 않으면 PRESENT 를 주장할 수 없다.
    assert item.status == CplStatus.NEEDS_CONFIRMATION
    assert item.reason_code == "EVIDENCE_NOT_GROUNDED"


def test_grounding_keeps_valid_evidence_when_one_occurrence_fails() -> None:
    """근거 하나가 어긋나도 같은 항목의 나머지 근거는 유지된다."""
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "BUSINESS_NEED",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "need:1",
                            "raw_text": "지역 기업",
                            "axis_code": "NEED_PROBLEM",
                        },
                        {
                            "evidence_ref": "need:1",
                            "raw_text": "문서에 없는 문장",
                            "axis_code": "NEED_PROBLEM",
                        },
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.BUSINESS_NEED}
    )

    item = grounded[0]
    assert item.status == CplStatus.PRESENT
    assert [o.raw_text for o in item.occurrences] == ["지역 기업"]


def test_purpose_axes_keep_verbatim_evidence_without_taxonomy_generation() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="사업 목적",
        blocks=[
            DocumentBlock(
                block_id="purpose:sim",
                block_type="paragraph",
                text="사업 목적: 중소기업의 해외시장 진출 확대",
                source_locator={"paragraph_index": 1},
            )
        ],
    )
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "purpose:sim",
                            "raw_text": "해외시장 진출",
                            "axis_code": "PURPOSE_PROBLEM_DOMAIN",
                        },
                        {
                            "evidence_ref": "purpose:sim",
                            "raw_text": "해외시장 진출",
                            "axis_code": "PURPOSE_SPECIFIC_OBJECTIVE",
                        },
                        {
                            "evidence_ref": "purpose:sim",
                            "raw_text": "확대",
                            "axis_code": "PURPOSE_DIRECTION",
                        },
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        document, response, {CplFieldCode.PURPOSE_GOAL}
    )
    assert [item.axis_code for item in grounded[0].occurrences[:2]] == [
        CplAxisCode.PURPOSE_PROBLEM_DOMAIN,
        CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
    ]
    assert [item.raw_text for item in grounded[0].occurrences[:2]] == [
        "해외시장 진출",
        "해외시장 진출",
    ]

    # 원문이 "해외시장 진출" 인데 "수출" 로 바꿔 인용하면 그 근거는 버려진다.
    response.items[0].occurrences[0].raw_text = "수출"
    regrounded = ground_llm_response(document, response, {CplFieldCode.PURPOSE_GOAL})
    assert all(
        occurrence.raw_text != "수출"
        for item in regrounded
        for occurrence in item.occurrences
    )


def test_cpl_prompt_is_versioned_and_messages_use_fragment_contract() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        cpl_prompt_path="backend/config/prompts/cpl-v0.2.txt",
    )
    prompt = load_cpl_prompt(settings.cpl_prompt_path)
    assert "raw_text must be a verbatim" in prompt
    messages = _semantic_messages(
        table_document(),
        {CplFieldCode.SUPPORT_CONTENT_AND_SCALE},
        prompt,
    )
    payload = json.loads(messages[1].content)
    assert payload["allowed_axes"]["SUPPORT_CONTENT_AND_SCALE"]
    assert payload["fragments"][0]["evidence_ref"] == "table:1:cell:0:0"
    assert "blocks" not in payload


def test_request_type_checkboxes_are_scoped_to_the_named_area() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="request type scope",
        blocks=[
            DocumentBlock(
                block_id="other:1",
                block_type="paragraph",
                text="참고: ■ 내역사업 신설",
                section_path=["기타"],
            ),
            DocumentBlock(
                block_id="request:scope",
                block_type="paragraph",
                text="사전협의 요청유형: ■ 세부사업 신설 □ 내역사업 신설",
                section_path=["신청 개요"],
            ),
        ],
    )
    result = evaluate_cpl_rules(document)
    item = next(
        value for value in result.items if value.field_code == CplFieldCode.REQUEST_TYPE
    )
    assert item.status == CplStatus.PRESENT
    assert {occurrence.block_id for occurrence in item.occurrences} == {"request:scope"}


def test_request_type_heading_can_scope_the_immediately_following_block() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="request type split across blocks",
        blocks=[
            DocumentBlock(
                block_id="request:heading",
                block_type="paragraph",
                text="사전협의 요청유형",
            ),
            DocumentBlock(
                block_id="request:choices",
                block_type="paragraph",
                text="■ 세부사업 신설 □ 내역사업 신설",
            ),
            DocumentBlock(
                block_id="other:checkbox",
                block_type="paragraph",
                text="■ 사업내용 변경",
                section_path=["기타"],
            ),
        ],
    )
    result = evaluate_cpl_rules(document)
    item = next(
        value for value in result.items if value.field_code == CplFieldCode.REQUEST_TYPE
    )
    assert item.status == CplStatus.PRESENT
    assert {occurrence.block_id for occurrence in item.occurrences} == {
        "request:choices"
    }


def test_merge_replaces_unaxis_rule_evidence_and_keeps_distinct_axes() -> None:
    statuses = [CplStatus.MISSING] * 13
    statuses[CPL_FIELDS.index(CplFieldCode.PURPOSE_GOAL)] = (
        CplStatus.NEEDS_CONFIRMATION
    )
    rule_result = result_with_statuses(statuses)
    purpose = next(
        item for item in rule_result.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    purpose.occurrences = [
        CplOccurrence(
            raw_text="해외시장 진출",
            normalized_value={"text": "해외시장 진출"},
            source_locator={"paragraph_index": 1},
            block_id="purpose:1",
            extraction_method="RULE",
        )
    ]
    candidate = CplItem(
        field_code=CplFieldCode.PURPOSE_GOAL,
        status=CplStatus.PRESENT,
        occurrences=[
            CplOccurrence(
                raw_text="해외시장 진출",
                normalized_value={"text": "해외시장 진출"},
                axis_code=axis,
                source_locator={"paragraph_index": 1},
                block_id="purpose:1",
                extraction_method="LLM",
            )
            for axis in (
                CplAxisCode.PURPOSE_PROBLEM_DOMAIN,
                CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
            )
        ],
    )
    merged = merge_llm_result(rule_result, [candidate])
    purpose = next(
        item for item in merged.items if item.field_code == CplFieldCode.PURPOSE_GOAL
    )
    assert [item.axis_code for item in purpose.occurrences] == [
        CplAxisCode.PURPOSE_PROBLEM_DOMAIN,
        CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
    ]


@pytest.mark.parametrize(
    ("period", "plan_lines", "occurrences", "expected_status"),
    [
        (
            "2026년",
            ["내역사업별 추진계획: 내역사업 없음"],
            [("plan:0", "내역사업 없음", "PROGRAM_LEVEL_ABSENT")],
            CplStatus.NOT_APPLICABLE,
        ),
        (
            "2026년 ~ 2028년",
            [
                "연차별 추진계획: 2026년 구축, 2027년 확산",
                "내역사업별 추진계획: 내역사업 없음",
            ],
            [
                ("plan:0", "2026년 구축, 2027년 확산", "ANNUAL_PLAN_CONTENT"),
                ("plan:1", "내역사업 없음", "PROGRAM_LEVEL_ABSENT"),
            ],
            CplStatus.PRESENT,
        ),
        (
            "2026년 ~ 2028년",
            [
                "연차별 추진계획: 2026년 구축, 2027년 확산",
                "내역사업별 추진계획: 내역사업",
            ],
            [
                ("plan:0", "2026년 구축, 2027년 확산", "ANNUAL_PLAN_CONTENT"),
                ("plan:1", "내역사업", "PROGRAM_LEVEL"),
            ],
            CplStatus.MISSING,
        ),
        (
            "2026년 ~ 2028년",
            [
                "연차별 추진계획: 2026년 구축, 2027년 확산",
                "추진계획: 세부사업",
            ],
            [
                ("plan:0", "2026년 구축, 2027년 확산", "ANNUAL_PLAN_CONTENT"),
                ("plan:1", "세부사업", "PROGRAM_LEVEL"),
            ],
            CplStatus.NEEDS_CONFIRMATION,
        ),
    ],
)
def test_implementation_plan_combines_annual_and_subprogram_states(
    period: str,
    plan_lines: list[str],
    occurrences: list[tuple[str, str, str]],
    expected_status: CplStatus,
) -> None:
    document = implementation_plan_document(period, plan_lines)
    rule_result = evaluate_cpl_rules(document)
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "IMPLEMENTATION_PLAN",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": evidence_ref,
                            "raw_text": raw_text,
                            "axis_code": axis_code,
                        }
                        for evidence_ref, raw_text, axis_code in occurrences
                    ],
                    "absent_axis_codes": [],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        document, response, {CplFieldCode.IMPLEMENTATION_PLAN}
    )
    result = merge_llm_result(rule_result, grounded)
    plan = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert plan.status == expected_status


def test_combined_plan_label_does_not_treat_standalone_not_applicable_as_hierarchy_absence() -> None:
    result = evaluate_cpl_rules(
        implementation_plan_document(
            "2026년",
            ["추진계획: 해당 없음"],
        )
    )
    plan = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert plan.status == CplStatus.NEEDS_CONFIRMATION
    assert plan.reason_code == "IMPLEMENTATION_PLAN_REVIEW_REQUIRED"


def test_openai_adapter_uses_responses_structured_output_contract() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        output = {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "MISSING",
                    "occurrences": [],
                    "absent_axis_codes": [],
                    "reason_code": "EXPLICIT_VALUE_NOT_FOUND",
                    "explanation": None,
                }
            ]
        }
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": json.dumps(output)}
                        ]
                    }
                ],
            },
        )

    client = OpenAILLMClient(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        model_profiles={"gpt-4o-mini": "gpt-4o-mini"},
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.generate_structured(
            task_name="cpl_semantic_evidence",
            messages=[Message(role="user", content="fixture")],
            response_schema=CplSemanticResponse,
            model_profile="gpt-4o-mini",
        )
    )
    assert isinstance(result, CplSemanticResponse)
    assert captured["model"] == "gpt-4o-mini"
    assert captured["store"] is False
    assert captured["text"]["format"]["type"] == "json_schema"
    assert captured["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (LLMUnavailableError("unavailable"), "LLM_UNAVAILABLE"),
        (LLMTimeoutError("timeout"), "LLM_TIMEOUT"),
        (LLMInvalidResponseError("invalid"), "LLM_INVALID_RESPONSE"),
    ],
)
def test_pipeline_maps_llm_failures_without_losing_rule_occurrences(
    error: Exception,
    reason_code: str,
) -> None:
    class FailingLlm:
        async def generate_structured(self, **_kwargs):
            raise error

    document = parsed_document()
    rule_result = evaluate_cpl_rules(document)
    semantic_fields = {
        item.field_code
        for item in rule_result.items
        if item.field_code
        in {
            CplFieldCode.PURPOSE_GOAL,
            CplFieldCode.IMPLEMENTATION_PLAN,
            CplFieldCode.NEW_OR_CHANGED_CONTENT,
            CplFieldCode.BUSINESS_NEED,
            CplFieldCode.TARGET_AND_CONDITIONS,
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
            CplFieldCode.DELIVERY_SYSTEM,
            CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
        }
        and item.status != CplStatus.PRESENT
    }
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        openai_api_key=None,
    )
    result = asyncio.run(
        _complete_semantic_review(
            document,
            rule_result,
            semantic_fields,
            FailingLlm(),
            settings,
        )
    )
    need = next(
        item for item in result.items if item.field_code == CplFieldCode.BUSINESS_NEED
    )
    assert need.reason_code == reason_code
    assert need.status == CplStatus.NEEDS_CONFIRMATION
    assert need.occurrences[0].raw_text.startswith("지역 기업")
    assert f"CPL semantic extraction incomplete: {reason_code}" in result.warnings


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def create_user(engine: Engine) -> Iterator[Callable[[str], int]]:
    user_ids: list[int] = []

    def create(login_id: str) -> int:
        with engine.begin() as connection:
            user_id = connection.scalar(
                text(
                    """
                    INSERT INTO sims.app_user (login_id, email, password_hash)
                    VALUES (:login_id, :email, :password_hash)
                    RETURNING id
                    """
                ),
                {
                    "login_id": login_id,
                    "email": f"{login_id}@example.com",
                    "password_hash": hash_password(PASSWORD),
                },
            )
        assert user_id is not None
        user_ids.append(user_id)
        return user_id

    yield create
    with engine.begin() as connection:
        for user_id in user_ids:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = :user_id"),
                {"user_id": user_id},
            )


class CplFakeParser:
    name = "cpl-fixture-parser"
    version = "1.0"

    def supports(self, mime_type: str, extension: str) -> bool:
        return mime_type == "application/hwp+zip" and extension == "hwpx"

    async def parse(self, source: FileSource) -> ParsedDocument:
        assert source.content.read(2) == b"PK"
        return parsed_document().model_copy(
            update={"parser_name": self.name, "parser_version": self.version}
        )


def hwpx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "mimetype", b"application/hwp+zip", compress_type=zipfile.ZIP_STORED
        )
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


def test_pipeline_persists_cpl_snapshot_and_all_occurrences(
    database_url: str,
    engine: Engine,
    tmp_path: Path,
    create_user: Callable[[str], int],
) -> None:
    from main import create_app

    login_id = "cpl-pipeline"
    create_user(login_id)
    settings = Settings(
        database_url=database_url,
        jwt_secret=JWT_SECRET,
        local_storage_root=tmp_path,
    )
    with TestClient(create_app(settings, CplFakeParser(), None, None)) as client:
        token = client.post(
            "/api/v1/auth/login",
            json={"login_id": login_id, "password": PASSWORD},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        uploaded = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        assert uploaded.status_code == 201
        case_id = uploaded.json()["case_id"]
        assert client.post(
            f"/api/v1/cases/{case_id}/analyze", headers=headers
        ).status_code == 202

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with engine.connect() as connection:
                extraction_id = connection.scalar(
                    text(
                        "SELECT id FROM sims.request_extraction "
                        "WHERE inspection_case_id = :case_id"
                    ),
                    {"case_id": case_id},
                )
            if extraction_id is not None:
                break
            time.sleep(0.01)
        else:
            with engine.connect() as connection:
                diagnostic = connection.execute(
                    text(
                        """
                        SELECT c.status, c.failure_code, r.status AS parse_status,
                               r.error_code AS parse_error_code
                        FROM sims.inspection_case c
                        JOIN sims.uploaded_document d ON d.inspection_case_id = c.id
                        JOIN sims.document_parse_run r ON r.file_asset_id = d.file_asset_id
                        WHERE c.id = :case_id
                        """
                    ),
                    {"case_id": case_id},
                ).mappings().one()
            pytest.fail(f"CPL extraction was not persisted: {dict(diagnostic)}")

    with engine.connect() as connection:
        extraction = connection.execute(
            text(
                """
                SELECT e.request_reason, e.status, e.raw_extraction,
                       c.status AS case_status, c.failure_code
                FROM sims.request_extraction e
                JOIN sims.inspection_case c ON c.id = e.inspection_case_id
                WHERE e.inspection_case_id = :case_id
                """
            ),
            {"case_id": case_id},
        ).mappings().one()
        purpose = connection.execute(
            text(
                """
                SELECT v.raw_text, v.page_no, v.normalized_value, v.source_locator
                FROM sims.request_field_value v
                JOIN sims.form_field_definition f ON f.id = v.field_definition_id
                WHERE v.request_extraction_id = :extraction_id
                  AND f.field_code = 'PURPOSE_GOAL'
                """
            ),
            {"extraction_id": extraction_id},
        ).mappings().one()
        items = connection.execute(
            text(
                """
                SELECT f.field_code, i.result_status, i.reason_code,
                       i.evidence_field_value_id
                FROM sims.missing_check_item i
                JOIN sims.form_field_definition f ON f.id = i.field_definition_id
                JOIN sims.missing_check_run r ON r.id = i.missing_check_run_id
                WHERE r.inspection_case_id = :case_id
                ORDER BY f.display_order
                """
            ),
            {"case_id": case_id},
        ).mappings().all()

    snapshot = extraction["raw_extraction"]
    assert extraction["request_reason"] == "DETAIL_NEW"
    assert extraction["status"] == "SUCCESS"
    assert extraction["case_status"] == "FAILED"
    assert extraction["failure_code"] == "RETRIEVAL_UNAVAILABLE"
    assert len(snapshot["items"]) == 13
    assert snapshot["confirmed_count"] == 3
    assert snapshot["total_count"] == 13
    assert snapshot["confirmation_rate"] == 3 / 13 * 100
    assert snapshot["model_profile"] == "gpt-4o-mini"
    assert "CPL semantic extraction incomplete: LLM_UNAVAILABLE" in snapshot[
        "warnings"
    ]

    occurrences = purpose["normalized_value"]["occurrences"]
    assert purpose["raw_text"] is None
    assert purpose["page_no"] is None
    assert len(occurrences) == 2
    assert {item["page_no"] for item in occurrences} == {2, 3}
    assert all(
        {
            "raw_text",
            "normalized_value",
            "page_no",
            "section_path",
            "source_locator",
            "block_id",
            "extraction_method",
        }.issubset(item)
        for item in occurrences
    )
    lineage = purpose["source_locator"]["occurrences"]
    assert len(lineage) == 2
    assert all("axis_code" in item and "source_role" in item for item in lineage)
    assert [item["field_code"] for item in items] == [
        field.value for field in CPL_FIELDS
    ]
    unresolved = next(
        item for item in items if item["field_code"] == "IMPLEMENTATION_PLAN"
    )
    assert unresolved["result_status"] == "NEEDS_CONFIRMATION"
    assert unresolved["reason_code"] == "LLM_UNAVAILABLE"
    assert unresolved["evidence_field_value_id"] is None


def paragraph_document(lines: list[str]) -> ParsedDocument:
    """문단만으로 이루어진 최소 문서를 만든다."""
    return ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="\n".join(lines),
        blocks=[
            DocumentBlock(
                block_id=f"body:{index}",
                block_type="paragraph",
                text=line,
                section_path=[],
                source_locator={"body_block_index": index},
            )
            for index, line in enumerate(lines)
        ],
    )


def occurrences_for(result: CplResult, field_code: CplFieldCode) -> list:
    return next(
        item.occurrences for item in result.items if item.field_code == field_code
    )


def test_rules_read_labels_wrapped_in_parentheses() -> None:
    """안내자료의 작성 우수 사례는 "○ (지원근거) ..." 처럼 라벨을 괄호로 감싼다."""
    result = evaluate_cpl_rules(
        paragraph_document(["○ (지원근거) 「중소기업기본법」 제2조"])
    )

    occurrences = occurrences_for(result, CplFieldCode.LEGAL_BASIS)
    assert len(occurrences) == 1
    # 닫는 괄호가 값에 남으면 안 된다.
    assert occurrences[0].raw_text.startswith("「중소기업기본법」")


def test_rules_still_read_colon_separated_labels() -> None:
    """작성 미흡 사례가 쓰는 "라벨 : 값" 표기는 그대로 통과해야 한다."""
    result = evaluate_cpl_rules(
        paragraph_document(["○ 지원근거 : 「중소기업기본법」 제2조"])
    )

    occurrences = occurrences_for(result, CplFieldCode.LEGAL_BASIS)
    assert len(occurrences) == 1
    assert occurrences[0].raw_text.startswith("「중소기업기본법」")


def test_rules_ignore_parenthesised_text_that_is_not_a_label() -> None:
    result = evaluate_cpl_rules(
        paragraph_document(["○ (참고) 상세 내용은 붙임 자료를 확인한다"])
    )

    for item in result.items:
        assert item.occurrences == []


@pytest.mark.parametrize("area_label", ["사전협의 요청사유", "사전협의 요청유형"])
def test_request_type_area_accepts_both_form_and_criteria_labels(
    area_label: str,
) -> None:
    """서식 [서식 1]은 "요청사유", CPL 판별기준 2절은 "요청유형"으로 부른다."""
    result = evaluate_cpl_rules(
        paragraph_document([area_label, "■ 세부사업 신설  □ 내역사업 신설"])
    )

    item = next(
        item for item in result.items if item.field_code == CplFieldCode.REQUEST_TYPE
    )
    assert item.status == CplStatus.PRESENT
