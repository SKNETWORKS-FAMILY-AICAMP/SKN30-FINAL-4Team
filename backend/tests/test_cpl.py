import asyncio
import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
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
from app.schemas.fit import (
    FitInputFeedback,
    FitInputFeedbackReason,
    FitRelationId,
)
from app.schemas.parsed_document import DocumentBlock, ParsedDocument
from app.services.analysis_pipeline import (
    _complete_semantic_review,
    _semantic_messages,
    load_cpl_prompt,
)
from app.services.cpl.logic_validator import (
    CPL_SEMANTIC_FIELDS,
    evaluate_cpl_rules,
    ground_llm_response,
    merge_llm_result,
    semantic_fragments,
    _normalize_axis_value,
    _same_contained_fact,
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
    # 파서가 준 위치는 그대로 오고, 서버가 계산한 인용 좌표가 더해진다.
    assert occurrence.source_locator["paragraph_index"] == 4
    assert occurrence.source_locator["span_start"] >= 0
    assert occurrence.source_locator["line_index"] >= 0
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
    assert all(
        fragment.field_codes == {CplFieldCode.SUPPORT_CONTENT_AND_SCALE}
        for fragment in values
    )

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
    assert fragments[0].field_codes == {CplFieldCode.PURPOSE_GOAL}
    assert fragments[1].field_codes == {CplFieldCode.BUSINESS_NEED}
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


def test_unlabeled_inline_segments_keep_the_preceding_label_ownership() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="inline continuation fixture",
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
                            "text": (
                                "○ 지원내용 :"
                                "- 온라인 마케팅 지원"
                                "- 팝업스토어 참가 지원"
                                "○ 사업예산 : 1억원"
                                "○ 수행기관 : 전담기관"
                            ),
                            "segments": [
                                {"segment_index": 0, "text": "○ 지원내용 :"},
                                {
                                    "segment_index": 1,
                                    "text": "- 온라인 마케팅 지원",
                                },
                                {
                                    "segment_index": 2,
                                    "text": "- 팝업스토어 참가 지원",
                                },
                                {
                                    "segment_index": 3,
                                    "text": "○ 사업예산 : 1억원",
                                },
                                {
                                    "segment_index": 4,
                                    "text": "○ 수행기관 : 전담기관",
                                },
                            ],
                            "structure_status": "unresolved",
                        },
                        {
                            "row": 1,
                            "col": 0,
                            "text": "기대효과",
                        },
                        {
                            "row": 1,
                            "col": 1,
                            "text": "○ 파급효과 : 매출 증가○ 성과지표 : 지원 기업 수",
                            "segments": [
                                {
                                    "segment_index": 0,
                                    "text": "○ 파급효과 : 매출 증가",
                                },
                                {
                                    "segment_index": 1,
                                    "text": "○ 성과지표 : 지원 기업 수",
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
    by_text = {fragment.text: fragment for fragment in fragments}

    for text in ["- 온라인 마케팅 지원", "- 팝업스토어 참가 지원"]:
        assert by_text[text].field_codes == {
            CplFieldCode.SUPPORT_CONTENT_AND_SCALE
        }
        assert by_text[text].source_role == CplSourceRole.SUPPORT_CONTENT
    assert by_text["○ 사업예산 : 1억원"].field_codes == set()
    assert by_text["○ 수행기관 : 전담기관"].field_codes == {
        CplFieldCode.DELIVERY_SYSTEM
    }
    assert (
        by_text["○ 성과지표 : 지원 기업 수"].source_role
        == CplSourceRole.PERFORMANCE_INDICATOR
    )


def test_semantic_grounding_rejects_evidence_owned_by_another_cpl_field() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "need:1",
                            "raw_text": "지역 기업의 설비 노후화가 심각함",
                            "axis_code": "PURPOSE_PROBLEM_DOMAIN",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    grounded = ground_llm_response(
        parsed_document(), response, {CplFieldCode.PURPOSE_GOAL}
    )

    assert grounded[0].occurrences == []
    assert grounded[0].status == CplStatus.NEEDS_CONFIRMATION
    assert grounded[0].reason_code == "LLM_INVALID_RESPONSE"


def test_invalid_or_missing_cpl_items_do_not_discard_valid_siblings() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "purpose:1",
                            "raw_text": "지역 기업의 디지털 전환 지원",
                            "axis_code": "PURPOSE_SPECIFIC_OBJECTIVE",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                },
                {
                    "field_code": "BUSINESS_NEED",
                    "status": "NEEDS_CONFIRMATION",
                    "occurrences": [],
                    "reason_code": None,
                    "explanation": None,
                },
            ]
        }
    )

    grounded = ground_llm_response(
        parsed_document(),
        response,
        {
            CplFieldCode.PURPOSE_GOAL,
            CplFieldCode.BUSINESS_NEED,
            CplFieldCode.NEW_OR_CHANGED_CONTENT,
        },
    )
    by_code = {item.field_code: item for item in grounded}

    assert by_code[CplFieldCode.PURPOSE_GOAL].status == CplStatus.PRESENT
    assert [
        occurrence.axis_code
        for occurrence in by_code[CplFieldCode.PURPOSE_GOAL].occurrences
    ] == [CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE]
    # 이유코드만 빠진 것은 모델이 원문에 없는 말을 지어낸 것과 다른 사건이다.
    assert by_code[CplFieldCode.BUSINESS_NEED].reason_code == (
        "LLM_REASON_CODE_MISSING"
    )
    assert by_code[CplFieldCode.NEW_OR_CHANGED_CONTENT].reason_code == (
        "LLM_INVALID_RESPONSE"
    )


def test_explicit_missing_marker_is_not_cpl_or_fit_evidence() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="수행기관 미기재",
        blocks=[
            DocumentBlock(
                block_id="delivery:blank",
                block_type="paragraph",
                text="수행기관: (공란 / 미기재)",
            )
        ],
    )
    rule_result = evaluate_cpl_rules(document)
    delivery = next(
        item
        for item in rule_result.items
        if item.field_code == CplFieldCode.DELIVERY_SYSTEM
    )
    assert delivery.occurrences == []

    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "DELIVERY_SYSTEM",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "delivery:blank",
                            "raw_text": "(공란 / 미기재)",
                            "axis_code": "DELIVERY_ORG_NAME",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    grounded = ground_llm_response(
        document, response, {CplFieldCode.DELIVERY_SYSTEM}
    )
    assert grounded[0].occurrences == []
    assert grounded[0].reason_code == "LLM_INVALID_RESPONSE"


