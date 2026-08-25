import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.schemas.cpl import (
    CPL_FIELDS,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplSemanticResponse,
    CplStatus,
)
from app.schemas.parsed_document import ParsedDocument


DEFAULT_RULESET_VERSION = "cpl-alpha-v0.1"
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


@dataclass(frozen=True)
class _Fragment:
    text: str
    page_no: int | None
    section_path: list[str]
    source_locator: dict
    block_id: str


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
    valid_block_ids: set[str] | None = None,
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
            or (
                valid_block_ids is not None
                and occurrence.block_id not in valid_block_ids
            )
            for occurrence in candidate.occurrences
        ):
            return _result_with_llm_failure(rule_result, "LLM_INVALID_RESPONSE")

    merged_items: list[CplItem] = []
    for rule_item in rule_result.items:
        candidate = candidate_by_code.get(rule_item.field_code)
        if candidate is None:
            merged_items.append(rule_item.model_copy(deep=True))
            continue

        occurrences = _deduplicate_occurrences(
            [*rule_item.occurrences, *candidate.occurrences]
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

    fragments = _document_fragments(document)
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
            if not occurrence.raw_text.strip() or not occurrence.block_id.strip():
                raise ValueError("LLM evidence must not be blank")
            matches = [
                fragment
                for fragment in fragments
                if fragment.block_id == occurrence.block_id
                and occurrence.raw_text in fragment.text
            ]
            exact_matches = [
                fragment for fragment in matches if fragment.text == occurrence.raw_text
            ]
            if exact_matches:
                matches = exact_matches
            if len(matches) != 1:
                raise ValueError("LLM evidence cannot be grounded uniquely")
            fragment = matches[0]
            occurrences.append(
                CplOccurrence(
                    raw_text=occurrence.raw_text,
                    normalized_value=occurrence.normalized_value,
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
            for cell in cells:
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
                    text=block.text.strip(),
                    page_no=block.page_no,
                    section_path=list(block.section_path),
                    source_locator=dict(block.source_locator),
                    block_id=block.block_id,
                )
            )
    return fragments


def _request_type_item(fragments: list[_Fragment]) -> CplItem:
    occurrences: list[CplOccurrence] = []
    for fragment in fragments:
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


def _labeled_occurrences(
    field_code: CplFieldCode,
    fragments: list[_Fragment],
) -> list[CplOccurrence]:
    aliases = _LABEL_ALIASES[field_code]
    occurrences: list[CplOccurrence] = []
    label_only_fragments: list[_Fragment] = []

    for fragment in fragments:
        for line in filter(None, (line.strip() for line in fragment.text.splitlines())):
            match = _label_match(line, aliases)
            if match is None:
                continue
            value = match.group("value").strip(" \t:：-–—")
            if not value:
                label_only_fragments.append(fragment)
                continue
            occurrences.extend(_normalized_occurrences(field_code, fragment, value))

    for label_fragment in label_only_fragments:
        adjacent = _adjacent_table_value(label_fragment, fragments)
        if adjacent is None:
            occurrences.append(_occurrence(label_fragment, label_fragment.text, None))
        else:
            occurrences.extend(
                _normalized_occurrences(field_code, adjacent, adjacent.text)
            )
    return _deduplicate_occurrences(occurrences)


def _label_match(line: str, aliases: tuple[str, ...]) -> re.Match[str] | None:
    alternatives = "|".join(
        re.escape(alias).replace(r"\ ", r"\s*")
        for alias in sorted(aliases, key=len, reverse=True)
    )
    return re.match(
        rf"{_LINE_PREFIX}(?:{alternatives})(?:\s*[:：])?\s*(?P<value>.*)$",
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


def _normalized_occurrences(
    field_code: CplFieldCode,
    fragment: _Fragment,
    value: str,
) -> list[CplOccurrence]:
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
    return [_occurrence(fragment, value, {"text": value})]


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
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=normalized_value,
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
        key = json.dumps(
            occurrence.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
        )
        if key not in seen:
            seen.add(key)
            unique.append(occurrence)
    return unique


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
