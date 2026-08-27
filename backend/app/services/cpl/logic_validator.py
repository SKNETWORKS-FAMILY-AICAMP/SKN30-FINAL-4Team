import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Literal

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
from app.schemas.parsed_document import ParsedDocument


DEFAULT_RULESET_VERSION = "cpl-alpha-v0.2"
LlmFailureCode = Literal[
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_INVALID_RESPONSE",
]
LLM_FAILURE_CODES = frozenset(
    {"LLM_UNAVAILABLE", "LLM_TIMEOUT", "LLM_INVALID_RESPONSE"}
)

CPL_FIELD_LABELS: dict[CplFieldCode, str] = {
    CplFieldCode.REQUEST_TYPE: "요청유형 체크값",
    CplFieldCode.PURPOSE_GOAL: "사업 목적·목표",
    CplFieldCode.IMPLEMENTATION_PLAN: "연차별·내역사업별 추진계획",
    CplFieldCode.BUSINESS_PERIOD: "사업기간",
    CplFieldCode.NEW_OR_CHANGED_CONTENT: "신설·변경 주요내용",
    CplFieldCode.BUSINESS_NEED: "사업필요성 최소 논리구조",
    CplFieldCode.LEGAL_BASIS: "지원근거",
    CplFieldCode.LINKED_POLICY: "연계정책",
    CplFieldCode.BUDGET: "사업예산",
    CplFieldCode.TARGET_AND_CONDITIONS: "지원대상·지원조건",
    CplFieldCode.SUPPORT_CONTENT_AND_SCALE: "지원내용·지원규모",
    CplFieldCode.DELIVERY_SYSTEM: "수행기관·수행방식·수행체계",
    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE: "기대효과·성과 관련 정보",
}

_LABEL_ALIASES: dict[CplFieldCode, tuple[str, ...]] = {
    CplFieldCode.PURPOSE_GOAL: (
        "사업 목적 및 목표",
        "사업목적 및 목표",
        "사업 목적·목표",
        "사업 목적",
        "사업목적",
        "사업 목표",
        "사업목표",
    ),
    CplFieldCode.IMPLEMENTATION_PLAN: (
        "연차별·내역사업별 추진계획",
        "연차별 및 내역사업별 추진계획",
        "연차별 추진계획",
        "내역사업별 추진계획",
        "내내역사업별 추진계획",
        "추진계획",
    ),
    CplFieldCode.BUSINESS_PERIOD: ("사업기간", "사업 기간"),
    CplFieldCode.NEW_OR_CHANGED_CONTENT: (
        "신설·변경 주요내용",
        "신설 주요내용",
        "변경 주요내용",
        "주요 변경내용",
    ),
    CplFieldCode.BUSINESS_NEED: (
        "사업필요성",
        "사업 필요성",
        "추진 필요성",
    ),
    CplFieldCode.LEGAL_BASIS: (
        "지원근거",
        "법적 근거",
        "법령 근거",
    ),
    CplFieldCode.LINKED_POLICY: (
        "연계정책",
        "연계 정책",
        "관련 정책",
    ),
    CplFieldCode.BUDGET: ("사업예산", "사업 예산", "총사업비", "총 사업비"),
    CplFieldCode.TARGET_AND_CONDITIONS: (
        "지원대상 및 지원조건",
        "지원대상·지원조건",
        "지원대상",
        "지원 대상",
        "지원조건",
        "지원 조건",
    ),
    CplFieldCode.SUPPORT_CONTENT_AND_SCALE: (
        "지원내용 및 지원규모",
        "지원내용·지원규모",
        "지원내용",
        "지원 내용",
        "지원규모",
        "지원 규모",
    ),
    CplFieldCode.DELIVERY_SYSTEM: (
        "수행기관·수행방식·수행체계",
        "수행기관 및 수행방식 및 수행체계",
        "수행기관",
        "수행 기관",
        "수행방식",
        "수행 방식",
        "수행체계",
        "수행 체계",
        "추진체계",
        "역할",
        "기관 역할",
        "단계별 역할",
        "수행절차",
        "수행 절차",
        "추진절차",
        "추진 절차",
    ),
    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE: (
        "기대효과·성과 관련 정보",
        "기대효과 및 성과 관련 정보",
        "기대효과",
        "기대 효과",
        "파급효과",
        "파급 효과",
        "성과지표",
        "성과 지표",
        "성과목표",
    ),
}