def test_filtering_a_blank_marker_does_not_fail_the_whole_cpl_field() -> None:
    """공란 근거를 걸러낸 것은 LLM 실패가 아니다 — FIT 재검 신호로 쓰지 않는다."""
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="사업목적",
        blocks=[
            DocumentBlock(
                block_id="purpose:0",
                block_type="paragraph",
                text="사업목적: 중소기업의 해외시장 진출 확대 (세부목표: 공란)",
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
                            "evidence_ref": "purpose:0",
                            "raw_text": "해외시장 진출 확대",
                            "axis_code": "PURPOSE_DIRECTION",
                        },
                        {
                            "evidence_ref": "purpose:0",
                            "raw_text": "공란",
                            "axis_code": "PURPOSE_DIRECTION",
                        },
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    item = ground_llm_response(
        document, response, {CplFieldCode.PURPOSE_GOAL}
    )[0]

    assert item.status == CplStatus.PRESENT
    assert item.reason_code is None
    assert [occurrence.raw_text for occurrence in item.occurrences] == [
        "해외시장 진출 확대"
    ]


def test_missing_word_inside_real_explanation_is_not_treated_as_blank() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="수행기관 설명",
        blocks=[
            DocumentBlock(
                block_id="delivery:instruction",
                block_type="paragraph",
                text="수행기관: 기관명 미기재 시 신청할 수 없음",
            )
        ],
    )

    result = evaluate_cpl_rules(document)
    delivery = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.DELIVERY_SYSTEM
    )

    assert [occurrence.raw_text for occurrence in delivery.occurrences] == [
        "기관명 미기재 시 신청할 수 없음"
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
                    "지원내용: 기업당 최대 3천만원, 100개사, 보조율 50%\n"
                    "성과목표: 목표값 30%, 기준연도 2026년"
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
    assert values[CplAxisCode.KPI_TARGET_VALUE] == {"number": 30, "unit": "PERCENT"}
    assert values[CplAxisCode.KPI_BASE_YEAR] == {"year": 2026, "unit": "YEAR"}


def test_unattributable_evidence_is_not_recorded_as_an_llm_failure() -> None:
    """조각 귀속 실패와 모델의 지어내기를 같은 코드로 적지 않는다.

    파서가 라벨 여러 개를 한 조각에 담아 넘기면 인용 구간의 소속 필드를
    정할 수 없다. 이것은 우리 구조의 한계이지 모델이 틀린 것이 아니다.
    같은 코드로 적으면 Rule·LLM 비교 지표가 우리 결함을 LLM 실패로 센다.
    """

    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="다중 라벨 셀 fixture",
        blocks=[
            DocumentBlock(
                block_id="mixed:1",
                block_type="paragraph",
                text="○ 사업목적 : 지역 산업 고도화\n○ 사업필요성 : 전환 지연",
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
                            "evidence_ref": "mixed:1",
                            # 한 줄에 담기지 않는 구간이라 소속 필드를 정할 수 없다.
                            "raw_text": "고도화\n○ 사업필요성",
                            "axis_code": "PURPOSE_DIRECTION",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    item = next(
        entry
        for entry in ground_llm_response(
            document, response, {CplFieldCode.PURPOSE_GOAL}
        )
        if entry.field_code == CplFieldCode.PURPOSE_GOAL
    )

    assert item.occurrences == []
    assert item.reason_code == "EVIDENCE_OWNERSHIP_UNRESOLVED"


def test_condition_sublabels_decide_the_axis() -> None:
    """하위 라벨이 축을 정한다. 값을 보고 추측하지 않는다.

    "- 지역:" 뒤에 무엇이 오든 그것은 지역 조건이다. 판별기준 11.2 가 나열한
    조건 항목이 라벨로 구분돼 있으면 Rule 이 축을 붙일 수 있다.
    """

    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원조건 fixture",
        blocks=[
            DocumentBlock(
                block_id="cond:1",
                block_type="paragraph",
                text=(
                    "○ 지원조건\n"
                    "- 지역: 대전광역시 내 본사 보유\n"
                    "- 업종: 한국표준산업분류 C(제조업)\n"
                    "- 업력: 업력 3년 이상 10년 이내\n"
                    "- 인증 여부: 벤처기업 인증\n"
                    "- 인증: 직접생산확인\n"
                    "- 기타 조건: 국세 및 지방세 완납\n"
                    "- 기타: 신청기업의 국세 완납\n"
                    "- 제외조건: 휴·폐업 기업"
                ),
            )
        ],
    )
    item = next(
        entry
        for entry in evaluate_cpl_rules(document).items
        if entry.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )
    axes: dict[CplAxisCode, list[str]] = {}
    for occurrence in item.occurrences:
        if occurrence.axis_code is not None:
            axes.setdefault(occurrence.axis_code, []).append(occurrence.raw_text)
    assert axes[CplAxisCode.COND_REGION] == ["대전광역시 내 본사 보유"]
    assert axes[CplAxisCode.COND_INDUSTRY] == ["한국표준산업분류 C(제조업)"]
    assert axes[CplAxisCode.COND_CERTIFICATION] == [
        "벤처기업 인증",
        "직접생산확인",
    ]
    assert axes[CplAxisCode.COND_OTHER] == [
        "국세 및 지방세 완납",
        "신청기업의 국세 완납",
    ]
    assert axes[CplAxisCode.COND_EXCLUSION] == ["휴·폐업 기업"]
    # 업력은 정식 하위 라벨 축이며 기존 정량 정규화 경로가 값을 처리한다.
    assert axes[CplAxisCode.COND_BUSINESS_AGE] == ["업력 3년 이상 10년 이내"]


def test_condition_sublabel_does_not_consume_unknown_label_on_next_line() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원조건 fixture",
        blocks=[
            DocumentBlock(
                block_id="cond:unknown-next-line",
                block_type="paragraph",
                text=(
                    "○ 지원조건\n"
                    "- 지역: 대전광역시\n"
                    "- 알 수 없는 하위 라벨: 제조업"
                ),
            )
        ],
    )

    item = next(
        item
        for item in evaluate_cpl_rules(document).items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )

    region_occurrences = [
        occurrence
        for occurrence in item.occurrences
        if occurrence.axis_code == CplAxisCode.COND_REGION
    ]
    assert [occurrence.raw_text for occurrence in region_occurrences] == [
        "대전광역시"
    ]


def test_condition_sublabels_split_known_labels_on_the_same_line() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원조건 fixture",
        blocks=[
            DocumentBlock(
                block_id="cond:same-line-labels",
                block_type="paragraph",
                text="○ 지원조건\n- 지역: 대전광역시, 업종: 제조업",
            )
        ],
    )

    item = next(
        item
        for item in evaluate_cpl_rules(document).items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )

    by_axis: dict[CplAxisCode, list[str]] = {}
    for occurrence in item.occurrences:
        if occurrence.axis_code is not None:
            by_axis.setdefault(occurrence.axis_code, []).append(occurrence.raw_text)

    assert by_axis[CplAxisCode.COND_REGION] == ["대전광역시"]
    assert by_axis[CplAxisCode.COND_INDUSTRY] == ["제조업"]


