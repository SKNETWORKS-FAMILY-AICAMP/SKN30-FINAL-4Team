import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date
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


logger = logging.getLogger(__name__)

DEFAULT_RULESET_VERSION = "cpl-alpha-v0.3"
LlmFailureCode = Literal[
    "LLM_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_INVALID_RESPONSE",
]
LLM_FAILURE_CODES = frozenset(
    {"LLM_UNAVAILABLE", "LLM_TIMEOUT", "LLM_INVALID_RESPONSE"}
)

# 근거를 잃은 원인을 구분한다. 파서가 라벨 여러 개를 한 조각에 담아 귀속하지
# 못한 것과, 모델이 원문에 없는 말을 인용한 것은 다른 사건이다. 둘을 같은
# 코드로 적으면 Rule·LLM 비교 지표가 우리 결함을 LLM 실패로 센다.
CPL_INCOMPLETE_REASON_CODES = frozenset(
    {
        *LLM_FAILURE_CODES,
        "EVIDENCE_OWNERSHIP_UNRESOLVED",
        "LLM_REASON_CODE_MISSING",
    }
)
_LLM_FAULT_DROPS = frozenset(
    {
        "BLANK_EVIDENCE",
        "REF_NOT_FOUND",
        "NOT_VERBATIM",
        "FIELD_NOT_ALLOWED",
        "AXIS_NOT_ALLOWED",
    }
)
_STRUCTURAL_DROPS = frozenset({"FIELD_OWNERSHIP_UNRESOLVED"})
# 문서가 스스로 공란·부재라고 밝힌 값을 걸러낸 것은 어느 쪽 실패도 아니다.
_BENIGN_DROPS = frozenset({"EXPLICIT_MISSING_VALUE", "EXPLICIT_ABSENCE_VALUE"})
_LLM_FAULT_CONTRACT_ERRORS = frozenset(
    {"PRESENT_WITHOUT_EVIDENCE", "MISSING_WITH_EVIDENCE"}
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

# 명시적 부재 판정은 라벨이 아니라 값에 내려야 한다. LLM 이 라벨을 포함한
# 줄을 인용할 수 있어 값만 떼어내는 데 쓴다.
_ALL_LABEL_ALIASES: tuple[str, ...] = tuple(
    alias for aliases in _LABEL_ALIASES.values() for alias in aliases
)

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
_DATE_TOKEN_PATTERN = re.compile(
    r"(?P<ymd_year>20\d{2})\s*[./-]\s*(?P<ymd_month>\d{1,2})"
    r"\s*[./-]\s*(?P<ymd_day>\d{1,2})\s*\.?"
    r"|(?P<ym_year>20\d{2})\s*[./-]\s*(?P<ym_month>\d{1,2})\s*\.?"
    r"|(?P<kr_year>20\d{2})\s*년(?:\s*(?P<kr_month>\d{1,2})\s*월"
    r"(?:\s*(?P<kr_day>\d{1,2})\s*일)?)?"
    r"|(?P<short_quote>['’])(?P<short_year>\d{2})\s*년"
    r"|(?<!\d)(?P<bare_year>20\d{2})(?!\d)"
)
_SINGLE_YEAR_PATTERN = re.compile(r"(?<!\d)(?P<year>20\d{2})\s*년")
_AMOUNT_PATTERN = re.compile(
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<unit>조|억|천만|백만|십만|만|천|백|십)?\s*원?"
)
_ARTICLE_PATTERN = r"제\s*\d+\s*조(?:의\s*\d+)?"
_QUOTED_LAW_PATTERN = re.compile(
    rf"「(?P<law>[^」]+)」\s*(?P<article>{_ARTICLE_PATTERN})?"
)
_UNQUOTED_LAW_PATTERN = re.compile(
    rf"(?P<law>[가-힣A-Za-z0-9·ㆍ ]{{1,80}}?"
    rf"(?:특별법|법률|조례|규칙|법))\s*(?P<article>{_ARTICLE_PATTERN})?"
)
_GENERIC_LAW_REFERENCE_PATTERN = re.compile(
    r"^(?:(?:관련|해당|관계|상위|기타)\s*)?법(?:률)?$"
)
_RATIO_PATTERN = re.compile(r"(?<![\d.])(?P<number>\d+(?:\.\d+)?)\s*%")
_COMPANY_COUNT_PATTERN = re.compile(
    r"(?<!\d)(?P<number>\d{1,3}(?:,\d{3})*|\d+)\s*(?:개\s*사|기업|개\s*업체)"
)
_CONDITION_PATTERNS: tuple[tuple[CplAxisCode, re.Pattern[str]], ...] = (
    (
        CplAxisCode.COND_BUSINESS_AGE,
        re.compile(
            r"(?:업력|창업)\s*\d+(?:\.\d+)?\s*년\s*"
            r"(?:이내|미만|이상|초과|이하)"
            r"(?:\s*\d+(?:\.\d+)?\s*년\s*"
            r"(?:이내|미만|이상|초과|이하))?"
        ),
    ),
    (
        CplAxisCode.COND_REVENUE,
        re.compile(
            r"(?:최근\s*\d+\s*개년\s*)?(?:연평균\s*)?매출(?:액)?\s*"
            r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
            r"(?:조|억|천만|백만|십만|만|천)?\s*원?\s*"
            r"(?:이내|미만|이상|초과|이하)"
            r"(?:\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
            r"(?:조|억|천만|백만|십만|만|천)?\s*원?\s*"
            r"(?:이내|미만|이상|초과|이하))?"
        ),
    ),
    (
        CplAxisCode.COND_HEADCOUNT,
        re.compile(
            r"(?:상시\s*)?(?:근로자|종사자)\s*\d{1,3}(?:,\d{3})*\s*명\s*"
            r"(?:이내|미만|이상|초과|이하)"
            r"(?:\s*\d{1,3}(?:,\d{3})*\s*명\s*"
            r"(?:이내|미만|이상|초과|이하))?"
        ),
    ),
)
_NUMBER_PATTERN = re.compile(
    r"(?<!\d)(?P<number>\d{1,3}(?:,\d{3})*|\d+)(?:\.(?P<decimal>\d+))?"
)
_PROGRAM_LEVEL_PATTERN = re.compile(r"(?P<level>내내역사업|내역사업|세부사업)")
_EXPLICIT_MISSING_PATTERN = re.compile(
    r"(?:공란|미기재|미작성)"
    r"(?:\s*[/,·]\s*(?:공란|미기재|미작성))*"
)
_EXPLICIT_ABSENCE_PATTERN = re.compile(
    r"(?:없음|해당\s*없음|별도\s*조건\s*없음)"
)
_AGE_BOUNDARY_PATTERN = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*년\s*"
    r"(?P<operator>이내|미만|이상|초과|이하)"
)
_REVENUE_BOUNDARY_PATTERN = re.compile(
    r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?P<unit>조|억|천만|백만|십만|만|천)?\s*원?\s*"
    r"(?P<operator>이내|미만|이상|초과|이하)"
)
_HEADCOUNT_BOUNDARY_PATTERN = re.compile(
    r"(?P<number>\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?\s*명\s*"
    r"(?P<operator>이내|미만|이상|초과|이하)"
)
# 성과지표 목표값은 배수어와 단위를 함께 읽어야 한다. 숫자만 떼어내면
# 10만건이 10 으로 기록되어 실패가 아니라 틀린 값이 남는다.
_KPI_VALUE_PATTERN = re.compile(
    r"(?P<number>\d{1,3}(?:,\d{3})*|\d+)(?:\.(?P<decimal>\d+))?"
    r"\s*(?P<scale>조|억|천만|백만|십만|만|천|백|십)?"
    r"\s*(?P<unit>%|개사|명|건|원)?"
)
_KPI_UNITS = {
    "%": "PERCENT",
    "개사": "COMPANY",
    "명": "PERSON",
    "건": "CASE",
    "원": "KRW",
}


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
        CplFieldCode.LINKED_POLICY,
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
    CplFieldCode.LINKED_POLICY: frozenset(
        {CplAxisCode.LINKED_POLICY_IDENTIFIER}
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
class _TextScope:
    start: int
    end: int
    field_codes: frozenset[CplFieldCode]
    source_role: CplSourceRole | None = None


@dataclass(frozen=True)
class _Fragment:
    evidence_ref: str
    text: str
    page_no: int | None
    section_path: list[str]
    source_locator: dict
    block_id: str
    source_role: CplSourceRole | None = None
    field_codes: frozenset[CplFieldCode] = frozenset()
    scopes: tuple[_TextScope, ...] = ()


def evaluate_cpl_rules(
    document: ParsedDocument,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
) -> CplResult:
    """Extract only explicit, reproducible CPL evidence from a parsed document."""
    # Rule extraction and LLM grounding must agree on label ownership.
    fragments = semantic_fragments(document)
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
    requested_fields: set[CplFieldCode] | None = None,
    additional_warnings: list[str] | None = None,
) -> CplResult:
    """Merge grounded LLM facts without replacing deterministic Rule evidence.

    ``requested_fields`` limits which items a failure may degrade.  The optional
    CPL/FIT recheck asks about a few fields only, and a failure there must not
    rewrite fields the first pass already settled.
    """
    if llm_error is not None:
        return _result_with_llm_failure(rule_result, llm_error, requested_fields)
    if llm_items is None:
        return rule_result.model_copy(deep=True)

    candidate_by_code: dict[CplFieldCode, CplItem] = {}
    invalid_fields: set[CplFieldCode] = set()
    for candidate in llm_items:
        if candidate.field_code in candidate_by_code:
            invalid_fields.add(candidate.field_code)
            continue
        candidate_by_code[candidate.field_code] = candidate
    for candidate in llm_items:
        if candidate.field_code in invalid_fields:
            continue
        if candidate.status == CplStatus.PARSE_FAILED:
            invalid_fields.add(candidate.field_code)
        elif candidate.status == CplStatus.PRESENT and not candidate.occurrences:
            invalid_fields.add(candidate.field_code)
        elif any(
            occurrence.extraction_method != "LLM"
            for occurrence in candidate.occurrences
        ):
            invalid_fields.add(candidate.field_code)

    merged_items: list[CplItem] = []
    for rule_item in rule_result.items:
        candidate = candidate_by_code.get(rule_item.field_code)
        if candidate is None or rule_item.field_code in invalid_fields:
            copied = rule_item.model_copy(deep=True)
            if (
                rule_item.field_code in invalid_fields
                and rule_item.status
                not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
            ):
                copied.status = CplStatus.NEEDS_CONFIRMATION
                copied.reason_code = "LLM_INVALID_RESPONSE"
            merged_items.append(copied)
            continue

        occurrences = _merge_occurrences(
            rule_item.occurrences,
            candidate.occurrences,
        )
        rule_is_conclusive = rule_item.status in {
            CplStatus.PRESENT,
            CplStatus.NOT_APPLICABLE,
        }
        candidate_is_invalid = (
            candidate.reason_code in CPL_INCOMPLETE_REASON_CODES
        )
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
                    else candidate.reason_code
                    if candidate_is_invalid
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
    invalid_unresolved_fields = {
        rule_item.field_code: candidate.reason_code
        for rule_item in rule_result.items
        if rule_item.status not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
        and (candidate := candidate_by_code.get(rule_item.field_code)) is not None
        and candidate.reason_code in CPL_INCOMPLETE_REASON_CODES
    }
    for item in merged_items:
        if item.field_code in invalid_unresolved_fields:
            item.status = CplStatus.NEEDS_CONFIRMATION
            item.reason_code = invalid_unresolved_fields[item.field_code]
    warnings = list(rule_result.warnings)
    for warning in additional_warnings or []:
        if warning not in warnings:
            warnings.append(warning)
    for field_code in sorted(invalid_fields, key=lambda code: code.value):
        warning = (
            "CPL semantic field incomplete: "
            f"{field_code.value}:LLM_INVALID_RESPONSE"
        )
        if warning not in warnings:
            warnings.append(warning)
    for code in sorted(
        {
            candidate.reason_code
            for candidate in llm_items
            if candidate.reason_code in CPL_INCOMPLETE_REASON_CODES
        }
    ):
        warning = f"CPL semantic extraction incomplete: {code}"
        if warning not in warnings:
            warnings.append(warning)
    return CplResult(
        ruleset_version=rule_result.ruleset_version,
        items=merged_items,
        warnings=warnings,
        model_profile=rule_result.model_profile,
        prompt_version=rule_result.prompt_version,
    )