_REQUEST_TYPES: tuple[tuple[str, str], ...] = (
    ("내내역사업 신설", "SUBSUBPROGRAM_NEW"),
    ("내역사업 신설", "SUBPROGRAM_NEW"),
    ("세부사업 신설", "DETAIL_NEW"),
    ("사업내용 변경", "CONTENT_CHANGE"),
)
_SELECTED_MARKS = frozenset({"■", "☑", "▣", "✓", "✔"})
_CHECKBOX_PATTERN = re.compile(
    r"(?P<mark>[□☐■☑▣✓✔])\s*"
    r"(?P<label>내내역사업\s*신설|내역사업\s*신설|세부사업\s*신설|사업내용\s*변경)"
)
# 24년도 서식 [서식 1]의 항목명은 "사전협의 요청사유"이고 CPL 판별기준 2절은
# 같은 영역을 "요청유형"으로 부른다. 두 표기를 모두 영역 표지로 받는다.
_REQUEST_TYPE_AREA_PATTERN = re.compile(r"(?:사전\s*협의\s*)?요청\s*(?:유형|사유)")
_LINE_PREFIX = r"^\s*(?:(?:[○◦●•※□■☑▣✓✔\-–—])|(?:\d+[.)]))*\s*"
_PERIOD_PATTERN = re.compile(
    r"(?P<start_year>20\d{2})\s*(?:[.년/-]\s*(?P<start_month>\d{1,2}))?"
    r"\s*(?:년|월|[.])?\s*(?:~|∼|～|부터|-)\s*"
    r"(?:(?P<end_year>20\d{2})\s*(?:[.년/-]\s*(?P<end_month>\d{1,2}))?"
    r"\s*(?:년|월|[.])?|(?P<continuing>계속))"
)
_SINGLE_YEAR_PATTERN = re.compile(r"(?<!\d)(?P<year>20\d{2})\s*년")
_AMOUNT_PATTERN = re.compile(
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<unit>조|억|천만|백만|십만|만|천|백|십)?\s*원?"
)
_LAW_PATTERN = re.compile(
    r"(?P<law>[가-힣A-Za-z0-9·ㆍ ]{1,80}?법)"
    r"(?:\s*(?P<article>제\s*\d+\s*조(?:의\s*\d+)?))?"
)
_RATIO_PATTERN = re.compile(r"(?<![\d.])(?P<number>\d+(?:\.\d+)?)\s*%")
_COMPANY_COUNT_PATTERN = re.compile(
    r"(?<!\d)(?P<number>\d{1,3}(?:,\d{3})*|\d+)\s*(?:개\s*사|기업|개\s*업체)"
)
_NUMBER_PATTERN = re.compile(
    r"(?<!\d)(?P<number>\d{1,3}(?:,\d{3})*|\d+)(?:\.(?P<decimal>\d+))?"
)
_PROGRAM_LEVEL_PATTERN = re.compile(r"(?P<level>내내역사업|내역사업|세부사업)")
_GENERIC_POLICY_VALUES = frozenset(
    {"관련 정책", "정부 정책", "상위 정책", "관련 계획", "정책 연계"}
)
_AMOUNT_MULTIPLIERS = {
    None: Decimal(1),
    "십": Decimal(10),
    "백": Decimal(100),
    "천": Decimal(1_000),
    "만": Decimal(10_000),
    "십만": Decimal(100_000),
    "백만": Decimal(1_000_000),
    "천만": Decimal(10_000_000),
    "억": Decimal(100_000_000),
    "조": Decimal(1_000_000_000_000),
}

CPL_SEMANTIC_FIELDS = frozenset(
    {
        CplFieldCode.PURPOSE_GOAL,
        CplFieldCode.IMPLEMENTATION_PLAN,
        CplFieldCode.NEW_OR_CHANGED_CONTENT,
        CplFieldCode.BUSINESS_NEED,
        CplFieldCode.TARGET_AND_CONDITIONS,
        CplFieldCode.SUPPORT_CONTENT_AND_SCALE,
        CplFieldCode.DELIVERY_SYSTEM,
        CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE,
    }
)

CPL_FIELD_AXES: dict[CplFieldCode, frozenset[CplAxisCode]] = {
    CplFieldCode.PURPOSE_GOAL: frozenset(
        {
            CplAxisCode.PURPOSE_TARGET_CONDITION,
            CplAxisCode.PURPOSE_PROBLEM_DOMAIN,
            CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
            CplAxisCode.PURPOSE_DIRECTION,
        }
    ),
    CplFieldCode.IMPLEMENTATION_PLAN: frozenset(
        {
            CplAxisCode.ANNUAL_PLAN_CONTENT,
            CplAxisCode.PROGRAM_LEVEL,
            CplAxisCode.PROGRAM_LEVEL_ABSENT,
            CplAxisCode.SUBPROGRAM_PLAN_CONTENT,
        }
    ),
    CplFieldCode.NEW_OR_CHANGED_CONTENT: frozenset({CplAxisCode.CHANGE_CONTENT}),
    CplFieldCode.BUSINESS_NEED: frozenset(
        {
            CplAxisCode.NEED_PROBLEM,
            CplAxisCode.NEED_CAUSE,
            CplAxisCode.NEED_RESPONSE,
            CplAxisCode.NEED_SOURCE,
        }
    ),
    CplFieldCode.TARGET_AND_CONDITIONS: frozenset(
        {
            CplAxisCode.TARGET_GROUP,
            CplAxisCode.COND_COMPANY_TYPE,
            CplAxisCode.COND_INDUSTRY,
            CplAxisCode.COND_REGION,
            CplAxisCode.COND_BUSINESS_AGE,
            CplAxisCode.COND_REVENUE,
            CplAxisCode.COND_HEADCOUNT,
            CplAxisCode.COND_CERTIFICATION,
            CplAxisCode.COND_OTHER,
            CplAxisCode.COND_EXCLUSION,
        }
    ),
    CplFieldCode.SUPPORT_CONTENT_AND_SCALE: frozenset(
        {
            CplAxisCode.SUPPORT_ACTIVITY,
            CplAxisCode.SUPPORT_INSTRUMENT,
            CplAxisCode.SUPPORT_ITEM,
            CplAxisCode.PER_COMPANY_LIMIT,
            CplAxisCode.COMPANY_COUNT,
            CplAxisCode.SUBSIDY_RATE,
            CplAxisCode.SELF_BURDEN_RATE,
            CplAxisCode.TOTAL_SCALE,
        }
    ),
    CplFieldCode.DELIVERY_SYSTEM: frozenset(
        {
            CplAxisCode.DELIVERY_ORG_NAME,
            CplAxisCode.DELIVERY_METHOD_TYPE,
            CplAxisCode.DELIVERY_PROCEDURE_STEP,
            CplAxisCode.DELIVERY_STEP_ROLE,
        }
    ),
    CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE: frozenset(
        {
            CplAxisCode.EFFECT_SUBJECT,
            CplAxisCode.EFFECT_CONTENT,
            CplAxisCode.EFFECT_DIRECTION,
            CplAxisCode.KPI_NAME,
            CplAxisCode.KPI_TARGET_VALUE,
            CplAxisCode.KPI_UNIT,
            CplAxisCode.KPI_BASE_YEAR,
            CplAxisCode.KPI_FORMULA,
        }
    ),
}

_QUANTITATIVE_AXES = frozenset(
    {
        CplAxisCode.PER_COMPANY_LIMIT,
        CplAxisCode.COMPANY_COUNT,
        CplAxisCode.SUBSIDY_RATE,
        CplAxisCode.SELF_BURDEN_RATE,
        CplAxisCode.TOTAL_SCALE,
    }
)