def test_condition_sublabels_include_quantitative_axes() -> None:
    result = evaluate_cpl_rules(
        paragraph_document(
            [
                "지원조건\n"
                "- 업력: 3년 이상\n"
                "- 매출액: 최근 3개년 연평균 매출액 50억원 이상\n"
                "- 종사자 수: 10명 이하"
            ]
        )
    )
    occurrences = occurrences_for(result, CplFieldCode.TARGET_AND_CONDITIONS)
    by_axis = {
        occurrence.axis_code: occurrence
        for occurrence in occurrences
        if occurrence.axis_code in {
            CplAxisCode.COND_BUSINESS_AGE,
            CplAxisCode.COND_REVENUE,
            CplAxisCode.COND_HEADCOUNT,
        }
    }

    assert by_axis[CplAxisCode.COND_BUSINESS_AGE].normalized_value == {
        "years": 3,
        "operator": "GTE",
        "unit": "YEAR",
    }
    assert by_axis[CplAxisCode.COND_REVENUE].normalized_value == {
        "amount_won": 5_000_000_000,
        "operator": "GTE",
        "unit": "KRW",
        "period_years": 3,
    }
    assert by_axis[CplAxisCode.COND_HEADCOUNT].normalized_value == {
        "count": 10,
        "operator": "LTE",
        "unit": "PERSON",
    }


@pytest.mark.parametrize(
    ("condition", "expected_headcount"),
    [
        ("종사자 수: 10명 이하", True),
        ("종사자 수: 10인 이하", True),
        ("상시 근로자 10인 이하", True),
        ("기타 조건: 10인 이하", False),
    ],
)
def test_headcount_in_requires_label_or_employee_context(
    condition: str,
    expected_headcount: bool,
) -> None:
    result = evaluate_cpl_rules(
        paragraph_document([f"지원조건\n- {condition}"])
    )
    occurrences = occurrences_for(result, CplFieldCode.TARGET_AND_CONDITIONS)
    headcounts = [
        occurrence
        for occurrence in occurrences
        if occurrence.axis_code == CplAxisCode.COND_HEADCOUNT
    ]

    assert bool(headcounts) is expected_headcount
    if expected_headcount:
        assert headcounts[0].normalized_value["unit"] == "PERSON"


def test_grounded_evidence_carries_its_position_in_the_fragment() -> None:
    """같은 사실끼리 묶으려면 어디서 나왔는지가 남아 있어야 한다.

    지표 하나가 이름·목표값·기준연도를 갖는 구조를 지금 계약은 표현하지
    못한다. 묶음 규칙을 정하기 전에 좌표부터 보존한다. 좌표는 줄 번호와
    오프셋이라 판단이 필요 없어 LLM 이 아니라 서버가 계산한다.
    """

    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="성과지표 fixture",
        blocks=[
            DocumentBlock(
                block_id="effect:1",
                block_type="paragraph",
                text=(
                    "○ 성과지표 : 지표 목록\n"
                    "- 사업화 성공률: 목표값 30%\n"
                    "- 신규 고용창출: 목표값 240명"
                ),
            )
        ],
    )
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "EXPECTED_EFFECTS_AND_PERFORMANCE",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "effect:1",
                            "raw_text": "사업화 성공률",
                            "axis_code": "KPI_NAME",
                        },
                        {
                            "evidence_ref": "effect:1",
                            "raw_text": "목표값 30%",
                            "axis_code": "KPI_TARGET_VALUE",
                        },
                        {
                            "evidence_ref": "effect:1",
                            "raw_text": "신규 고용창출",
                            "axis_code": "KPI_NAME",
                        },
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    item = next(
        entry
        for entry in ground_llm_response(
            document, response, {CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE}
        )
        if entry.field_code == CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE
    )
    lines = {
        occurrence.raw_text: occurrence.source_locator.get("line_index")
        for occurrence in item.occurrences
    }
    # 같은 지표의 이름과 목표값은 같은 줄에서 나온다.
    assert lines["사업화 성공률"] == lines["목표값 30%"]
    assert lines["신규 고용창출"] != lines["사업화 성공률"]
    assert all(
        occurrence.source_locator.get("span_start") is not None
        for occurrence in item.occurrences
    )


def test_label_prefixed_duplicate_is_treated_as_one_fact() -> None:
    """라벨을 포함해 인용해도 같은 사실이면 하나만 남는다.

    Rule 은 라벨 뒤 값만 보고 LLM 은 라벨을 포함해 인용할 수 있다. 정규값이
    원문을 그대로 담는 축에서는 두 건이 서로 달라 보여 중복이 남았다.
    """

    def occurrence(
        raw_text: str,
        method: str,
        *,
        with_coordinates: bool = True,
    ) -> CplOccurrence:
        source_locator = {"paragraph_index": 1}
        if with_coordinates:
            span_start = 0 if method == "LLM" else 6
            source_locator.update(
                {
                    "line_index": 0,
                    "span_start": span_start,
                    "span_end": span_start + len(raw_text),
                }
            )
        return CplOccurrence(
            raw_text=raw_text,
            normalized_value={"text": raw_text},
            axis_code=CplAxisCode.DELIVERY_METHOD_TYPE,
            source_role=CplSourceRole.DELIVERY_METHOD,
            page_no=1,
            section_path=[],
            source_locator=source_locator,
            block_id="delivery:1",
            extraction_method=method,
        )

    rule = occurrence("시 출연기관 위탁(보조)", "RULE")
    llm = occurrence("수행방식: 시 출연기관 위탁(보조)", "LLM")
    assert _same_contained_fact(rule, llm)

    # 좌표가 없으면 위치를 확정할 수 없어 중복으로 합치지 않는다.
    assert not _same_contained_fact(
        rule,
        occurrence("수행방식: 시 출연기관 위탁(보조)", "LLM", with_coordinates=False),
    )

    # 값이 다르면 두 건 모두 남는다.
    assert not _same_contained_fact(rule, occurrence("시 직접 수행", "LLM"))


def test_rule_and_label_prefixed_llm_occurrences_merge_by_overlapping_spans() -> None:
    full_text = "수행방식: 시 출연기관 위탁(보조)"
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text=full_text,
        blocks=[
            DocumentBlock(
                block_id="delivery:1",
                block_type="paragraph",
                text=full_text,
                page_no=1,
                source_locator={"paragraph_index": 1},
            )
        ],
    )
    rule_result = evaluate_cpl_rules(document)
    delivery = next(
        item
        for item in rule_result.items
        if item.field_code == CplFieldCode.DELIVERY_SYSTEM
    )
    rule_occurrence = next(
        occurrence
        for occurrence in delivery.occurrences
        if occurrence.axis_code == CplAxisCode.DELIVERY_METHOD_TYPE
    )
    assert rule_occurrence.source_locator["line_index"] == 0
    assert rule_occurrence.source_locator["span_start"] > 0

    candidate = CplItem(
        field_code=CplFieldCode.DELIVERY_SYSTEM,
        status=CplStatus.PRESENT,
        occurrences=[
            CplOccurrence(
                raw_text=full_text,
                normalized_value={"text": full_text},
                axis_code=CplAxisCode.DELIVERY_METHOD_TYPE,
                source_role=CplSourceRole.DELIVERY_METHOD,
                page_no=1,
                source_locator={
                    "paragraph_index": 1,
                    "line_index": 0,
                    "span_start": 0,
                    "span_end": len(full_text),
                },
                block_id="delivery:1",
                extraction_method="LLM",
            )
        ],
    )

    merged = merge_llm_result(rule_result, [candidate])
    occurrences = next(
        item
        for item in merged.items
        if item.field_code == CplFieldCode.DELIVERY_SYSTEM
    ).occurrences
    assert occurrences == [rule_occurrence]


