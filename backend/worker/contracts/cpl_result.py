"""Request Profile v0.1.2 → CPL 13항목 결과 계약 (초안 §6, §6.1).

원칙 세 줄로 요약된다.

1. 상태는 프로파일 ``field_states`` 의 문자열을 그대로 옮긴다. 다른 어휘로
   번역하지 않고, 사실 목록이 비었다는 이유로 상태를 만들어내지도 않는다.
2. 하위 필드가 둘 이상 섞인 항목의 대표 신호등은 미확정이다. 우선순위를
   지어내서 하나를 고르지 않는다 (초안 §6.1).
3. 없는 reason 은 ``None`` 이지 문자열 ``"null"`` 이 아니다 (초안 §9.5).

점수·확인율·등급 필드는 이 계약에 존재하지 않는다. 초안 §6.1 이
"사용자에게 확인율·종합 점수를 표시하지 않는다" 로 못박았기 때문에,
표시 계층이 실수로라도 집계할 수 있는 자리를 만들지 않는다.
"""

from dataclasses import dataclass, field

from app.schemas.cpl import CplFieldCode

from .profile_snapshot import StageDiagnostic

__all__ = [
    "CplFieldCode",
    "StageDiagnostic",
    "DISPLAY_AGGREGATION_UNDEFINED",
    "NO_PROFILE_FIELD",
    "PROFILE_FIELD_STATE_MISSING",
    "UNMAPPED_PROFILE_FIELD",
    "UNDETERMINED",
    "CplEvidence",
    "CplFact",
    "CplSubfield",
    "CplItem",
    "CplResult",
]


# 초안 §6.1 의 reason code. 값 자체가 API·로그에 그대로 실려 나가므로
# ponytail: StrEnum 대신 문자열 상수로 둔다 (profile_snapshot.py 와 같은 이유).
DISPLAY_AGGREGATION_UNDEFINED = "DISPLAY_AGGREGATION_UNDEFINED"
NO_PROFILE_FIELD = "NO_PROFILE_FIELD"
PROFILE_FIELD_STATE_MISSING = "PROFILE_FIELD_STATE_MISSING"
# 요청유형은 field_states 가 아니라 서버 체크박스 판정에서 상태가 나온다.
SERVER_RESOLVED_CHECKBOX = "SERVER_RESOLVED_CHECKBOX"
UNMAPPED_PROFILE_FIELD = "UNMAPPED_PROFILE_FIELD"

# 프로파일 상태 어휘(``identified`` 등)와 섞이지 않는 별도 표시값이다.
UNDETERMINED = "UNDETERMINED"


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
    """CPL 13항목 중 하나."""

    field_code: CplFieldCode
    representative_status: str | None  # 프로파일 상태 문자열 또는 UNDETERMINED
    undetermined_reason: str | None
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