_AXIS_SOURCE_ROLES: dict[CplAxisCode, CplSourceRole] = {
    CplAxisCode.TARGET_GROUP: CplSourceRole.TARGET,
    CplAxisCode.COND_COMPANY_TYPE: CplSourceRole.CONDITION,
    CplAxisCode.COND_INDUSTRY: CplSourceRole.CONDITION,
    CplAxisCode.COND_REGION: CplSourceRole.CONDITION,
    CplAxisCode.COND_BUSINESS_AGE: CplSourceRole.CONDITION,
    CplAxisCode.COND_REVENUE: CplSourceRole.CONDITION,
    CplAxisCode.COND_HEADCOUNT: CplSourceRole.CONDITION,
    CplAxisCode.COND_CERTIFICATION: CplSourceRole.CONDITION,
    CplAxisCode.COND_OTHER: CplSourceRole.CONDITION,
    CplAxisCode.COND_EXCLUSION: CplSourceRole.CONDITION,
    CplAxisCode.SUPPORT_ACTIVITY: CplSourceRole.SUPPORT_CONTENT,
    CplAxisCode.SUPPORT_INSTRUMENT: CplSourceRole.SUPPORT_CONTENT,
    CplAxisCode.SUPPORT_ITEM: CplSourceRole.SUPPORT_CONTENT,
    CplAxisCode.DELIVERY_ORG_NAME: CplSourceRole.DELIVERY_ORG,
    CplAxisCode.DELIVERY_METHOD_TYPE: CplSourceRole.DELIVERY_METHOD,
    CplAxisCode.DELIVERY_PROCEDURE_STEP: CplSourceRole.DELIVERY_PROCEDURE,
    CplAxisCode.DELIVERY_STEP_ROLE: CplSourceRole.DELIVERY_PROCEDURE,
    CplAxisCode.EFFECT_SUBJECT: CplSourceRole.EXPECTED_EFFECT,
    CplAxisCode.EFFECT_CONTENT: CplSourceRole.EXPECTED_EFFECT,
    CplAxisCode.EFFECT_DIRECTION: CplSourceRole.EXPECTED_EFFECT,
    CplAxisCode.KPI_NAME: CplSourceRole.PERFORMANCE_INDICATOR,
    CplAxisCode.KPI_TARGET_VALUE: CplSourceRole.PERFORMANCE_INDICATOR,
    CplAxisCode.KPI_UNIT: CplSourceRole.PERFORMANCE_INDICATOR,
    CplAxisCode.KPI_BASE_YEAR: CplSourceRole.PERFORMANCE_INDICATOR,
    CplAxisCode.KPI_FORMULA: CplSourceRole.PERFORMANCE_INDICATOR,
    CplAxisCode.ANNUAL_PLAN_CONTENT: CplSourceRole.ANNUAL_PLAN,
    CplAxisCode.PROGRAM_LEVEL: CplSourceRole.SUBPROGRAM_PLAN,
    CplAxisCode.PROGRAM_LEVEL_ABSENT: CplSourceRole.SUBPROGRAM_PLAN,
    CplAxisCode.SUBPROGRAM_PLAN_CONTENT: CplSourceRole.SUBPROGRAM_PLAN,
}


@dataclass(frozen=True)
class _Fragment:
    evidence_ref: str
    text: str
    page_no: int | None
    section_path: list[str]
    source_locator: dict
    block_id: str
    source_role: CplSourceRole | None = None


def evaluate_cpl_rules(
    document: ParsedDocument,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
) -> CplResult:
    """Extract only explicit, reproducible CPL evidence from a parsed document."""
    fragments = _document_fragments(document)
    items: list[CplItem] = [_request_type_item(fragments)]

    for field_code in CPL_FIELDS[1:]:
        occurrences = _labeled_occurrences(field_code, fragments)
        items.append(_item_from_rule_occurrences(field_code, occurrences))

    _resolve_implementation_plan(items)

    return CplResult(
        ruleset_version=ruleset_version,
        items=items,
        warnings=list(document.warnings),
    )


def merge_llm_result(
    rule_result: CplResult,
    llm_items: list[CplItem] | None = None,
    *,
    llm_error: LlmFailureCode | None = None,
) -> CplResult:
    """Merge grounded LLM facts without replacing deterministic Rule evidence."""
    if llm_error is not None:
        return _result_with_llm_failure(rule_result, llm_error)
    if llm_items is None:
        return rule_result.model_copy(deep=True)

    if len({item.field_code for item in llm_items}) != len(llm_items):
        return _result_with_llm_failure(rule_result, "LLM_INVALID_RESPONSE")

    candidate_by_code = {item.field_code: item for item in llm_items}
    for candidate in llm_items:
        if candidate.status == CplStatus.PARSE_FAILED:
            return _result_with_llm_failure(rule_result, "LLM_INVALID_RESPONSE")
        if candidate.status == CplStatus.PRESENT and not candidate.occurrences:
            return _result_with_llm_failure(rule_result, "LLM_INVALID_RESPONSE")
        if any(
            occurrence.extraction_method != "LLM"
            for occurrence in candidate.occurrences
        ):
            return _result_with_llm_failure(rule_result, "LLM_INVALID_RESPONSE")

    merged_items: list[CplItem] = []
    for rule_item in rule_result.items:
        candidate = candidate_by_code.get(rule_item.field_code)
        if candidate is None:
            merged_items.append(rule_item.model_copy(deep=True))
            continue

        occurrences = _merge_occurrences(
            rule_item.occurrences,
            candidate.occurrences,
        )
        rule_is_conclusive = rule_item.status in {
            CplStatus.PRESENT,
            CplStatus.NOT_APPLICABLE,
        }
        evidence_conflict = bool(rule_item.occurrences) and (
            candidate.status == CplStatus.MISSING
        )
        status = (
            rule_item.status
            if rule_is_conclusive
            else CplStatus.NEEDS_CONFIRMATION
            if evidence_conflict
            else candidate.status
        )
        merged_items.append(
            CplItem(
                field_code=rule_item.field_code,
                status=status,
                occurrences=occurrences,
                reason_code=(
                    rule_item.reason_code
                    if rule_is_conclusive
                    else "EVIDENCE_CONFLICT"
                    if evidence_conflict
                    else candidate.reason_code
                ),
                explanation=(
                    rule_item.explanation if rule_is_conclusive else candidate.explanation
                ),
            )
        )

    _resolve_implementation_plan(merged_items)
    return CplResult(
        ruleset_version=rule_result.ruleset_version,
        items=merged_items,
        warnings=list(rule_result.warnings),
        model_profile=rule_result.model_profile,
        prompt_version=rule_result.prompt_version,
    )