def test_kpi_target_value_reads_scale_words_and_company_units() -> None:
    """성과지표 목표값은 배수어와 단위를 원문대로 읽어야 한다.

    `10만건` 을 `10` 으로 기록하면 실패가 아니라 틀린 값을 저장한다.
    `20개사` 를 단위 없는 수로 기록하면 지원기업수와 구분되지 않는다.
    """

    cases = [
        ("목표값 30%", {"number": 30, "unit": "PERCENT"}),
        ("목표값 240명", {"number": 240, "unit": "PERSON"}),
        ("목표값 90건", {"number": 90, "unit": "CASE"}),
        ("마케팅 지원 기업 수(20개사)", {"number": 20, "unit": "COMPANY"}),
        ("SNS 브랜드 노출 수(10만건)", {"number": 100000, "unit": "CASE"}),
        ("수출액 3억원", {"number": 300000000, "unit": "KRW"}),
    ]
    for raw_text, expected in cases:
        assert _normalize_axis_value(
            CplAxisCode.KPI_TARGET_VALUE, raw_text, None
        ) == expected, raw_text


def test_llm_cannot_ground_evidence_on_a_line_the_document_marks_blank() -> None:
    """문서가 스스로 공란이라고 밝힌 자리는 라벨을 붙여 인용해도 근거가 아니다.

    Rule 은 라벨 뒤 값만 보므로 원래 걸러냈지만, LLM 은 `○ 수행기관 : 미기재`
    처럼 라벨을 포함한 줄을 인용할 수 있어 같은 자리가 근거로 남았다.
    """

    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="수행기관 fixture",
        blocks=[
            DocumentBlock(
                block_id="delivery:1",
                block_type="paragraph",
                text="○ 수행기관 : 미기재",
            )
        ],
    )
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "DELIVERY_SYSTEM",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "delivery:1",
                            "raw_text": "○ 수행기관 : 미기재",
                            "axis_code": "DELIVERY_ORG_NAME",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    grounded = ground_llm_response(
        document,
        response,
        {CplFieldCode.DELIVERY_SYSTEM},
    )
    item = next(
        entry
        for entry in grounded
        if entry.field_code == CplFieldCode.DELIVERY_SYSTEM
    )

    assert item.occurrences == []
    assert item.status != CplStatus.PRESENT


def test_labeled_support_values_leave_axis_assignment_to_llm() -> None:
    """지원 금액·비율의 축은 문맥 판단이라 Rule 이 정하지 않는다.

    같은 `3천만원` 이 기업당 한도일 수도 총 규모일 수도 있어 단위만으로는
    축이 정해지지 않는다. Rule 은 근거와 구역만 남기고, 축은 LLM 이 접지한
    뒤 `_normalize_axis_value` 가 값을 정규화한다.
    """

    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원내용 fixture",
        blocks=[
            DocumentBlock(
                block_id="support:1",
                block_type="paragraph",
                text="지원내용: 1개사 최대 5,000만원, 자기부담 30%",
            )
        ],
    )

    result = evaluate_cpl_rules(document)
    support = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.SUPPORT_CONTENT_AND_SCALE
    )

    assert [occurrence.raw_text for occurrence in support.occurrences] == [
        "1개사 최대 5,000만원, 자기부담 30%"
    ]
    # `1개사` 를 기업 수로 읽어 금액을 잃어버리던 오분류가 사라진다.
    assert all(occurrence.axis_code is None for occurrence in support.occurrences)
    assert all(
        occurrence.source_role == CplSourceRole.SUPPORT_CONTENT
        for occurrence in support.occurrences
    )

def test_numeric_target_conditions_are_structured_by_rule() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원조건 fixture",
        blocks=[
            DocumentBlock(
                block_id="condition:1",
                block_type="paragraph",
                text=(
                    "지원조건: 업력 3년 미만, 최근 3개년 연평균 매출액 "
                    "50억원 이상, 상시근로자 10명 이하"
                ),
            )
        ],
    )

    result = evaluate_cpl_rules(document)
    target = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )
    by_axis = {
        occurrence.axis_code: occurrence
        for occurrence in target.occurrences
        if occurrence.axis_code is not None
    }

    assert set(by_axis) == {
        CplAxisCode.COND_BUSINESS_AGE,
        CplAxisCode.COND_REVENUE,
        CplAxisCode.COND_HEADCOUNT,
    }
    assert all(
        occurrence.source_role == CplSourceRole.CONDITION
        for occurrence in by_axis.values()
    )
    assert by_axis[CplAxisCode.COND_BUSINESS_AGE].normalized_value == {
        "years": 3,
        "operator": "LT",
        "unit": "YEAR",
    }
    assert by_axis[CplAxisCode.COND_REVENUE].normalized_value == {
        "amount_won": 5_000_000_000,
        "operator": "GTE",
        "unit": "KRW",
        "period_years": 3,
    }
    assert by_axis[CplAxisCode.COND_HEADCOUNT].normalized_value == {
        "count": 10,
        "operator": "LTE",
        "unit": "PERSON",
    }


def test_linked_policy_requires_semantic_identifier_and_preserves_absence() -> None:
    document = paragraph_document(
        [
            "연계정책: 관련 정책에 따라 추진",
            "지원조건: 없음",
        ]
    )

    result = evaluate_cpl_rules(document)
    linked = next(
        item for item in result.items if item.field_code == CplFieldCode.LINKED_POLICY
    )
    target = next(
        item
        for item in result.items
        if item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
    )

    assert linked.status == CplStatus.NEEDS_CONFIRMATION
    assert linked.occurrences[0].normalized_value == {
        "text": "관련 정책에 따라 추진"
    }
    assert len(target.occurrences) == 1
    assert target.occurrences[0].raw_text == "없음"
    assert target.occurrences[0].axis_code is None
    assert target.occurrences[0].normalized_value == {"explicit_absence": True}


