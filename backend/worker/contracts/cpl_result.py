"""Request Profile v0.1.2 → CPL 13항목 결과 계약 (초안 §6, §6.1).

원칙 세 줄로 요약된다.

1. 하위 필드 상태는 프로파일 ``field_states`` 의 문자열을 그대로 보존한다.
   사실 목록이 비었다는 이유로 상태를 만들어내지 않는다.
2. **대표 신호등만** 프론트 표시 어휘 네 개로 옮긴다. 프론트 계약
   (`1.FRONTEND_SCREEN_API_SPEC.md` · `3.FRONTEND_SUPABASE_HANDOFF.md`
   "상태 → 화면 표시 매핑") 이 받을 수 있는 값이 그 넷뿐이라, 화면에 못 실리는
   제5의 값(옛 ``UNDETERMINED``)을 만들지 않는다. 집계 규칙은 AGENTS.md
   ``IMPLEMENTATION_PLAN`` 절의 것을 일반화해 쓴다.
3. 없는 reason 은 ``None`` 이지 문자열 ``"null"`` 이 아니다 (초안 §9.5).

점수·확인율·등급 필드는 이 계약에 존재하지 않는다. 초안 §6.1 이
"사용자에게 확인율·종합 점수를 표시하지 않는다" 로 못박았기 때문에,
표시 계층이 실수로라도 집계할 수 있는 자리를 만들지 않는다.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from .profile_snapshot import StageDiagnostic

__all__ = [
    "CplFieldCode",
    "StageDiagnostic",
    "NO_PROFILE_FIELD",
    "PROFILE_FIELD_STATE_MISSING",
    "SERVER_RESOLVED_CHECKBOX",
    "SERVER_DERIVED_HIERARCHY_STATE",
    "UNMAPPED_PROFILE_FIELD",
    "CONFIRMED",
    "NEEDS_CONFIRMATION",
    "NO_CONTENT",
    "NOT_APPLICABLE",
    "CPL_DISPLAY_STATUSES",
    "PURPOSE_AXIS_CODES",
    "cpl_display_code",
    "EXTRACTION_COVERAGE_GAP",
    "RECHECK_RECOVERED",
    "RECHECK_NO_VALID_OCCURRENCE",
    "PROMPT_UNAVAILABLE",
    "PURPOSE_AXIS_UNRESOLVED",
    "CplAxisCode",
    "PurposeAxisAssignment",
    "PurposeAxisClassification",
    "display_status",
    "aggregate_display",
    "CplEvidence",
    "CplFact",
    "CplSubfield",
    "CplItem",
    "CplResult",
]


class CplFieldCode(StrEnum):
    """The immutable 13-field CPL vocabulary.

    This preserves the old API enum values and declaration order, but belongs
    to the worker because CPL is produced here rather than in FastAPI.
    """

    REQUEST_TYPE = "REQUEST_TYPE"
    PURPOSE_GOAL = "PURPOSE_GOAL"
    IMPLEMENTATION_PLAN = "IMPLEMENTATION_PLAN"
    BUSINESS_PERIOD = "BUSINESS_PERIOD"
    NEW_OR_CHANGED_CONTENT = "NEW_OR_CHANGED_CONTENT"
    BUSINESS_NEED = "BUSINESS_NEED"
    LEGAL_BASIS = "LEGAL_BASIS"
    LINKED_POLICY = "LINKED_POLICY"
    BUDGET = "BUDGET"
    TARGET_AND_CONDITIONS = "TARGET_AND_CONDITIONS"
    SUPPORT_CONTENT_AND_SCALE = "SUPPORT_CONTENT_AND_SCALE"
    DELIVERY_SYSTEM = "DELIVERY_SYSTEM"
    EXPECTED_EFFECTS_AND_PERFORMANCE = "EXPECTED_EFFECTS_AND_PERFORMANCE"


# 초안 §6.1 의 reason code. 값 자체가 API·로그에 그대로 실려 나가므로
# ponytail: StrEnum 대신 문자열 상수로 둔다 (profile_snapshot.py 와 같은 이유).
NO_PROFILE_FIELD = "NO_PROFILE_FIELD"
PROFILE_FIELD_STATE_MISSING = "PROFILE_FIELD_STATE_MISSING"
# 요청유형은 field_states 가 아니라 서버 체크박스 판정에서 상태가 나온다.
SERVER_RESOLVED_CHECKBOX = "SERVER_RESOLVED_CHECKBOX"
# 계층 자체는 LLM 이 고른다(``ProgramNodeSelection.level``). 서버가 하는 일은
# 그 노드에서 표시 상태를 계산하는 것뿐이라 체크박스와 이름을 구분한다.
SERVER_DERIVED_HIERARCHY_STATE = "SERVER_DERIVED_HIERARCHY_STATE"
UNMAPPED_PROFILE_FIELD = "UNMAPPED_PROFILE_FIELD"


# ------------------------------------------------------------- 표시 어휘

# 프론트 계약의 CPL 상태 네 개. 화면 라벨·색이 이 값에만 붙어 있으므로
# 여기 없는 값을 내보내면 화면이 그 항목을 그리지 못한다.
CONFIRMED = "confirmed"  # 확인됨
NEEDS_CONFIRMATION = "needs_confirmation"  # 확인 필요
NO_CONTENT = "no_content"  # 내용 없음 (적용 대상인데 문서에 없음)
NOT_APPLICABLE = "not_applicable"  # 해당 없음 (항목 자체가 적용 안 됨)

CPL_DISPLAY_STATUSES = frozenset(
    {CONFIRMED, NEEDS_CONFIRMATION, NO_CONTENT, NOT_APPLICABLE}
)

# 팀 프로파일 ``field_states`` 어휘 → 표시값.
#
# ``extraction_failed`` 는 ``no_content`` 가 아니라 ``needs_confirmation`` 이다.
# 초안 §6.1: "구조화 실패를 단순 `내용 없음` 으로 바꾸지 않는다." 문서에 없다는
# 판정과 우리가 못 읽었다는 사실은 사용자에게 다른 다음 행동을 요구한다.
_PROFILE_STATUS_DISPLAY: dict[str, str] = {
    "identified": CONFIRMED,
    "not_found": NO_CONTENT,
    "not_applicable": NOT_APPLICABLE,
    "partial": NEEDS_CONFIRMATION,
    "mentioned_unresolved": NEEDS_CONFIRMATION,
    "extraction_failed": NEEDS_CONFIRMATION,
}


def display_status(profile_status: str | None) -> str:
    """프로파일 상태 하나를 표시값으로 옮긴다.

    모르는 상태(팀 프로파일이 어휘를 늘렸거나 ``field_states`` 에 항목이 없어
    ``None``)는 ``needs_confirmation`` 이다. 상태를 모른다는 사실을 확인됨으로
    올리지 않는다.
    """

    return _PROFILE_STATUS_DISPLAY.get(profile_status or "", NEEDS_CONFIRMATION)


def aggregate_display(statuses: Iterable[str]) -> str:
    """하위 필드 표시값들의 대표값 (AGENTS.md ``IMPLEMENTATION_PLAN`` 절 일반화).

    전부 내용 없음이면 내용 없음, 내용 없음과 다른 상태가 섞이면 확인 필요,
    그다음 하나라도 확인 필요면 확인 필요, 전부 해당 없음이면 해당 없음,
    나머지는 확인됨이다.

    집계에서 ``not_applicable`` 은 "부재" 로 다룬다. 확인 + 해당 없음이면
    확인됨이다 (AGENTS.md: "`확인 + 해당 없음` 이면 `PRESENT`").
    ``no_content`` 는 하위 필드에 그대로 남지만, 다른 하위 필드의 근거가
    있는 경우 상위 항목 전체를 내용 없음으로 낮추지 않는다.
    """

    values = list(statuses)
    if not values:
        # 부르는 쪽이 빈 목록을 걸러야 한다. 여기서 조용히 확인됨으로 떨어지면
        # 근거가 없는 항목이 초록으로 보인다.
        raise ValueError("집계할 하위 필드 상태가 없다")
    if all(value == NO_CONTENT for value in values):
        return NO_CONTENT
    if NO_CONTENT in values or NEEDS_CONFIRMATION in values or any(
        value not in CPL_DISPLAY_STATUSES for value in values
    ):
        # 어휘 밖의 값도 확인 필요로 떨어진다. 확인됨 쪽으로 올리는 실수만
        # 사용자를 속인다.
        return NEEDS_CONFIRMATION
    if all(value == NOT_APPLICABLE for value in values):
        return NOT_APPLICABLE
    return CONFIRMED


def cpl_display_code(field_code: "CplFieldCode") -> str:
    """프론트 표시 코드. ``CplFieldCode`` 선언 순서의 순번이다.

    의미 축(``CplAxisCode``) 이 아니다. 공개 페이로드의 ``axes[].axis_code`` 가
    담는 값이 이것이라 이름이 겹쳐 보였다. 키 이름은 계약이라 두고 함수만
    구분한다.

    프론트 명세의 예시 두 개가 이 순서와 맞는다 — ``CPL-01`` 이 요청유형
    ("요청유형이 확인되었습니다"), ``CPL-11`` 이 지원내용·지원규모
    ("지원 기업 수는 확인되지만 기업당 한도는 확인이 필요합니다").
    17 항목짜리 POC 표는 분해가 다른 낡은 문서라 기준으로 쓰지 않는다.
    """

    return f"CPL-{list(CplFieldCode).index(field_code) + 1:02d}"


class CplAxisCode(StrEnum):
    """원문 값의 의미 축 (AGENTS.md ``CPL Slice 6 정정 계약``).

    ``cpl_display_code()`` 와 다른 층이다. 저쪽은 13항목 표시 코드(``CPL-01``)로
    항목 하나를 가리키고, 이쪽은 fact 하나가 어떤 의미를 말하는지를 가리킨다.
    이름이 겹쳐 보여도 어휘를 공유하지 않는다.

    지금은 ``PURPOSE_GOAL`` 네 축만 있다. 다른 필드의 축이 확정되면 여기에
    더한다. FIT 이 당장 두 축만 쓰더라도 CPL 은 네 축을 구조화한다 — 소비자가
    쓰는 만큼만 만들면 CPL 이 다시 FIT 전용 중간 형식이 된다.
    """

    TARGET_CONDITION = "PURPOSE_TARGET_CONDITION"
    PROBLEM_DOMAIN = "PURPOSE_PROBLEM_DOMAIN"
    SPECIFIC_OBJECTIVE = "PURPOSE_SPECIFIC_OBJECTIVE"
    DIRECTION = "PURPOSE_DIRECTION"


PURPOSE_AXIS_CODES = frozenset(code.value for code in CplAxisCode)
# 호출은 했는데 유효한 축을 하나도 만들지 못했다. 축이 없다는 사실이지
# 값이 없다는 뜻이 아니다.
PURPOSE_AXIS_UNRESOLVED = "PURPOSE_AXIS_UNRESOLVED"
# 버전 프롬프트 파일을 읽지 못했다. 기본 문구로 대체하지 않는다 — 어떤 문구로
# 만든 분류인지 말할 수 없는 결과를 내느니 축을 비운다.
PROMPT_UNAVAILABLE = "PROMPT_UNAVAILABLE"
# 원문에 그 필드의 라벨 구역이 있는데 값이 비었다. 실패 원인이 무엇인지는
# 모르지만 "문서에 내용이 없다" 고 확정할 수 없다는 것은 확실하다.
EXTRACTION_COVERAGE_GAP = "EXTRACTION_COVERAGE_GAP"
# 1차 추출이 놓친 값을 CPL 재검이 되찾았다. subfield.status 는 구조화가 낸
# not_found 로 남는다 — 처음에 놓쳤다는 이력을 지우지 않는다.
RECHECK_RECOVERED = "RECHECK_RECOVERED"
# 재검이 형식은 정상인 응답을 냈는데 검증을 통과한 occurrence 가 하나도 없다.
# 전송 실패와 구분한다.
RECHECK_NO_VALID_OCCURRENCE = "RECHECK_NO_VALID_OCCURRENCE"


@dataclass(frozen=True, slots=True)
class PurposeAxisAssignment:
    """목적 fact 하나에 붙은 의미 축. 값·오프셋·근거를 새로 만들지 않는다."""

    fact_id: str
    axis_code: str
    quoted_text: str


@dataclass(frozen=True, slots=True)
class PurposeAxisClassification:
    """의미 축 분류 1회의 기록.

    ``attempted`` 는 호출 여부, ``reason_code`` 는 호출이 실패했거나 유효한
    축을 하나도 만들지 못한 사유다. 문서당 한 번이며 같은 입력으로 재시도하지
    않는다. 분류가 비어도 CPL 의 값·근거·상태는 그대로다 — 축은 값이 아니다.
    """

    attempted: bool
    assignments: list[PurposeAxisAssignment] = field(default_factory=list)
    reason_code: str | None = None
    dropped: list[str] = field(default_factory=list)
    prompt_version: str | None = None
    # 버전 이름이 가리키는 실제 문구의 해시. 같은 이름으로 내용이 바뀌면
    # 여기서 드러난다.
    prompt_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CplEvidence:
    """Common IR 블록까지 내려가는 근거 한 줄. 접지가 없으면 만들지 않는다."""

    source_block_id: str | None
    common_ir_document_id: str | None
    common_ir_block_id: str | None
    common_ir_occurrence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CplFact:
    """프로파일의 값 하나를 표시 계층이 쓰는 모양으로 정규화한 것.

    ``fact_id`` 가 optional 인 이유: ``program_hierarchy.nodes``,
    ``support_components`` 항목은 ``fact_id`` 가 아니라 각자의 id 키를 쓴다.
    어느 키에서 왔는지는 ``id_source_key`` 에 남겨 두어 원본 컨테이너를
    되짚을 수 있게 한다. ``comparison_profile.delivery_relations`` 의 멤버는
    아예 자기 id 가 없어서 ``relation_id`` · ``member`` 로 자리를 가리킨다.
    """

    fact_id: str | None
    value_raw: str | None
    status: str | None
    source_block_id: str | None
    start_char: int | None
    end_char: int | None
    text_basis: str | None
    evidence: list[CplEvidence] = field(default_factory=list)
    program_node_id: str | None = None
    primary_component_id: str | None = None
    id_source_key: str | None = None
    # ``delivery_relations`` 는 actor·role·actions 가 한 relation 안에 중첩돼
    # 있고 각자의 fact_id 가 없다. 그 자리를 (relation_id, member) 좌표로
    # 남긴다. 없는 id 를 지어내지 않고, 셋 중 하나를 대표로 고르지도 않는다.
    relation_id: str | None = None
    member: str | None = None
    # ``actions`` 는 같은 relation 안에 여러 항목이 있을 수 있어 순번을
    # 보존한다. 내부 FIT 입력 좌표로만 쓰며 공개 result_data에는 직렬화하지
    # 않는다.
    member_index: int | None = field(
        default=None, repr=False, metadata={"serialize": False}
    )
    # 의미 축. 한 원문이 축을 여럿 가지면 축마다 fact 를 따로 보존한다.
    # 단수라야 소비 쪽 필터가 (field, axis) 한 쌍으로 끝난다.
    axis_code: str | None = None
    # 그 축에 해당한다고 모델이 지목하고 서버가 검증한 ``value_raw`` 부분.
    # 비교 입력은 문장 전체가 아니라 이 구간이다 (초안 §7.1 FIT-1). 축과 짝이라
    # 축이 없으면 이것도 없다.
    axis_quoted_text: str | None = None
    # CPL 재검으로 복구한 값은 구조화가 만든 fact_id 가 없다. 서버가 가짜 id 를
    # 지어내는 대신 그 값이 나온 원문 구역 참조를 그대로 둔다.
    evidence_ref: str | None = None
    # 요청 유형 체크박스 글리프. 서버가 원본 글리프로 정한 값이라
    # 표시 계층까지 원형으로 끌고 간다 (초안 §6).
    selection_glyph_raw: str | None = None


@dataclass(frozen=True, slots=True)
class CplSubfield:
    """CPL 항목 하나가 참조하는 프로파일 필드 하나."""

    profile_field: str  # 예: "comparison_profile.support_scale"
    profile_field_name: str  # 예: "support_scale"
    status: str | None  # field_states 의 문자열 그대로. 없으면 None.
    reason_codes: list[str] = field(default_factory=list)
    facts: list[CplFact] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CplItem:
    """CPL 13항목 중 하나.

    ``representative_status`` 만 표시 어휘다. 하위 필드의 프로파일 상태 원본은
    ``subfields[].status`` 에 그대로 남아 있어, 상세 팝업의 ``values[]`` ·
    ``source_fields`` 를 만들 재료가 사라지지 않는다.
    """

    field_code: CplFieldCode
    representative_status: str  # CPL_DISPLAY_STATUSES 중 하나
    # 대표값이 하위 필드 상태에서 바로 나오지 않은 경우의 사유. 없으면 None.
    status_reason: str | None
    subfields: list[CplSubfield] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CplResult:
    """13항목 + 계보. 점수·확인율·건수 필드는 의도적으로 없다 (초안 §6.1)."""

    items: list[CplItem]
    profile_id: str | None
    common_ir_document_id: str | None
    common_ir_source_sha256: str | None
    candidate_pack_id: str | None
    pipeline_version: str | None
    structured_schema_version: str | None
    model_id: str | None
    prompt_version: str | None
    unmapped_profile_fields: list[str] = field(default_factory=list)
    diagnostics: list[StageDiagnostic] = field(default_factory=list)
    # 의미 축 분류 기록. 결정적 빌더만 돌린 결과에는 attempted=False 로 남고,
    # 값·근거·상태는 축이 없어도 그대로다.
    purpose_axis: PurposeAxisClassification = field(
        default_factory=lambda: PurposeAxisClassification(attempted=False)
    )