def ground_llm_response(
    document: ParsedDocument,
    response: CplSemanticResponse,
    expected_fields: set[CplFieldCode],
) -> list[CplItem]:
    """Replace model-provided lineage with lineage verified against parser output."""
    codes = [item.field_code for item in response.items]
    if len(codes) != len(set(codes)) or set(codes) != expected_fields:
        raise ValueError("LLM response fields do not match the requested CPL fields")
    if not expected_fields.issubset(CPL_SEMANTIC_FIELDS):
        raise ValueError("LLM response contains a Rule-only CPL field")

    fragments = semantic_fragments(document)
    fragment_by_ref = {fragment.evidence_ref: fragment for fragment in fragments}
    if len(fragment_by_ref) != len(fragments):
        raise ValueError("Parser fragments do not have unique evidence references")
    grounded: list[CplItem] = []
    for item in response.items:
        if item.status == "PRESENT" and not item.occurrences:
            raise ValueError("PRESENT LLM result requires evidence")
        if item.status == "MISSING" and item.occurrences:
            raise ValueError("MISSING LLM result cannot contain evidence")
        if item.status == "NEEDS_CONFIRMATION" and not item.reason_code:
            raise ValueError("NEEDS_CONFIRMATION LLM result requires a reason")

        occurrences: list[CplOccurrence] = []
        for occurrence in item.occurrences:
            if not occurrence.raw_text.strip() or not occurrence.evidence_ref.strip():
                raise ValueError("LLM evidence must not be blank")
            fragment = fragment_by_ref.get(occurrence.evidence_ref)
            if fragment is None:
                raise ValueError("LLM evidence reference does not exist")
            if occurrence.raw_text not in fragment.text:
                raise ValueError("LLM evidence is not contained in the parser fragment")
            if occurrence.axis_code not in CPL_FIELD_AXES[item.field_code]:
                raise ValueError("LLM axis is not allowed for the CPL field")
            source_role = fragment.source_role
            if (
                source_role is None
                and occurrence.axis_code not in _QUANTITATIVE_AXES
                and occurrence.axis_code != CplAxisCode.PROGRAM_LEVEL_ABSENT
            ):
                source_role = _AXIS_SOURCE_ROLES.get(occurrence.axis_code)
            normalized_value = _normalize_axis_value(
                occurrence.axis_code,
                occurrence.raw_text,
                source_role,
            )
            occurrences.append(
                CplOccurrence(
                    raw_text=occurrence.raw_text,
                    normalized_value=normalized_value,
                    axis_code=occurrence.axis_code,
                    source_role=source_role,
                    page_no=fragment.page_no,
                    section_path=list(fragment.section_path),
                    source_locator=dict(fragment.source_locator),
                    block_id=fragment.block_id,
                    extraction_method="LLM",
                )
            )
        grounded.append(
            CplItem(
                field_code=item.field_code,
                status=CplStatus(item.status),
                occurrences=_deduplicate_occurrences(occurrences),
                reason_code=item.reason_code,
                explanation=item.explanation,
            )
        )
    return grounded


def _document_fragments(document: ParsedDocument) -> list[_Fragment]:
    fragments: list[_Fragment] = []
    for block in document.blocks:
        cells = block.source_locator.get("cells")
        if block.block_type == "table" and isinstance(cells, list) and cells:
            base_locator = {
                key: value
                for key, value in block.source_locator.items()
                if key != "cells"
            }
            for cell_index, cell in enumerate(cells):
                if not isinstance(cell, dict):
                    continue
                text = cell.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                cell_locator = {
                    key: value
                    for key, value in cell.items()
                    if key != "text"
                }
                fragments.append(
                    _Fragment(
                        evidence_ref=_cell_evidence_ref(
                            block.block_id,
                            cell_locator,
                            cell_index,
                        ),
                        text=text.strip(),
                        page_no=block.page_no,
                        section_path=list(block.section_path),
                        source_locator={
                            **base_locator,
                            "table_cell": cell_locator,
                        },
                        block_id=block.block_id,
                    )
                )
        elif block.text.strip():
            fragments.append(
                _Fragment(
                    evidence_ref=block.block_id,
                    text=block.text.strip(),
                    page_no=block.page_no,
                    section_path=list(block.section_path),
                    source_locator=dict(block.source_locator),
                    block_id=block.block_id,
                )
            )
    return fragments


def semantic_fragments(document: ParsedDocument) -> list[_Fragment]:
    """Return parser fragments with only deterministic section roles attached."""
    fragments = _document_fragments(document)
    roles: dict[str, set[CplSourceRole]] = {
        fragment.evidence_ref: set() for fragment in fragments
    }
    for fragment in fragments:
        for field_code in CPL_SEMANTIC_FIELDS:
            for line in filter(None, (line.strip() for line in fragment.text.splitlines())):
                match = _label_match(line, _LABEL_ALIASES[field_code])
                if match is None:
                    continue
                role = _source_role_for_alias(match.group("label"))
                if role is None:
                    continue
                value = match.group("value").strip(" \t:：-–—")
                target = fragment if value else _adjacent_table_value(fragment, fragments)
                if target is not None:
                    roles[target.evidence_ref].add(role)
    return [
        replace(
            fragment,
            source_role=next(iter(roles[fragment.evidence_ref]))
            if len(roles[fragment.evidence_ref]) == 1
            else None,
        )
        for fragment in fragments
    ]


def _cell_evidence_ref(
    block_id: str,
    locator: dict,
    cell_index: int,
) -> str:
    row = locator.get("row")
    col = locator.get("col")
    if isinstance(row, int) and isinstance(col, int):
        return f"{block_id}:cell:{row}:{col}"
    return f"{block_id}:cell:{cell_index}"