def test_linked_policy_identifier_is_grounded_without_rule_invention() -> None:
    document = paragraph_document(["연계정책: 중소기업 육성 종합계획에 따라 추진"])
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "LINKED_POLICY",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "body:0",
                            "raw_text": "중소기업 육성 종합계획",
                            "axis_code": "LINKED_POLICY_IDENTIFIER",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    item = ground_llm_response(
        document,
        response,
        {CplFieldCode.LINKED_POLICY},
    )[0]

    assert item.status == CplStatus.PRESENT
    assert item.occurrences[0].normalized_value == {
        "policy_identifier": "중소기업 육성 종합계획"
    }


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
    assert item.reason_code == "LLM_INVALID_RESPONSE"


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
    assert item.status == CplStatus.NEEDS_CONFIRMATION
    assert item.reason_code == "LLM_INVALID_RESPONSE"
    assert [o.raw_text for o in item.occurrences] == ["지역 기업"]

    merged = merge_llm_result(evaluate_cpl_rules(parsed_document()), grounded)
    assert "CPL semantic extraction incomplete: LLM_INVALID_RESPONSE" in (
        merged.warnings
    )


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
        cpl_prompt_version="cpl-semantic-v0.7",
        cpl_prompt_path="backend/config/prompts/cpl-v0.7.txt",
    )
    prompt = load_cpl_prompt(settings.cpl_prompt_path)
    assert "raw_text must be a verbatim" in prompt
    assert "PRESENT when at least one requested axis has explicit evidence" in prompt
    assert "A BUSINESS_NEED fragment is not PURPOSE_GOAL evidence" in prompt
    assert "A BUSINESS_NEED fragment is not PURPOSE_GOAL evidence" in prompt
    assert settings.cpl_prompt_path.name == "cpl-v0.7.txt"
    messages = _semantic_messages(
        table_document(),
        {CplFieldCode.SUPPORT_CONTENT_AND_SCALE},
        prompt,
    )
    payload = json.loads(messages[1].content)
    assert payload["allowed_axes"]["SUPPORT_CONTENT_AND_SCALE"]
    assert payload["fragments"][0]["evidence_ref"] == "table:1:cell:0:0"
    assert "blocks" not in payload


def test_default_cpl_prompt_requires_axis_audit_and_minimum_spans() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret=JWT_SECRET,
        cpl_prompt_version="cpl-semantic-v0.9",
        cpl_prompt_path="backend/config/prompts/cpl-v0.9.txt",
    )
    prompt = load_cpl_prompt(settings.cpl_prompt_path)
    assert settings.cpl_prompt_path.name == "cpl-v0.9.txt"
    assert "audit the allowed-axis list again" in prompt
    assert "smallest complete phrase" in prompt
    assert "every named step separately" in prompt
    assert "have a non-empty reason_code" in prompt


def test_fit_recheck_messages_keep_required_axis_and_role_constraints() -> None:
    feedback = FitInputFeedback(
        relation_id=FitRelationId.FIT_2,
        side="left",
        field_code=CplFieldCode.PURPOSE_GOAL,
        reason_code=FitInputFeedbackReason.REQUIRED_AXIS_MISSING,
        required_axis_codes=[CplAxisCode.PURPOSE_DIRECTION],
        required_source_roles=[None],
    )

    messages = _semantic_messages(
        parsed_document(),
        {CplFieldCode.PURPOSE_GOAL},
        "prompt",
        fit_feedback=(feedback,),
    )
    payload = json.loads(messages[1].content)

    assert payload["allowed_axes"] == {
        "PURPOSE_GOAL": ["PURPOSE_DIRECTION"]
    }
    assert payload["fit_input_feedback"] == [feedback.model_dump(mode="json")]
    by_ref = {item["evidence_ref"]: item for item in payload["fragments"]}
    assert by_ref["purpose:1"]["field_codes"] == ["PURPOSE_GOAL"]
    assert "need:1" not in by_ref


def test_fit_recheck_grounding_keeps_field_valid_axes_outside_prompt_focus() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "PURPOSE_GOAL",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "purpose:1",
                            "raw_text": "디지털 전환",
                            "axis_code": "PURPOSE_SPECIFIC_OBJECTIVE",
                        },
                        {
                            "evidence_ref": "purpose:1",
                            "raw_text": "지원",
                            "axis_code": "PURPOSE_DIRECTION",
                        },
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    grounded = ground_llm_response(
        parsed_document(),
        response,
        {CplFieldCode.PURPOSE_GOAL},
    )

    assert {
        occurrence.axis_code for occurrence in grounded[0].occurrences
    } == {
        CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
        CplAxisCode.PURPOSE_DIRECTION,
    }
    assert grounded[0].status == CplStatus.PRESENT
    assert grounded[0].reason_code is None


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


def test_merge_collapses_only_contained_rule_llm_occurrences_for_the_same_fact() -> None:
    statuses = [CplStatus.MISSING] * 13
    field_code = CplFieldCode.SUPPORT_CONTENT_AND_SCALE
    statuses[CPL_FIELDS.index(field_code)] = CplStatus.PRESENT
    rule_result = result_with_statuses(statuses)
    support = next(item for item in rule_result.items if item.field_code == field_code)
    rule = CplOccurrence(
        raw_text="최대 5,000만원",
        normalized_value={"amount_won": 50_000_000, "unit": "KRW"},
        axis_code=CplAxisCode.PER_COMPANY_LIMIT,
        source_role=CplSourceRole.SUPPORT_CONTENT,
        source_locator={
            "body_block_index": 4,
            "row": 3,
            "col": 1,
            "line_index": 0,
            "span_start": 10,
            "span_end": 20,
        },
        block_id="body:4:cell:3:1",
        extraction_method="RULE",
    )
    support.occurrences = [rule]
    candidate = CplItem(
        field_code=field_code,
        status=CplStatus.PRESENT,
        occurrences=[
            CplOccurrence(
                raw_text="기업당 한도: 최대 5,000만원",
                normalized_value={"amount_won": 50_000_000, "unit": "KRW"},
                axis_code=CplAxisCode.PER_COMPANY_LIMIT,
                source_role=CplSourceRole.SUPPORT_CONTENT,
                source_locator={
                    "body_block_index": 4,
                    "row": 3,
                    "col": 1,
                    "line_index": 0,
                    "span_start": 0,
                    "span_end": 30,
                },
                block_id="body:4:cell:3:1",
                extraction_method="LLM",
            ),
            CplOccurrence(
                raw_text="기업당 한도: 최대 5,000만원",
                normalized_value={"amount_won": 40_000_000, "unit": "KRW"},
                axis_code=CplAxisCode.PER_COMPANY_LIMIT,
                source_role=CplSourceRole.SUPPORT_CONTENT,
                source_locator={
                    "body_block_index": 4,
                    "row": 3,
                    "col": 1,
                    "line_index": 0,
                    "span_start": 0,
                    "span_end": 30,
                },
                block_id="body:4:cell:3:1",
                extraction_method="LLM",
            ),
            CplOccurrence(
                raw_text="기업당 한도: 최대 5,000만원",
                normalized_value={"amount_won": 50_000_000, "unit": "KRW"},
                axis_code=CplAxisCode.PER_COMPANY_LIMIT,
                source_role=CplSourceRole.SUPPORT_CONTENT,
                source_locator={
                    "body_block_index": 4,
                    "row": 9,
                    "col": 1,
                    "line_index": 1,
                    "span_start": 10,
                    "span_end": 20,
                },
                block_id="body:4:cell:9:1",
                extraction_method="LLM",
            ),
            CplOccurrence(
                raw_text="기업당 한도: 최대 5,000만원",
                normalized_value={"amount_won": 50_000_000, "unit": "KRW"},
                axis_code=CplAxisCode.PER_COMPANY_LIMIT,
                source_role=CplSourceRole.SUPPORT_CONTENT,
                source_locator={
                    "body_block_index": 4,
                    "row": 3,
                    "col": 1,
                    "line_index": 1,
                    "span_start": 10,
                    "span_end": 20,
                },
                block_id="body:4:cell:3:1",
                extraction_method="LLM",
            ),
        ],
    )

    merged = merge_llm_result(rule_result, [candidate])
    occurrences = next(
        item for item in merged.items if item.field_code == field_code
    ).occurrences

    assert occurrences == [
        rule,
        candidate.occurrences[1],
        candidate.occurrences[2],
        candidate.occurrences[3],
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


def test_partial_grounding_failure_is_not_hidden_by_plan_aggregation() -> None:
    # 기간은 Rule로 확인되지만 계획 문장에는 라벨이 없어 LLM 전에는
    # IMPLEMENTATION_PLAN이 확정되지 않는 문서다.
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="plan fixture",
        blocks=[
            DocumentBlock(
                block_id="period:0",
                block_type="paragraph",
                text="사업기간: 2026년 ~ 2028년",
            ),
                DocumentBlock(
                    block_id="plan:0",
                    block_type="paragraph",
                    text="2026년 구축, 2027년 확산",
                    section_path=["연차별 추진계획"],
                ),
                DocumentBlock(
                    block_id="plan:1",
                    block_type="paragraph",
                    text="내역사업 없음",
                    section_path=["내역사업별 추진계획"],
                ),
        ],
    )
    rule_result = evaluate_cpl_rules(document)
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "IMPLEMENTATION_PLAN",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "plan:0",
                            "raw_text": "2026년 구축, 2027년 확산",
                            "axis_code": "ANNUAL_PLAN_CONTENT",
                        },
                        {
                            "evidence_ref": "plan:1",
                            "raw_text": "내역사업 없음",
                            "axis_code": "PROGRAM_LEVEL_ABSENT",
                        },
                        {
                            "evidence_ref": "missing:ref",
                            "raw_text": "없는 근거",
                            "axis_code": "SUBPROGRAM_PLAN_CONTENT",
                        },
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    grounded = ground_llm_response(
        document,
        response,
        {CplFieldCode.IMPLEMENTATION_PLAN},
    )
    merged = merge_llm_result(rule_result, grounded)
    plan = next(
        item
        for item in merged.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )

    assert plan.status == CplStatus.NEEDS_CONFIRMATION
    assert plan.reason_code == "LLM_INVALID_RESPONSE"
    assert len(plan.occurrences) == 2


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
            json={"email": f"{login_id}@example.com", "password": PASSWORD},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        uploaded = client.post(
            "/api/v1/cases",
            headers=headers,
            files={"file": ("request.hwpx", hwpx_bytes(), "application/hwp+zip")},
        )
        assert uploaded.status_code == 200
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