def ground_llm_response(
    document: ParsedDocument,
    response: CplSemanticResponse,
    expected_fields: set[CplFieldCode],
    *,
    warning_sink: list[str] | None = None,
) -> list[CplItem]:
    """Replace model-provided lineage with lineage verified against parser output."""
    if not expected_fields.issubset(CPL_SEMANTIC_FIELDS):
        raise ValueError("LLM response contains a Rule-only CPL field")
    fragments = semantic_fragments(document)
    fragment_by_ref = {fragment.evidence_ref: fragment for fragment in fragments}
    if len(fragment_by_ref) != len(fragments):
        raise ValueError("Parser fragments do not have unique evidence references")
    response_by_field: dict[CplFieldCode, list] = {}
    for item in response.items:
        response_by_field.setdefault(item.field_code, []).append(item)
    unexpected = set(response_by_field) - expected_fields
    if unexpected:
        warning = (
            "CPL semantic response ignored unexpected field(s): "
            + ",".join(sorted(field.value for field in unexpected))
        )
        if warning_sink is not None:
            warning_sink.append(warning)
        logger.warning(
            "CPL response contained %s unexpected field(s)",
            len(unexpected),
        )
    grounded: list[CplItem] = []
    for field_code in CPL_FIELDS:
        if field_code not in expected_fields:
            continue
        candidates = response_by_field.get(field_code, [])
        if len(candidates) != 1:
            logger.warning(
                "CPL response item invalid field=%s reason=%s",
                field_code.value,
                "FIELD_MISSING" if not candidates else "FIELD_DUPLICATED",
            )
            grounded.append(
                CplItem(
                    field_code=field_code,
                    status=CplStatus.NEEDS_CONFIRMATION,
                    reason_code="LLM_INVALID_RESPONSE",
                )
            )
            continue
        item = candidates[0]
        contract_error = None
        if item.status == "PRESENT" and not item.occurrences:
            contract_error = "PRESENT_WITHOUT_EVIDENCE"
        elif item.status == "MISSING" and item.occurrences:
            contract_error = "MISSING_WITH_EVIDENCE"
        elif item.status == "NEEDS_CONFIRMATION" and not item.reason_code:
            contract_error = "CONFIRMATION_REASON_MISSING"

        occurrences: list[CplOccurrence] = []
        dropped: list[str] = []
        # 사유만 남기면 어떤 축이 왜 거부됐는지 알 수 없다. 오분류를 좁히려면
        # 거부된 축과 근거 위치가 함께 있어야 한다.
        dropped_detail: list[str] = []
        for occurrence in item.occurrences:
            # 근거 하나가 접지에 실패해도 나머지는 살린다. 예전에는 예외를
            # 올려 13개 항목 전체를 버렸고, 축을 많이 태깅할수록 하나가
            # 어긋날 확률이 커져 커버리지를 올리려는 시도가 실패율을 올렸다.
            if not occurrence.raw_text.strip() or not occurrence.evidence_ref.strip():
                dropped.append("BLANK_EVIDENCE")
                dropped_detail.append(
                    "BLANK_EVIDENCE:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            fragment = fragment_by_ref.get(occurrence.evidence_ref)
            if fragment is None:
                dropped.append("REF_NOT_FOUND")
                dropped_detail.append(
                    "REF_NOT_FOUND:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            if occurrence.raw_text not in fragment.text:
                dropped.append("NOT_VERBATIM")
                dropped_detail.append(
                    "NOT_VERBATIM:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            owned_fields, cited_role = _evidence_context(fragment, occurrence.raw_text)
            if owned_fields and item.field_code not in owned_fields:
                dropped.append("FIELD_NOT_ALLOWED")
                dropped_detail.append(
                    "FIELD_NOT_ALLOWED:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            if not owned_fields:
                dropped.append("FIELD_OWNERSHIP_UNRESOLVED")
                dropped_detail.append(
                    "FIELD_OWNERSHIP_UNRESOLVED:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            if _is_explicit_missing_text(occurrence.raw_text):
                dropped.append("EXPLICIT_MISSING_VALUE")
                dropped_detail.append(
                    "EXPLICIT_MISSING_VALUE:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            if (
                item.field_code == CplFieldCode.TARGET_AND_CONDITIONS
                and _is_explicit_absence_text(occurrence.raw_text)
            ):
                dropped.append("EXPLICIT_ABSENCE_VALUE")
                dropped_detail.append(
                    "EXPLICIT_ABSENCE_VALUE:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            if occurrence.axis_code not in CPL_FIELD_AXES[item.field_code]:
                dropped.append("AXIS_NOT_ALLOWED")
                dropped_detail.append(
                    "AXIS_NOT_ALLOWED:"
                    f"{occurrence.axis_code}@{occurrence.evidence_ref}"
                )
                continue
            source_role = cited_role
            if source_role is None and len(fragment.field_codes) == 1:
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
            source_locator = dict(fragment.source_locator)
            span = _cited_span(fragment, occurrence.raw_text)
            if span is not None:
                source_locator.update(span)
            occurrences.append(
                CplOccurrence(
                    raw_text=occurrence.raw_text,
                    normalized_value=normalized_value,
                    axis_code=occurrence.axis_code,
                    source_role=source_role,
                    page_no=fragment.page_no,
                    section_path=list(fragment.section_path),
                    source_locator=source_locator,
                    block_id=fragment.block_id,
                    extraction_method="LLM",
                )
            )
        status = CplStatus(item.status)
        reason_code = item.reason_code
        incomplete = _incomplete_reason(contract_error, dropped)
        if incomplete is None and status == CplStatus.PRESENT and not occurrences:
            incomplete = "LLM_INVALID_RESPONSE"
        if incomplete is not None:
            # 유효 근거는 보존하되 이 항목의 LLM 검토가 완전했다고 주장하지
            # 않는다. Rule 이 이미 확정한 상태는 merge 단계에서 유지된다.
            status = CplStatus.NEEDS_CONFIRMATION
            reason_code = incomplete
        if dropped:
            logger.warning(
                "CPL evidence dropped field=%s kept=%s dropped=%s detail=%s",
                item.field_code.value,
                len(occurrences),
                len(dropped),
                sorted(set(dropped_detail)),
            )
        if contract_error:
            logger.warning(
                "CPL response item invalid field=%s reason=%s",
                item.field_code.value,
                contract_error,
            )
        grounded.append(
            CplItem(
                field_code=item.field_code,
                status=status,
                occurrences=_deduplicate_occurrences(occurrences),
                reason_code=reason_code,
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
                    if key not in {"text", "segments", "paragraphs"}
                }
                segments = cell.get("segments")
                if isinstance(segments, list) and len(segments) > 1:
                    emitted_segment = False
                    segment_texts: list[str] = []
                    for segment_index, segment in enumerate(segments):
                        if not isinstance(segment, dict):
                            continue
                        segment_text = segment.get("text")
                        if not isinstance(segment_text, str) or not segment_text.strip():
                            continue
                        emitted_segment = True
                        segment_texts.append(segment_text)
                        cell_ref = _cell_evidence_ref(
                            block.block_id,
                            cell_locator,
                            cell_index,
                        )
                        segment_locator = {
                            **cell_locator,
                            "segment_index": segment.get(
                                "segment_index", segment_index
                            ),
                        }
                        fragments.append(
                            _Fragment(
                                evidence_ref=f"{cell_ref}:segment:{segment_index}",
                                text=segment_text.strip(),
                                page_no=block.page_no,
                                section_path=list(block.section_path),
                                source_locator={
                                    **base_locator,
                                    "table_cell": segment_locator,
                                },
                                block_id=block.block_id,
                            )
                        )
                    if emitted_segment and "".join(segment_texts) == text:
                        continue
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
    """Attach offset scopes proven by labels, section paths, or table adjacency."""
    fragments = _document_fragments(document)
    outer_fields: dict[str, set[CplFieldCode]] = {
        fragment.evidence_ref: set() for fragment in fragments
    }
    outer_roles: dict[str, set[CplSourceRole]] = {
        fragment.evidence_ref: set() for fragment in fragments
    }

    # A table label cell owns the adjacent value cell.  This is an ancestor
    # scope: inner labels in the value cell add field ownership instead of
    # deleting container ownership such as NEW_OR_CHANGED_CONTENT.
    for fragment in fragments:
        stripped = fragment.text.strip()
        for field_code, match in _line_label_matches(stripped):
            if match.group("value").strip(" \t:：-–—"):
                continue
            if stripped.count("\n"):
                continue
            target = _adjacent_table_value(fragment, fragments)
            if target is None:
                continue
            outer_fields[target.evidence_ref].add(field_code)
            role = _source_role_for_alias(match.group("label"))
            if role is not None:
                outer_roles[target.evidence_ref].add(role)

    # Some HWPX cells flatten visual lines into inline segments.  A label-only
    # segment owns the following unlabeled segments until the next label in the
    # same cell.  Cell-level ownership from an adjacent container label applies
    # to every segment in that value cell.
    segment_cells: dict[tuple[str, int, int], list[_Fragment]] = {}
    for fragment in fragments:
        cell = fragment.source_locator.get("table_cell")
        if not isinstance(cell, dict):
            continue
        row = cell.get("row")
        col = cell.get("col")
        segment_index = cell.get("segment_index")
        if all(isinstance(value, int) for value in (row, col, segment_index)):
            segment_cells.setdefault((fragment.block_id, row, col), []).append(
                fragment
            )

    for siblings in segment_cells.values():
        cell_fields = {
            field
            for fragment in siblings
            for field in outer_fields[fragment.evidence_ref]
        }
        cell_roles = {
            role
            for fragment in siblings
            for role in outer_roles[fragment.evidence_ref]
        }
        active_fields: set[CplFieldCode] = set()
        active_roles: set[CplSourceRole] = set()
        for fragment in sorted(
            siblings,
            key=lambda item: item.source_locator["table_cell"]["segment_index"],
        ):
            outer_fields[fragment.evidence_ref].update(cell_fields)
            matches = _line_label_matches(fragment.text.strip())
            has_known_label = any(
                _label_match(fragment.text.strip(), aliases) is not None
                for aliases in _LABEL_ALIASES.values()
            )
            if has_known_label:
                active_fields = {field for field, _match in matches}
                active_roles = {
                    role
                    for _field, match in matches
                    if (role := _source_role_for_alias(match.group("label")))
                    is not None
                }
            else:
                outer_fields[fragment.evidence_ref].update(active_fields)
                outer_roles[fragment.evidence_ref].update(
                    active_roles or cell_roles
                )

    resolved: list[_Fragment] = []
    for fragment in fragments:
        scopes = list(_local_text_scopes(fragment.text))
        fields = set(outer_fields[fragment.evidence_ref])
        roles = set(outer_roles[fragment.evidence_ref])

        for part in fragment.section_path:
            for field_code, match in _line_label_matches(part.strip()):
                fields.add(field_code)
                role = _source_role_for_alias(match.group("label"))
                if role is not None:
                    roles.add(role)

        if fields:
            scopes.insert(
                0,
                _TextScope(
                    start=0,
                    end=len(fragment.text),
                    field_codes=frozenset(fields),
                    source_role=(next(iter(roles)) if len(roles) == 1 else None),
                ),
            )

        all_fields = set(fields)
        all_roles = set(roles)
        for scope in scopes:
            all_fields.update(scope.field_codes)
            if scope.source_role is not None:
                all_roles.add(scope.source_role)
        resolved.append(
            replace(
                fragment,
                source_role=(
                    next(iter(all_roles)) if len(all_roles) == 1 else None
                ),
                field_codes=frozenset(all_fields),
                scopes=tuple(scopes),
            )
        )
    return resolved


def _evidence_context(
    fragment: _Fragment,
    raw_text: str,
) -> tuple[frozenset[CplFieldCode], CplSourceRole | None]:
    """Resolve every cited occurrence; repeated text must have one context."""
    contexts: list[tuple[frozenset[CplFieldCode], CplSourceRole | None]] = []
    start = 0
    while (position := fragment.text.find(raw_text, start)) >= 0:
        end = position + len(raw_text)
        containing = [
            scope
            for scope in fragment.scopes
            if scope.start <= position and end <= scope.end
        ]
        fields = frozenset(
            field
            for scope in containing
            for field in scope.field_codes
        )
        roles = [
            scope
            for scope in containing
            if scope.source_role is not None
        ]
        role = (
            min(roles, key=lambda scope: scope.end - scope.start).source_role
            if roles
            else None
        )
        contexts.append((fields, role))
        start = position + max(1, len(raw_text))

    if not contexts or any(context != contexts[0] for context in contexts[1:]):
        return frozenset(), None
    return contexts[0]


def _cited_span(fragment: _Fragment, raw_text: str) -> dict | None:
    """인용 구간이 조각 안에서 어디인지 돌려준다.

    같은 사실끼리 묶으려면 어느 줄의 어느 자리에서 나왔는지가 있어야 한다.
    지표 하나가 이름·목표값·기준연도를 갖는 구조를 지금 계약은 표현하지
    못하는데, 그 묶음 규칙을 정하려면 먼저 좌표가 보존돼 있어야 한다.

    좌표는 줄 번호와 오프셋이라 판단이 필요 없고 같은 문서면 항상 같은 값이
    나온다. 그래서 LLM 에게 묻지 않고 서버가 계산한다.

    같은 구간이 조각 안에 두 번 이상 나오면 어느 쪽인지 정할 수 없으므로
    좌표를 남기지 않는다. 어느 쪽을 고를지는 아직 정해진 규칙이 없다.
    """

    first = fragment.text.find(raw_text)
    if first < 0 or fragment.text.find(raw_text, first + max(1, len(raw_text))) >= 0:
        return None
    return {
        "line_index": fragment.text.count("\n", 0, first),
        "span_start": first,
        "span_end": first + len(raw_text),
    }


def _local_text_scopes(text: str) -> tuple[_TextScope, ...]:
    markers: list[tuple[int, frozenset[CplFieldCode], CplSourceRole | None]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        stripped = raw_line.strip()
        if stripped:
            leading = len(raw_line) - len(raw_line.lstrip())
            matches = _line_label_matches(stripped)
            if matches:
                fields = frozenset(field for field, _match in matches)
                roles = {
                    role
                    for _field, match in matches
                    if (role := _source_role_for_alias(match.group("label")))
                    is not None
                }
                markers.append(
                    (
                        offset + leading,
                        fields,
                        next(iter(roles)) if len(roles) == 1 else None,
                    )
                )
        offset += len(raw_line)

    return tuple(
        _TextScope(
            start=start,
            end=markers[index + 1][0] if index + 1 < len(markers) else len(text),
            field_codes=fields,
            source_role=role,
        )
        for index, (start, fields, role) in enumerate(markers)
    )


def _line_label_matches(
    line: str,
) -> list[tuple[CplFieldCode, re.Match[str]]]:
    matches: list[tuple[CplFieldCode, re.Match[str]]] = []
    for field_code in CPL_SEMANTIC_FIELDS:
        match = _label_match(line, _LABEL_ALIASES[field_code])
        if match is not None:
            matches.append((field_code, match))
    return matches


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
        offset = 0
        for raw_line in fragment.text.splitlines(keepends=True):
            line = raw_line.strip()
            if not line:
                offset += len(raw_line)
                continue
            match = _label_match(line, aliases)
            if match is None:
                offset += len(raw_line)
                continue
            value = match.group("value").strip(" \t:：-–—")
            source_role = _source_role_for_alias(match.group("label"))
            axis_code = _axis_for_alias(match.group("label"))
            if not value:
                leading = len(raw_line) - len(raw_line.lstrip())
                value = _scope_continuation(
                    fragment,
                    field_code,
                    offset + leading,
                    offset + len(raw_line),
                )
                if not value:
                    label_only_fragments.append(
                        (replace(fragment, source_role=source_role), axis_code)
                    )
                    offset += len(raw_line)
                    continue
            if _is_explicit_missing_text(value):
                offset += len(raw_line)
                continue
            if (
                field_code == CplFieldCode.TARGET_AND_CONDITIONS
                and source_role == CplSourceRole.CONDITION
                and _is_explicit_absence_text(value)
            ):
                occurrences.append(
                    _occurrence(
                        fragment,
                        value,
                        {"explicit_absence": True},
                        source_role=source_role,
                    )
                )
                offset += len(raw_line)
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
            offset += len(raw_line)

    for label_fragment, axis_code in label_only_fragments:
        adjacent = _adjacent_table_value(label_fragment, fragments)
        if adjacent is None:
            continue
        elif _is_explicit_missing_text(adjacent.text):
            continue
        elif (
            field_code == CplFieldCode.TARGET_AND_CONDITIONS
            and label_fragment.source_role == CplSourceRole.CONDITION
            and _is_explicit_absence_text(adjacent.text)
        ):
            occurrences.append(
                _occurrence(
                    adjacent,
                    adjacent.text,
                    {"explicit_absence": True},
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


def _scope_continuation(
    fragment: _Fragment,
    field_code: CplFieldCode,
    label_start: int,
    value_start: int,
) -> str:
    """Return a label-only line's value up to the next local label."""
    candidates = [
        scope
        for scope in fragment.scopes
        if scope.start == label_start and field_code in scope.field_codes
    ]
    if not candidates:
        return ""
    scope = min(candidates, key=lambda candidate: candidate.end - candidate.start)
    if value_start >= scope.end:
        return ""
    return fragment.text[value_start : scope.end].strip(" \t\r\n:：-–—")


def _incomplete_reason(
    contract_error: str | None,
    dropped: list[str],
) -> str | None:
    """근거를 잃은 원인을 하나로 좁힌다. 심각한 쪽이 이긴다.

    무엇이 잘못됐는지를 구분해 두지 않으면 파서 조각의 귀속 실패까지
    LLM 실패로 집계되어, 어느 수단이 실제로 흔들리는지 볼 수 없다.
    """

    reasons = set(dropped)
    if contract_error in _LLM_FAULT_CONTRACT_ERRORS or reasons & _LLM_FAULT_DROPS:
        return "LLM_INVALID_RESPONSE"
    if reasons & _STRUCTURAL_DROPS:
        return "EVIDENCE_OWNERSHIP_UNRESOLVED"
    if contract_error == "CONFIRMATION_REASON_MISSING":
        return "LLM_REASON_CODE_MISSING"
    if reasons - _BENIGN_DROPS:
        return "LLM_INVALID_RESPONSE"
    return None


def _strip_leading_label(value: str) -> str:
    """앞머리의 라벨을 떼고 값만 남긴다.

    Rule 은 라벨 뒤 값만 보지만 LLM 은 `○ 수행기관 : 미기재` 처럼 라벨을 포함한
    줄을 인용할 수 있다. 같은 사실인지 비교하려면 양쪽을 값으로 맞춰야 한다.
    """

    candidate = value.strip()
    label = _label_match(candidate, _ALL_LABEL_ALIASES)
    if label is not None:
        candidate = label.group("value").strip()
    return candidate


def _absence_marker(value: str) -> str:
    """인용 구간에서 부재 표기만 남긴다.

    표기 목록을 늘리는 대신 검사 대상을 값으로 맞춘다.
    """

    candidate = _strip_leading_label(value)
    if len(candidate) >= 2 and (candidate[0], candidate[-1]) in {
        ("(", ")"),
        ("[", "]"),
        ("{", "}"),
    }:
        candidate = candidate[1:-1].strip()
    return re.split(r"\s*(?:->|→|⇒)\s*", candidate, maxsplit=1)[0]


def _is_explicit_missing_text(value: str) -> bool:
    return _EXPLICIT_MISSING_PATTERN.fullmatch(_absence_marker(value)) is not None


def _is_explicit_absence_text(value: str) -> bool:
    return _EXPLICIT_ABSENCE_PATTERN.fullmatch(_absence_marker(value)) is not None
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
        amounts = _budget_amount_values(value)
        if amounts:
            raw_counts = Counter(raw_text for raw_text, _normalized in amounts)
            raw_indexes: Counter[str] = Counter()
            result: list[CplOccurrence] = []
            for raw_text, normalized in amounts:
                raw_index = raw_indexes[raw_text]
                raw_indexes[raw_text] += 1
                result.append(
                    _occurrence(
                        fragment,
                        raw_text,
                        normalized,
                        locator_extra=(
                            {"text_occurrence": raw_index}
                            if raw_counts[raw_text] > 1
                            else None
                        ),
                    )
                )
            return result
        return [_occurrence(fragment, value, None)]
    if field_code == CplFieldCode.LEGAL_BASIS:
        citations = _legal_citations(value)
        return (
            [
                _occurrence(fragment, raw_text, normalized)
                for raw_text, normalized in citations
            ]
            if citations
            else [_occurrence(fragment, value, None)]
        )
    if (
        field_code == CplFieldCode.TARGET_AND_CONDITIONS
        and source_role == CplSourceRole.CONDITION
    ):
        base = _occurrence(
            fragment,
            value,
            {"text": value},
            axis_code=axis_code,
            source_role=source_role,
        )
        return _deduplicate_occurrences(
            [base, *_condition_occurrences(fragment, value, source_role)]
        )
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


def _condition_occurrences(
    fragment: _Fragment,
    value: str,
    source_role: CplSourceRole,
) -> list[CplOccurrence]:
    return [
        _occurrence(
            fragment,
            match.group(0).strip(),
            _normalize_condition_value(axis_code, match.group(0).strip()),
            axis_code=axis_code,
            source_role=source_role,
        )
        for axis_code, pattern in _CONDITION_PATTERNS
        for match in pattern.finditer(value)
        if match.group(0).strip()
    ]


def _normalize_condition_value(
    axis_code: CplAxisCode,
    raw_text: str,
) -> dict | None:
    if axis_code == CplAxisCode.COND_BUSINESS_AGE:
        boundaries = [
            (
                Decimal(match.group("number")),
                _condition_operator(match.group("operator")),
            )
            for match in _AGE_BOUNDARY_PATTERN.finditer(raw_text)
        ]
        return _condition_range(
            boundaries,
            single_key="years",
            min_key="min_years",
            max_key="max_years",
            unit="YEAR",
        )

    if axis_code == CplAxisCode.COND_REVENUE:
        boundaries = [
            (
                Decimal(match.group("number").replace(",", ""))
                * _AMOUNT_MULTIPLIERS[match.group("unit")],
                _condition_operator(match.group("operator")),
            )
            for match in _REVENUE_BOUNDARY_PATTERN.finditer(raw_text)
        ]
        period_match = re.search(r"최근\s*(?P<years>\d+)\s*개년", raw_text)
        normalized = _condition_range(
            boundaries,
            single_key="amount_won",
            min_key="min_amount_won",
            max_key="max_amount_won",
            unit="KRW",
        )
        if normalized is not None and period_match is not None:
            normalized["period_years"] = int(period_match.group("years"))
        return normalized

    if axis_code == CplAxisCode.COND_HEADCOUNT:
        boundaries = [
            (
                Decimal(match.group("number").replace(",", "")),
                _condition_operator(match.group("operator")),
            )
            for match in _HEADCOUNT_BOUNDARY_PATTERN.finditer(raw_text)
        ]
        return _condition_range(
            boundaries,
            single_key="count",
            min_key="min_count",
            max_key="max_count",
            unit="PERSON",
        )

    return {"text": raw_text}


def _condition_operator(value: str) -> str:
    return {
        "이내": "LTE",
        "이하": "LTE",
        "미만": "LT",
        "이상": "GTE",
        "초과": "GT",
    }[value]


def _condition_range(
    boundaries: list[tuple[Decimal, str]],
    *,
    single_key: str,
    min_key: str,
    max_key: str,
    unit: str,
) -> dict | None:
    if not boundaries or any(value != value.to_integral_value() for value, _ in boundaries):
        return None
    if len(boundaries) == 1:
        value, operator = boundaries[0]
        return {single_key: int(value), "operator": operator, "unit": unit}
    if len(boundaries) != 2:
        return None

    lower = [(value, operator) for value, operator in boundaries if operator in {"GTE", "GT"}]
    upper = [(value, operator) for value, operator in boundaries if operator in {"LTE", "LT"}]
    if len(lower) != 1 or len(upper) != 1:
        return None
    min_value, min_operator = lower[0]
    max_value, max_operator = upper[0]
    if min_value > max_value:
        return None
    return {
        min_key: int(min_value),
        max_key: int(max_value),
        "min_operator": min_operator,
        "max_operator": max_operator,
        "unit": unit,
    }


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
    tokens = [_date_token(match) for match in _DATE_TOKEN_PATTERN.finditer(value)]
    if not tokens or any(token is None for token in tokens):
        return None
    start, start_year, start_short = tokens[0]
    if re.search(r"계속(?:사업)?", value):
        result = {"start": start, "end": None, "continuing": True}
        if start_short:
            result["two_digit_year_policy"] = "ASSUME_2000S"
        return result
    if len(tokens) == 1:
        result = {
            "start": start,
            "end": None,
            "continuing": False,
            "single_year": True,
        }
        if start_short:
            result["two_digit_year_policy"] = "ASSUME_2000S"
        return result

    end, end_year, end_short = tokens[1]
    if (start_year, start) > (end_year, end):
        return None
    result = {
        "start": start,
        "end": end,
        "continuing": False,
        "multi_year": start_year != end_year,
    }
    if start_year == end_year:
        result["single_year"] = True
    if start_short or end_short:
        result["two_digit_year_policy"] = "ASSUME_2000S"
    return result


def _date_token(match: re.Match[str]) -> tuple[str, int, bool] | None:
    short = match.group("short_year")
    if short is not None:
        year = 2000 + int(short)
        return str(year), year, True

    year_text = (
        match.group("ymd_year")
        or match.group("ym_year")
        or match.group("kr_year")
        or match.group("bare_year")
    )
    if year_text is None:
        return None
    year = int(year_text)
    month_text = (
        match.group("ymd_month")
        or match.group("ym_month")
        or match.group("kr_month")
    )
    day_text = match.group("ymd_day") or match.group("kr_day")
    month = int(month_text) if month_text is not None else None
    day = int(day_text) if day_text is not None else None
    try:
        if month is not None:
            date(year, month, day or 1)
    except ValueError:
        return None
    normalized = str(year)
    if month is not None:
        normalized += f"-{month:02d}"
    if day is not None:
        normalized += f"-{day:02d}"
    return normalized, year, False


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


def _budget_amount_values(value: str) -> list[tuple[str, dict]]:
    """Normalize explicit budget amounts plus nearby closed-form labels."""
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

        normalized: dict[str, object] = {
            "amount_won": int(won),
            "currency": "KRW",
        }
        prefix_start = max(0, match.start() - 40)
        prefix = value[prefix_start : match.start()]
        year_match = re.search(r"(?P<year>20\d{2})\s*년\s*$", prefix)
        if year_match is not None:
            normalized["year"] = int(year_match.group("year"))
            raw_text = value[prefix_start + year_match.start() : match.end()].strip()
        else:
            kind_patterns = (
                ("TOTAL", r"(?:총\s*사업비|총\s*예산)\s*$"),
                ("GRANT", r"(?:지원금|보조금)\s*$"),
                ("OPERATION", r"(?:운영(?:\s*[·ㆍ/]\s*평가)?비|평가비)\s*$"),
            )
            for kind, pattern in kind_patterns:
                if re.search(pattern, prefix):
                    normalized["kind"] = kind
                    break
        amounts.append((raw_text, normalized))
    return amounts


def _normalize_axis_value(
    axis_code: CplAxisCode,
    raw_text: str,
    source_role: CplSourceRole | None,
) -> dict | None:
    if axis_code in {
        CplAxisCode.COND_BUSINESS_AGE,
        CplAxisCode.COND_REVENUE,
        CplAxisCode.COND_HEADCOUNT,
    }:
        return _normalize_condition_value(axis_code, raw_text)
    if axis_code in {CplAxisCode.PER_COMPANY_LIMIT, CplAxisCode.TOTAL_SCALE}:
        amounts = _amount_values(raw_text)
        distinct_amounts = {
            amount["amount_won"]
            for _raw, amount in amounts
            if isinstance(amount.get("amount_won"), int)
        }
        if len(distinct_amounts) != 1:
            return None
        return {
            "amount_won": next(iter(distinct_amounts)),
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
        match = _KPI_VALUE_PATTERN.search(raw_text)
        if match is None:
            return None
        digits = match.group("number").replace(",", "")
        decimal_part = match.group('decimal')
        base = (
            Decimal(f"{digits}.{decimal_part}")
            if decimal_part
            else Decimal(digits)
        )
        scaled = base * _AMOUNT_MULTIPLIERS[match.group("scale")]
        value: int | float = (
            int(scaled)
            if scaled == scaled.to_integral_value()
            else float(scaled)
        )
        unit = _KPI_UNITS.get(match.group("unit"), "NUMBER")
        return {"number": value, "unit": unit}
    if axis_code == CplAxisCode.LINKED_POLICY_IDENTIFIER:
        return {"policy_identifier": raw_text}
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


def _legal_citations(value: str) -> list[tuple[str, dict]]:
    citations: list[tuple[str, dict]] = []
    occupied: list[tuple[int, int]] = []
    for match in _QUOTED_LAW_PATTERN.finditer(value):
        citations.append(_legal_citation(match))
        occupied.append(match.span())

    for match in _UNQUOTED_LAW_PATTERN.finditer(value):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        citation = _legal_citation(match)
        if _GENERIC_LAW_REFERENCE_PATTERN.fullmatch(citation[1]["law_name"]):
            continue
        citations.append(citation)
    return citations


def _legal_citation(match: re.Match[str]) -> tuple[str, dict]:
    law_name = re.sub(r"\s+", " ", match.group("law")).strip()
    article = match.group("article")
    return (
        match.group(0).strip(),
        {
            "law_name": law_name,
            "article": re.sub(r"\s+", "", article) if article else None,
        },
    )


def _occurrence(
    fragment: _Fragment,
    raw_text: str,
    normalized_value: object | None,
    *,
    axis_code: CplAxisCode | None = None,
    source_role: CplSourceRole | None = None,
    locator_extra: dict | None = None,
) -> CplOccurrence:
    return CplOccurrence(
        raw_text=raw_text,
        normalized_value=normalized_value,
        axis_code=axis_code,
        source_role=source_role,
        page_no=fragment.page_no,
        section_path=list(fragment.section_path),
        source_locator={**fragment.source_locator, **(locator_extra or {})},
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
    llm = [
        occurrence
        for occurrence in llm_occurrences
        if not any(
            _same_contained_fact(rule, occurrence)
            for rule in rules
        )
    ]
    return _deduplicate_occurrences([*rules, *llm])


def _canonical_fact_value(value: object, raw_text: str) -> object:
    """비교용 정규값. 원문을 그대로 담은 값은 라벨을 떼고 맞춘다.

    `시 출연기관 위탁(보조)` 과 `수행방식: 시 출연기관 위탁(보조)` 은 같은
    사실인데 정규값이 원문 전체라 서로 다르게 보인다. 정규화가 실제 값을
    뽑은 경우에는 손대지 않는다.
    """

    if isinstance(value, dict) and set(value) == {"text"}:
        return {"text": _strip_leading_label(str(value["text"]))}
    return value


def _same_contained_fact(rule: CplOccurrence, llm: CplOccurrence) -> bool:
    rule_text = _strip_leading_label(rule.raw_text)
    llm_text = _strip_leading_label(llm.raw_text)
    return (
        rule.axis_code == llm.axis_code
        and rule.source_role == llm.source_role
        and _canonical_fact_value(rule.normalized_value, rule.raw_text)
        == _canonical_fact_value(llm.normalized_value, llm.raw_text)
        and rule.page_no == llm.page_no
        and rule.block_id == llm.block_id
        and rule.section_path == llm.section_path
        and rule.source_locator == llm.source_locator
        and bool(rule_text and llm_text)
        and (rule_text in llm_text or llm_text in rule_text)
    )


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
    requested_fields: set[CplFieldCode] | None = None,
) -> CplResult:
    if error_code not in LLM_FAILURE_CODES:
        raise ValueError(f"Unsupported LLM failure code: {error_code}")

    degradable = (
        CPL_SEMANTIC_FIELDS
        if requested_fields is None
        else CPL_SEMANTIC_FIELDS & requested_fields
    )
    items: list[CplItem] = []
    for item in rule_result.items:
        copied = item.model_copy(deep=True)
        if (
            copied.field_code in degradable
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