def _request_type_item(fragments: list[_Fragment]) -> CplItem:
    occurrences: list[CplOccurrence] = []
    for fragment in _request_type_fragments(fragments):
        for match in _CHECKBOX_PATTERN.finditer(fragment.text):
            label = re.sub(r"\s+", "", match.group("label"))
            request_reason = next(
                code
                for known_label, code in _REQUEST_TYPES
                if re.sub(r"\s+", "", known_label) == label
            )
            mark = match.group("mark")
            occurrences.append(
                _occurrence(
                    fragment,
                    match.group(0),
                    {
                        "request_reason": request_reason,
                        "selected": mark in _SELECTED_MARKS,
                        "mark": mark,
                    },
                )
            )

    occurrences = _deduplicate_occurrences(occurrences)
    selected_count = sum(
        isinstance(occurrence.normalized_value, dict)
        and occurrence.normalized_value.get("selected") is True
        for occurrence in occurrences
    )
    if selected_count == 1:
        status = CplStatus.PRESENT
        reason_code = None
    elif not occurrences:
        status = CplStatus.MISSING
        reason_code = "REQUEST_TYPE_NOT_FOUND"
    else:
        status = CplStatus.NEEDS_CONFIRMATION
        reason_code = "REQUEST_TYPE_AMBIGUOUS"
    return CplItem(
        field_code=CplFieldCode.REQUEST_TYPE,
        status=status,
        occurrences=occurrences,
        reason_code=reason_code,
    )


def _request_type_fragments(fragments: list[_Fragment]) -> list[_Fragment]:
    allowed_refs: set[str] = set()
    for index, fragment in enumerate(fragments):
        in_named_section = any(
            _REQUEST_TYPE_AREA_PATTERN.search(part)
            for part in fragment.section_path
        )
        has_area_marker = bool(_REQUEST_TYPE_AREA_PATTERN.search(fragment.text))
        if in_named_section or has_area_marker:
            allowed_refs.add(fragment.evidence_ref)
            cell = fragment.source_locator.get("table_cell")
            if isinstance(cell, dict) and isinstance(cell.get("row"), int):
                row = cell["row"]
                for candidate in fragments:
                    candidate_cell = candidate.source_locator.get("table_cell")
                    if (
                        candidate.block_id == fragment.block_id
                        and isinstance(candidate_cell, dict)
                        and candidate_cell.get("row") == row
                    ):
                        allowed_refs.add(candidate.evidence_ref)

            if has_area_marker and not _CHECKBOX_PATTERN.search(fragment.text):
                next_block_id = next(
                    (
                        candidate.block_id
                        for candidate in fragments[index + 1 :]
                        if candidate.block_id != fragment.block_id
                    ),
                    None,
                )
                if next_block_id is not None:
                    allowed_refs.update(
                        candidate.evidence_ref
                        for candidate in fragments
                        if candidate.block_id == next_block_id
                    )
    return [
        fragment for fragment in fragments if fragment.evidence_ref in allowed_refs
    ]


def _labeled_occurrences(
    field_code: CplFieldCode,
    fragments: list[_Fragment],
) -> list[CplOccurrence]:
    aliases = _LABEL_ALIASES[field_code]
    occurrences: list[CplOccurrence] = []
    label_only_fragments: list[tuple[_Fragment, CplAxisCode | None]] = []

    for fragment in fragments:
        for line in filter(None, (line.strip() for line in fragment.text.splitlines())):
            match = _label_match(line, aliases)
            if match is None:
                continue
            value = match.group("value").strip(" \t:：-–—")
            source_role = _source_role_for_alias(match.group("label"))
            axis_code = _axis_for_alias(match.group("label"))
            if not value:
                label_only_fragments.append(
                    (replace(fragment, source_role=source_role), axis_code)
                )
                continue
            occurrences.extend(
                _normalized_occurrences(
                    field_code,
                    fragment,
                    value,
                    axis_code=axis_code,
                    source_role=source_role,
                )
            )

    for label_fragment, axis_code in label_only_fragments:
        adjacent = _adjacent_table_value(label_fragment, fragments)
        if adjacent is None:
            occurrences.append(
                _occurrence(
                    label_fragment,
                    label_fragment.text,
                    None,
                    source_role=label_fragment.source_role,
                )
            )
        else:
            occurrences.extend(
                _normalized_occurrences(
                    field_code,
                    adjacent,
                    adjacent.text,
                    axis_code=axis_code,
                    source_role=label_fragment.source_role,
                )
            )
    return _deduplicate_occurrences(occurrences)


def _label_match(line: str, aliases: tuple[str, ...]) -> re.Match[str] | None:
    alternatives = "|".join(
        re.escape(alias).replace(r"\ ", r"\s*")
        for alias in sorted(aliases, key=len, reverse=True)
    )
    # 서식 안내자료의 "작성 우수 사례"는 "○ (사업기간) ..." 처럼 라벨을 괄호로
    # 감싼다. 괄호를 라벨 쪽에서 소비해야 값에 닫는 괄호가 남지 않는다.
    return re.match(
        rf"{_LINE_PREFIX}\(?\s*(?P<label>{alternatives})\s*\)?"
        rf"(?:\s*[:：])?\s*(?P<value>.*)$",
        line,
    )


def _adjacent_table_value(
    label: _Fragment,
    fragments: list[_Fragment],
) -> _Fragment | None:
    label_cell = label.source_locator.get("table_cell")
    if not isinstance(label_cell, dict):
        return None
    row = label_cell.get("row")
    col = label_cell.get("col")
    if not isinstance(row, int) or not isinstance(col, int):
        return None

    candidates: list[tuple[int, _Fragment]] = []
    for fragment in fragments:
        if fragment.block_id != label.block_id:
            continue
        cell = fragment.source_locator.get("table_cell")
        if not isinstance(cell, dict):
            continue
        candidate_row = cell.get("row")
        candidate_col = cell.get("col")
        if candidate_row == row and isinstance(candidate_col, int) and candidate_col > col:
            candidates.append((candidate_col, fragment))
    return min(candidates, default=(0, None), key=lambda pair: pair[0])[1]