def mockup_08_document() -> ParsedDocument:
    path = (
        Path(__file__).parents[2]
        / "samples"
        / "hwpx"
        / "mockup_08_CPL전항목_스마트기술사업화.hwpx"
    )
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("Contents/section0.xml"))
    cells = []
    for cell in root.iter():
        if not cell.tag.endswith("}tc"):
            continue
        address = next(child for child in cell if child.tag.endswith("}cellAddr"))
        paragraphs = []
        for paragraph in cell.iter():
            if paragraph.tag.endswith("}p"):
                text_parts = [
                    node.text or ""
                    for node in paragraph.iter()
                    if node.tag.endswith("}t")
                ]
                paragraphs.append("".join(text_parts))
        cells.append(
            {
                "row": int(address.attrib["rowAddr"]),
                "col": int(address.attrib["colAddr"]),
                "text": "\n".join(paragraphs),
            }
        )
    return ParsedDocument(
        parser_name="exact-hwpx-fixture",
        parser_version="1.0",
        text="mockup 08 exact table",
        blocks=[
            DocumentBlock(
                block_id="body:4",
                block_type="table",
                text="\n".join(cell["text"] for cell in cells),
                source_locator={"cells": cells},
            )
        ],
    )


def test_mockup_08_scopes_keep_container_and_inner_field_ownership() -> None:
    document = mockup_08_document()
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "TARGET_AND_CONDITIONS",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "body:4:cell:3:1",
                            "raw_text": "부산광역시 내 본사 또는 주된 사업장 보유",
                            "axis_code": "COND_REGION",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                },
                {
                    "field_code": "NEW_OR_CHANGED_CONTENT",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "body:4:cell:3:1",
                            "raw_text": "부산광역시 내 본사 또는 주된 사업장 보유",
                            "axis_code": "CHANGE_CONTENT",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                },
            ]
        }
    )

    grounded = ground_llm_response(
        document,
        response,
        {CplFieldCode.TARGET_AND_CONDITIONS, CplFieldCode.NEW_OR_CHANGED_CONTENT},
    )
    by_field = {item.field_code: item for item in grounded}
    condition = by_field[CplFieldCode.TARGET_AND_CONDITIONS].occurrences[0]
    assert condition.source_role == CplSourceRole.CONDITION
    assert by_field[CplFieldCode.NEW_OR_CHANGED_CONTENT].occurrences


def test_mockup_08_repeated_raw_text_with_different_scopes_is_rejected() -> None:
    response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "SUPPORT_CONTENT_AND_SCALE",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "body:4:cell:3:1",
                            "raw_text": "40개사",
                            "axis_code": "COMPANY_COUNT",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )

    item = ground_llm_response(
        mockup_08_document(),
        response,
        {CplFieldCode.SUPPORT_CONTENT_AND_SCALE},
    )[0]

    assert item.occurrences == []
    assert item.reason_code == "EVIDENCE_OWNERSHIP_UNRESOLVED"


def test_mockup_08_rule_values_keep_budget_positions_and_condition_ranges() -> None:
    result = evaluate_cpl_rules(mockup_08_document())
    items = {item.field_code: item for item in result.items}

    budget = items[CplFieldCode.BUDGET]
    assert len(budget.occurrences) == 6
    assert [occurrence.normalized_value for occurrence in budget.occurrences] == [
        {"amount_won": 6_600_000_000, "currency": "KRW", "kind": "TOTAL"},
        {"amount_won": 6_000_000_000, "currency": "KRW", "kind": "GRANT"},
        {"amount_won": 600_000_000, "currency": "KRW", "kind": "OPERATION"},
        {"amount_won": 2_200_000_000, "currency": "KRW", "year": 2026},
        {"amount_won": 2_200_000_000, "currency": "KRW", "year": 2027},
        {"amount_won": 2_200_000_000, "currency": "KRW", "year": 2028},
    ]
    assert [occurrence.raw_text for occurrence in budget.occurrences[-3:]] == [
        "2026년 2,200백만원",
        "2027년 2,200백만원",
        "2028년 2,200백만원",
    ]

    conditions = items[CplFieldCode.TARGET_AND_CONDITIONS].occurrences
    by_axis = {
        occurrence.axis_code: occurrence.normalized_value
        for occurrence in conditions
        if occurrence.axis_code is not None
    }
    assert by_axis[CplAxisCode.COND_BUSINESS_AGE] == {
        "min_years": 3,
        "max_years": 10,
        "min_operator": "GTE",
        "max_operator": "LTE",
        "unit": "YEAR",
    }
    assert by_axis[CplAxisCode.COND_REVENUE]["max_amount_won"] == 30_000_000_000
    assert by_axis[CplAxisCode.COND_HEADCOUNT]["max_operator"] == "LT"