def _normalized_alias(alias: str) -> str:
    return re.sub(r"[\s·]", "", alias)


def _source_role_for_alias(alias: str) -> CplSourceRole | None:
    value = _normalized_alias(alias)
    if value in {"지원대상및지원조건", "지원대상지원조건"}:
        return None
    if value in {"지원내용및지원규모", "지원내용지원규모"}:
        return None
    if value in {"연차별및내역사업별추진계획", "연차별내역사업별추진계획"}:
        return None
    if value == "지원대상":
        return CplSourceRole.TARGET
    if value == "지원조건":
        return CplSourceRole.CONDITION
    if value == "지원내용":
        return CplSourceRole.SUPPORT_CONTENT
    if value == "지원규모":
        return CplSourceRole.SUPPORT_SCALE
    if value in {"기대효과", "파급효과"}:
        return CplSourceRole.EXPECTED_EFFECT
    if value in {"성과지표", "성과목표"}:
        return CplSourceRole.PERFORMANCE_INDICATOR
    if value in {"수행기관"}:
        return CplSourceRole.DELIVERY_ORG
    if value in {"수행방식"}:
        return CplSourceRole.DELIVERY_METHOD
    if value in {
        "수행절차",
        "추진절차",
        "수행체계",
        "추진체계",
        "역할",
        "기관역할",
        "단계별역할",
    }:
        return CplSourceRole.DELIVERY_PROCEDURE
    if value == "연차별추진계획":
        return CplSourceRole.ANNUAL_PLAN
    if value in {"내역사업별추진계획", "내내역사업별추진계획"}:
        return CplSourceRole.SUBPROGRAM_PLAN
    return None


def _axis_for_alias(alias: str) -> CplAxisCode | None:
    value = _normalized_alias(alias)
    return {
        "지원대상": CplAxisCode.TARGET_GROUP,
        "수행기관": CplAxisCode.DELIVERY_ORG_NAME,
        "수행방식": CplAxisCode.DELIVERY_METHOD_TYPE,
        "수행절차": CplAxisCode.DELIVERY_PROCEDURE_STEP,
        "추진절차": CplAxisCode.DELIVERY_PROCEDURE_STEP,
    }.get(value)


def _normalized_occurrences(
    field_code: CplFieldCode,
    fragment: _Fragment,
    value: str,
    *,
    axis_code: CplAxisCode | None = None,
    source_role: CplSourceRole | None = None,
) -> list[CplOccurrence]:
    if field_code == CplFieldCode.IMPLEMENTATION_PLAN:
        return _implementation_plan_occurrences(
            fragment,
            value,
            source_role,
        )
    if field_code == CplFieldCode.BUSINESS_PERIOD:
        normalized = _normalize_period(value)
        return [_occurrence(fragment, value, normalized)]
    if field_code == CplFieldCode.BUDGET:
        amounts = _amount_values(value)
        if amounts:
            return [
                _occurrence(fragment, raw_text, normalized)
                for raw_text, normalized in amounts
            ]
        return [_occurrence(fragment, value, None)]
    if field_code == CplFieldCode.LEGAL_BASIS:
        return [_occurrence(fragment, value, _normalize_legal_basis(value))]
    if field_code == CplFieldCode.LINKED_POLICY:
        normalized = (
            None if value.strip() in _GENERIC_POLICY_VALUES else {"text": value}
        )
        return [_occurrence(fragment, value, normalized)]
    normalized = (
        _normalize_axis_value(axis_code, value, source_role)
        if axis_code is not None
        else {"text": value}
    )
    return [
        _occurrence(
            fragment,
            value,
            normalized,
            axis_code=axis_code,
            source_role=source_role,
        )
    ]


def _implementation_plan_occurrences(
    fragment: _Fragment,
    value: str,
    source_role: CplSourceRole | None,
) -> list[CplOccurrence]:
    if source_role == CplSourceRole.ANNUAL_PLAN:
        axis_code = (
            None
            if re.fullmatch(r"\s*(?:해당\s*없음|없음)\s*", value)
            else CplAxisCode.ANNUAL_PLAN_CONTENT
        )
        return [
            _occurrence(
                fragment,
                value,
                _normalize_axis_value(axis_code, value, source_role)
                if axis_code is not None
                else None,
                axis_code=axis_code,
                source_role=source_role,
            )
        ]

    occurrences: list[CplOccurrence] = []
    explicit_absence = bool(
        re.search(
            r"(?:내내역사업|내역사업).{0,12}(?:없음|해당\s*없음|미운영|미구성)",
            value,
        )
    )
    if explicit_absence or (
        source_role == CplSourceRole.SUBPROGRAM_PLAN
        and re.fullmatch(r"\s*(?:해당\s*없음|없음)\s*", value)
    ):
        occurrences.append(
            _occurrence(
                fragment,
                value,
                {"program_level_absent": True},
                axis_code=CplAxisCode.PROGRAM_LEVEL_ABSENT,
                source_role=source_role,
            )
        )
        return occurrences

    level_matches = list(_PROGRAM_LEVEL_PATTERN.finditer(value))
    for match in level_matches:
        raw_text = match.group(0)
        occurrences.append(
            _occurrence(
                fragment,
                raw_text,
                _normalize_axis_value(
                    CplAxisCode.PROGRAM_LEVEL,
                    raw_text,
                    CplSourceRole.SUBPROGRAM_PLAN,
                ),
                axis_code=CplAxisCode.PROGRAM_LEVEL,
                source_role=CplSourceRole.SUBPROGRAM_PLAN,
            )
        )
    remaining = _PROGRAM_LEVEL_PATTERN.sub("", value).strip(" \t:：-–—")
    if source_role == CplSourceRole.SUBPROGRAM_PLAN and remaining:
        occurrences.append(
            _occurrence(
                fragment,
                value,
                {"text": value},
                axis_code=CplAxisCode.SUBPROGRAM_PLAN_CONTENT,
                source_role=source_role,
            )
        )
    return occurrences or [
        _occurrence(
            fragment,
            value,
            {"text": value},
            source_role=source_role,
        )
    ]


def _item_from_rule_occurrences(
    field_code: CplFieldCode,
    occurrences: list[CplOccurrence],
) -> CplItem:
    if not occurrences:
        status = (
            CplStatus.NEEDS_CONFIRMATION
            if field_code in CPL_SEMANTIC_FIELDS
            else CplStatus.MISSING
        )
        reason_code = (
            "SEMANTIC_REVIEW_REQUIRED"
            if status == CplStatus.NEEDS_CONFIRMATION
            else "EXPLICIT_VALUE_NOT_FOUND"
        )
    elif any(occurrence.normalized_value is None for occurrence in occurrences):
        status = CplStatus.NEEDS_CONFIRMATION
        reason_code = "VALUE_NOT_SPECIFIC"
    elif field_code in CPL_SEMANTIC_FIELDS:
        status = CplStatus.NEEDS_CONFIRMATION
        reason_code = (
            "MINIMUM_LOGIC_REVIEW_REQUIRED"
            if field_code == CplFieldCode.BUSINESS_NEED
            else "SEMANTIC_REVIEW_REQUIRED"
        )
    elif field_code == CplFieldCode.BUSINESS_PERIOD and any(
        isinstance(occurrence.normalized_value, dict)
        and occurrence.normalized_value.get("end") is None
        and occurrence.normalized_value.get("continuing") is not True
        and occurrence.normalized_value.get("single_year") is not True
        for occurrence in occurrences
    ):
        status = CplStatus.NEEDS_CONFIRMATION
        reason_code = "PERIOD_PARTIAL"
    else:
        status = CplStatus.PRESENT
        reason_code = None

    return CplItem(
        field_code=field_code,
        status=status,
        occurrences=occurrences,
        reason_code=reason_code,
    )


def _normalize_period(value: str) -> dict | None:
    match = _PERIOD_PATTERN.search(value)
    if match is not None:
        return {
            "start": _year_month(match.group("start_year"), match.group("start_month")),
            "end": (
                _year_month(match.group("end_year"), match.group("end_month"))
                if match.group("end_year")
                else None
            ),
            "continuing": match.group("continuing") is not None,
        }
    single_year = _SINGLE_YEAR_PATTERN.search(value)
    if single_year is not None:
        return {
            "start": single_year.group("year"),
            "end": None,
            "continuing": False,
            "single_year": True,
        }
    return None


def _year_month(year: str, month: str | None) -> str:
    return f"{year}-{int(month):02d}" if month is not None else year


def _amount_values(value: str) -> list[tuple[str, dict]]:
    amounts: list[tuple[str, dict]] = []
    for match in _AMOUNT_PATTERN.finditer(value):
        raw_text = match.group(0).strip()
        if match.group("unit") is None and not raw_text.endswith("원"):
            continue
        try:
            number = Decimal(match.group("number").replace(",", ""))
        except InvalidOperation:
            continue
        won = number * _AMOUNT_MULTIPLIERS[match.group("unit")]
        if won != won.to_integral_value():
            continue
        amounts.append(
            (
                raw_text,
                {"amount_won": int(won), "currency": "KRW"},
            )
        )
    return amounts


def _normalize_axis_value(
    axis_code: CplAxisCode,
    raw_text: str,
    source_role: CplSourceRole | None,
) -> dict | None:
    if axis_code in {CplAxisCode.PER_COMPANY_LIMIT, CplAxisCode.TOTAL_SCALE}:
        amounts = _amount_values(raw_text)
        if len(amounts) != 1:
            return None
        return {
            "amount_won": amounts[0][1]["amount_won"],
            "unit": "KRW",
        }
    if axis_code in {CplAxisCode.SUBSIDY_RATE, CplAxisCode.SELF_BURDEN_RATE}:
        match = _RATIO_PATTERN.search(raw_text)
        if match is None:
            return None
        return {
            "ratio": float(Decimal(match.group("number")) / Decimal(100)),
            "unit": "PERCENT",
        }
    if axis_code == CplAxisCode.COMPANY_COUNT:
        match = _COMPANY_COUNT_PATTERN.search(raw_text)
        if match is None:
            return None
        return {
            "count": int(match.group("number").replace(",", "")),
            "unit": "COMPANY",
        }
    if axis_code == CplAxisCode.KPI_BASE_YEAR:
        match = _SINGLE_YEAR_PATTERN.search(raw_text)
        if match is None:
            return None
        return {"year": int(match.group("year")), "unit": "YEAR"}
    if axis_code == CplAxisCode.KPI_TARGET_VALUE:
        match = _NUMBER_PATTERN.search(raw_text)
        if match is None:
            return None
        number = match.group("number").replace(",", "")
        value: int | float = (
            float(f"{number}.{match.group('decimal')}")
            if match.group("decimal")
            else int(number)
        )
        return {"number": value, "unit": "NUMBER"}
    if axis_code == CplAxisCode.PROGRAM_LEVEL:
        match = _PROGRAM_LEVEL_PATTERN.search(raw_text)
        if match is None:
            return None
        return {
            "program_level": {
                "세부사업": "DETAIL",
                "내역사업": "SUBPROGRAM",
                "내내역사업": "SUBSUBPROGRAM",
            }[match.group("level")]
        }
    if axis_code == CplAxisCode.PROGRAM_LEVEL_ABSENT:
        explicit_absence = bool(
            re.search(
                r"(?:내내역사업|내역사업).{0,12}(?:없음|해당\s*없음|미운영|미구성)",
                raw_text,
            )
        )
        if source_role != CplSourceRole.SUBPROGRAM_PLAN and not explicit_absence:
            return None
        return {"program_level_absent": True}
    return {"text": raw_text}


def _normalize_legal_basis(value: str) -> dict | None:
    match = _LAW_PATTERN.search(value)
    if match is None:
        return None
    law_name = re.sub(r"\s+", " ", match.group("law")).strip()
    article = match.group("article")
    return {
        "law_name": law_name,
        "article": re.sub(r"\s+", "", article) if article else None,
    }