def test_quantitative_normalization_accepts_repeated_identical_limit() -> None:
    document = ParsedDocument(
        parser_name="fixture-parser",
        parser_version="1.0",
        text="지원내용",
        blocks=[
            DocumentBlock(
                block_id="support:duplicate-limit",
                block_type="paragraph",
                text="지원내용: 기업당 한도 최대 5,000만원 (단가 5,000만원)",
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
                            "evidence_ref": "support:duplicate-limit",
                            "raw_text": "기업당 한도 최대 5,000만원 (단가 5,000만원)",
                            "axis_code": "PER_COMPANY_LIMIT",
                        }
                    ],
                    "reason_code": None,
                    "explanation": None,
                }
            ]
        }
    )
    item = ground_llm_response(
        document,
        response,
        {CplFieldCode.SUPPORT_CONTENT_AND_SCALE},
    )[0]
    assert item.occurrences[0].normalized_value == {
        "amount_won": 50_000_000,
        "unit": "KRW",
    }


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        (
            "2026. 01. 01. ~ 2028. 12. 31.",
            {
                "start": "2026-01-01",
                "end": "2028-12-31",
                "continuing": False,
                "multi_year": True,
            },
        ),
        (
            "2026년 1월 1일부터 2028년 12월 31일까지",
            {
                "start": "2026-01-01",
                "end": "2028-12-31",
                "continuing": False,
                "multi_year": True,
            },
        ),
        (
            "'21년 ~ '25년(5년)",
            {
                "start": "2021",
                "end": "2025",
                "continuing": False,
                "multi_year": True,
                "two_digit_year_policy": "ASSUME_2000S",
            },
        ),
    ],
)
def test_business_period_accepts_general_date_tokens(
    raw_text: str, expected: dict
) -> None:
    result = evaluate_cpl_rules(paragraph_document([f"사업기간: {raw_text}"]))
    occurrence = occurrences_for(result, CplFieldCode.BUSINESS_PERIOD)[0]
    assert occurrence.normalized_value == expected


@pytest.mark.parametrize(
    "raw_text",
    [
        "2026. 02. 30. ~ 2028. 12. 31.",
        "2026. 01. 01. ~ 2028. 02. 30.",
    ],
)
def test_business_period_does_not_promote_a_valid_boundary_when_the_other_is_invalid(
    raw_text: str,
) -> None:
    result = evaluate_cpl_rules(paragraph_document([f"사업기간: {raw_text}"]))
    occurrence = occurrences_for(result, CplFieldCode.BUSINESS_PERIOD)[0]
    assert occurrence.normalized_value is None


def test_legal_basis_emits_one_occurrence_per_citation() -> None:
    result = evaluate_cpl_rules(
        paragraph_document(
            [
                "지원근거: 「중소기업진흥에 관한 법률」 제62조의2, "
                "「부산광역시 중소기업 육성 및 지원 조례」 제9조"
            ]
        )
    )
    occurrences = occurrences_for(result, CplFieldCode.LEGAL_BASIS)
    assert [occurrence.normalized_value for occurrence in occurrences] == [
        {"law_name": "중소기업진흥에 관한 법률", "article": "제62조의2"},
        {
            "law_name": "부산광역시 중소기업 육성 및 지원 조례",
            "article": "제9조",
        },
    ]


@pytest.mark.parametrize(
    "raw_text",
    [
        "관련 법령에 따라 추진",
        "해당 법률에 따름",
        "상위 법령에 근거함",
        "기타 법률을 준용함",
    ],
)
def test_legal_basis_does_not_invent_a_law_from_a_generic_reference(
    raw_text: str,
) -> None:
    result = evaluate_cpl_rules(paragraph_document([f"지원근거: {raw_text}"]))
    item = next(
        item for item in result.items if item.field_code == CplFieldCode.LEGAL_BASIS
    )
    assert item.status == CplStatus.NEEDS_CONFIRMATION
    assert [occurrence.normalized_value for occurrence in item.occurrences] == [None]


def test_legal_basis_still_accepts_an_unquoted_named_ordinance() -> None:
    result = evaluate_cpl_rules(
        paragraph_document(
            ["지원근거: 대구광역시 수출 중소기업 육성 조례 제8조"]
        )
    )
    occurrence = occurrences_for(result, CplFieldCode.LEGAL_BASIS)[0]
    assert occurrence.normalized_value == {
        "law_name": "대구광역시 수출 중소기업 육성 조례",
        "article": "제8조",
    }


def test_numeric_condition_ranges_are_single_normalized_occurrences() -> None:
    result = evaluate_cpl_rules(
        paragraph_document(
            [
                "지원조건: 업력 3년 이상 10년 이내, 최근 3개년 연평균 "
                "매출액 10억원 이상 300억원 이하, 상시 근로자 10명 이상 "
                "300명 미만"
            ]
        )
    )
    occurrences = occurrences_for(result, CplFieldCode.TARGET_AND_CONDITIONS)
    by_axis = {
        occurrence.axis_code: occurrence
        for occurrence in occurrences
        if occurrence.axis_code is not None
    }
    assert by_axis[CplAxisCode.COND_BUSINESS_AGE].normalized_value == {
        "min_years": 3,
        "max_years": 10,
        "min_operator": "GTE",
        "max_operator": "LTE",
        "unit": "YEAR",
    }
    assert by_axis[CplAxisCode.COND_REVENUE].normalized_value == {
        "min_amount_won": 1_000_000_000,
        "max_amount_won": 30_000_000_000,
        "min_operator": "GTE",
        "max_operator": "LTE",
        "unit": "KRW",
        "period_years": 3,
    }
    assert by_axis[CplAxisCode.COND_HEADCOUNT].normalized_value == {
        "min_count": 10,
        "max_count": 300,
        "min_operator": "GTE",
        "max_operator": "LT",
        "unit": "PERSON",
    }


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