def _occurrence(
    fragment: _Fragment,
    raw_text: str,
    normalized_value: object | None,
    *,
    axis_code: CplAxisCode | None = None,
    source_role: CplSourceRole | None = None,
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=normalized_value,
        axis_code=axis_code,
        source_role=source_role,
        page_no=fragment.page_no,
        section_path=list(fragment.section_path),
        source_locator=dict(fragment.source_locator),
        block_id=fragment.block_id,
        extraction_method="RULE",
    )


def _deduplicate_occurrences(
    occurrences: list[CplOccurrence],
) -> list[CplOccurrence]:
    unique: list[CplOccurrence] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        key = _occurrence_key(occurrence)
        if key not in seen:
            seen.add(key)
            unique.append(occurrence)
    return unique


def _merge_occurrences(
    rule_occurrences: list[CplOccurrence],
    llm_occurrences: list[CplOccurrence],
) -> list[CplOccurrence]:
    concrete_llm_locations = {
        _occurrence_location_key(occurrence)
        for occurrence in llm_occurrences
        if occurrence.axis_code is not None
    }
    rules = [
        occurrence
        for occurrence in rule_occurrences
        if occurrence.axis_code is not None
        or _occurrence_location_key(occurrence) not in concrete_llm_locations
    ]
    return _deduplicate_occurrences([*rules, *llm_occurrences])


def _occurrence_key(occurrence: CplOccurrence) -> str:
    return json.dumps(
        {
            "page_no": occurrence.page_no,
            "block_id": occurrence.block_id,
            "source_locator": occurrence.source_locator,
            "raw_text": occurrence.raw_text,
            "axis_code": occurrence.axis_code,
            "source_role": occurrence.source_role,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _occurrence_location_key(occurrence: CplOccurrence) -> str:
    return json.dumps(
        {
            "page_no": occurrence.page_no,
            "block_id": occurrence.block_id,
            "source_locator": occurrence.source_locator,
            "raw_text": occurrence.raw_text,
            "source_role": occurrence.source_role,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _resolve_implementation_plan(items: list[CplItem]) -> None:
    plan = next(
        item for item in items if item.field_code == CplFieldCode.IMPLEMENTATION_PLAN
    )
    period = next(
        item for item in items if item.field_code == CplFieldCode.BUSINESS_PERIOD
    )
    annual_status = _annual_plan_status(period, plan)
    subprogram_status = _subprogram_plan_status(plan)
    statuses = {annual_status, subprogram_status}

    if CplStatus.MISSING in statuses:
        plan.status = CplStatus.MISSING
        plan.reason_code = "IMPLEMENTATION_PLAN_REQUIRED"
    elif CplStatus.NEEDS_CONFIRMATION in statuses:
        plan.status = CplStatus.NEEDS_CONFIRMATION
        plan.reason_code = "IMPLEMENTATION_PLAN_REVIEW_REQUIRED"
    elif statuses == {CplStatus.NOT_APPLICABLE}:
        plan.status = CplStatus.NOT_APPLICABLE
        plan.reason_code = None
    else:
        plan.status = CplStatus.PRESENT
        plan.reason_code = None


def _annual_plan_status(period: CplItem, plan: CplItem) -> CplStatus:
    periods = [
        occurrence.normalized_value
        for occurrence in period.occurrences
        if isinstance(occurrence.normalized_value, dict)
    ]
    if not periods:
        return CplStatus.NEEDS_CONFIRMATION
    if any(value.get("single_year") is True for value in periods):
        return CplStatus.NOT_APPLICABLE
    multi_year = any(
        value.get("continuing") is True
        or (
            isinstance(value.get("start"), str)
            and isinstance(value.get("end"), str)
            and value["start"][:4] != value["end"][:4]
        )
        for value in periods
    )
    if not multi_year:
        return CplStatus.NEEDS_CONFIRMATION
    return (
        CplStatus.PRESENT
        if any(
            occurrence.axis_code == CplAxisCode.ANNUAL_PLAN_CONTENT
            and occurrence.source_role == CplSourceRole.ANNUAL_PLAN
            for occurrence in plan.occurrences
        )
        else CplStatus.MISSING
    )


def _subprogram_plan_status(plan: CplItem) -> CplStatus:
    if any(
        occurrence.axis_code == CplAxisCode.PROGRAM_LEVEL_ABSENT
        and isinstance(occurrence.normalized_value, dict)
        and occurrence.normalized_value.get("program_level_absent") is True
        for occurrence in plan.occurrences
    ):
        return CplStatus.NOT_APPLICABLE

    levels = {
        occurrence.normalized_value.get("program_level")
        for occurrence in plan.occurrences
        if occurrence.axis_code == CplAxisCode.PROGRAM_LEVEL
        and isinstance(occurrence.normalized_value, dict)
        and occurrence.normalized_value.get("program_level")
    }
    has_content = any(
        occurrence.axis_code == CplAxisCode.SUBPROGRAM_PLAN_CONTENT
        and occurrence.source_role == CplSourceRole.SUBPROGRAM_PLAN
        for occurrence in plan.occurrences
    )
    if levels & {"SUBPROGRAM", "SUBSUBPROGRAM"}:
        return CplStatus.PRESENT if has_content else CplStatus.MISSING
    return CplStatus.NEEDS_CONFIRMATION


def _result_with_llm_failure(
    rule_result: CplResult,
    error_code: LlmFailureCode,
) -> CplResult:
    if error_code not in LLM_FAILURE_CODES:
        raise ValueError(f"Unsupported LLM failure code: {error_code}")

    items: list[CplItem] = []
    for item in rule_result.items:
        copied = item.model_copy(deep=True)
        if (
            copied.field_code in CPL_SEMANTIC_FIELDS
            and copied.status not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
        ):
            copied.status = CplStatus.NEEDS_CONFIRMATION
            copied.reason_code = error_code
        items.append(copied)

    warning = f"CPL semantic extraction incomplete: {error_code}"
    warnings = list(rule_result.warnings)
    if warning not in warnings:
        warnings.append(warning)
    return CplResult(
        ruleset_version=rule_result.ruleset_version,
        items=items,
        warnings=warnings,
        model_profile=rule_result.model_profile,
        prompt_version=rule_result.prompt_version,
    )