def test_llm_failure_only_degrades_the_fields_it_was_asked_about() -> None:
    """선택적 재검이 실패해도 첫 분석이 확정한 다른 필드를 덮지 않는다.

    재검 helper 는 요청한 필드만 다시 묻는데, 실패 처리는 semantic 필드
    전체를 훑어 MISSING 을 NEEDS_CONFIRMATION 으로 바꿨다. 목적 필드 재검
    timeout 이 사업필요성의 정상 판정까지 오염시킬 수 있었다.
    """
    rule_result = CplResult(
        ruleset_version="cpl-alpha-v0.2",
        items=[
            CplItem(field_code=field_code, status=CplStatus.MISSING)
            for field_code in CPL_FIELDS
        ],
    )

    degraded = merge_llm_result(
        rule_result,
        llm_error="LLM_TIMEOUT",
        requested_fields={CplFieldCode.PURPOSE_GOAL},
    )
    by_code = {item.field_code: item for item in degraded.items}
    assert by_code[CplFieldCode.PURPOSE_GOAL].status == CplStatus.NEEDS_CONFIRMATION
    assert by_code[CplFieldCode.PURPOSE_GOAL].reason_code == "LLM_TIMEOUT"
    assert by_code[CplFieldCode.BUSINESS_NEED].status == CplStatus.MISSING
    assert by_code[CplFieldCode.BUSINESS_NEED].reason_code != "LLM_TIMEOUT"

    # 범위를 주지 않으면 기존대로 semantic 필드 전체가 대상이다.
    everything = merge_llm_result(rule_result, llm_error="LLM_TIMEOUT")
    assert all(
        item.status == CplStatus.NEEDS_CONFIRMATION
        for item in everything.items
        if item.field_code in CPL_SEMANTIC_FIELDS
    )


@pytest.mark.parametrize(
    "failure_reason",
    [
        "LLM_UNAVAILABLE",
        "LLM_TIMEOUT",
        "LLM_INVALID_RESPONSE",
        "EVIDENCE_OWNERSHIP_UNRESOLVED",
    ],
)
def test_targeted_recheck_without_plan_preserves_prior_plan_failure(
    failure_reason: str,
) -> None:
    document = implementation_plan_document(
        "2026년 ~ 2028년",
        [
            "연차별 추진계획: 2026년 구축, 2027년 확산",
            "내역사업별 추진계획: 내역사업",
        ],
    )
    rule_result = evaluate_cpl_rules(document)
    first = merge_llm_result(
        rule_result,
        [
            CplItem(
                field_code=CplFieldCode.IMPLEMENTATION_PLAN,
                status=CplStatus.NEEDS_CONFIRMATION,
                reason_code=failure_reason,
                explanation="잘못된 계획 근거 응답",
            )
        ],
    )
    first_plan = next(
        item
        for item in first.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert first_plan.status == CplStatus.NEEDS_CONFIRMATION
    assert first_plan.reason_code == failure_reason

    rechecked = merge_llm_result(
        first,
        [
            CplItem(
                field_code=CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
                status=CplStatus.NEEDS_CONFIRMATION,
                reason_code="SEMANTIC_REVIEW_REQUIRED",
            )
        ],
    )
    rechecked_plan = next(
        item
        for item in rechecked.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )

    assert rechecked_plan.status == first_plan.status
    assert rechecked_plan.reason_code == first_plan.reason_code
    assert rechecked_plan.explanation == first_plan.explanation
    assert rechecked_plan.occurrences == first_plan.occurrences


def test_valid_plan_recheck_recomputes_implementation_plan_status() -> None:
    document = implementation_plan_document(
        "2026년 ~ 2028년",
        [
            "추진계획: 2026년 구축, 2027년 확산; 내역사업 추진내용",
        ],
    )
    rule_result = evaluate_cpl_rules(document)
    invalid_response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "IMPLEMENTATION_PLAN",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "plan:0",
                            "raw_text": "사업 목적 raw",
                            "axis_code": "ANNUAL_PLAN_CONTENT",
                        }
                    ],
                    "reason_code": None,
                    "explanation": "잘못된 계획 근거 응답",
                }
            ]
        }
    )
    first = merge_llm_result(
        rule_result,
        ground_llm_response(
            document, invalid_response, {CplFieldCode.IMPLEMENTATION_PLAN}
        ),
    )
    first_plan = next(
        item
        for item in first.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    assert first_plan.status == CplStatus.NEEDS_CONFIRMATION
    assert first_plan.reason_code == "LLM_INVALID_RESPONSE"
    before_recheck = first_plan.model_dump(mode="json")

    valid_response = CplSemanticResponse.model_validate(
        {
            "items": [
                {
                    "field_code": "IMPLEMENTATION_PLAN",
                    "status": "PRESENT",
                    "occurrences": [
                        {
                            "evidence_ref": "plan:0",
                            "raw_text": "2026년 구축, 2027년 확산",
                            "axis_code": "ANNUAL_PLAN_CONTENT",
                        },
                        {
                            "evidence_ref": "plan:0",
                            "raw_text": "내역사업 추진내용",
                            "axis_code": "SUBPROGRAM_PLAN_CONTENT",
                        },
                    ],
                    "reason_code": None,
                    "explanation": "계획 근거 확인",
                }
            ]
        }
    )
    recovered = merge_llm_result(
        first,
        ground_llm_response(
            document, valid_response, {CplFieldCode.IMPLEMENTATION_PLAN}
        ),
    )
    plan = next(
        item
        for item in recovered.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )

    assert plan.status == CplStatus.PRESENT
    assert plan.reason_code is None
    assert any(
        occurrence.axis_code == CplAxisCode.SUBPROGRAM_PLAN_CONTENT
        for occurrence in plan.occurrences
    )
    assert first_plan.model_dump(mode="json") == before_recheck


@pytest.mark.parametrize("candidate_mode", ["invalid", "duplicate"])
def test_invalid_plan_candidate_does_not_recompute_prior_plan_failure(
    candidate_mode: str,
) -> None:
    document = implementation_plan_document(
        "2026년 ~ 2028년",
        [
            "연차별 추진계획: 2026년 구축, 2027년 확산",
            "내역사업별 추진계획: 내역사업",
        ],
    )
    first = merge_llm_result(
        evaluate_cpl_rules(document),
        [
            CplItem(
                field_code=CplFieldCode.IMPLEMENTATION_PLAN,
                status=CplStatus.NEEDS_CONFIRMATION,
                reason_code="LLM_TIMEOUT",
            )
        ],
    )
    if candidate_mode == "invalid":
        candidates = [
            CplItem(
                field_code=CplFieldCode.IMPLEMENTATION_PLAN,
                status=CplStatus.PRESENT,
            )
        ]
    else:
        candidates = [
            CplItem(
                field_code=CplFieldCode.IMPLEMENTATION_PLAN,
                status=CplStatus.NEEDS_CONFIRMATION,
                reason_code="LLM_INVALID_RESPONSE",
            ),
            CplItem(
                field_code=CplFieldCode.IMPLEMENTATION_PLAN,
                status=CplStatus.PRESENT,
            ),
        ]

    merged = merge_llm_result(first, candidates)
    plan = next(
        item
        for item in merged.items
        if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )

    assert plan.status == CplStatus.NEEDS_CONFIRMATION
    assert plan.reason_code == "LLM_INVALID_RESPONSE"


def test_parser_version_survives_missing_rhwp_distribution() -> None:
    """rhwp 배포판이 없어도 모듈 import 가 깨지지 않는다.

    version() 을 클래스 본문에서 부르면 PackageNotFoundError 가 올라와
    앞의 ImportError fallback 이 무의미해진다.
    """
    from app.parsers import hwp_parser

    assert isinstance(hwp_parser.RhwpDocumentParser.version, str)
    assert hwp_parser.RhwpDocumentParser.version
