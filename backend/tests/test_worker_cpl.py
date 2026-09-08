"""Slice 2: 프로파일 → CPL 13항목 매핑 테스트. 네트워크·DB 를 타지 않는다.

입력은 팀 프로파일 v0.1.2 예시 인스턴스 하나뿐이다. 값 판정은 이미 그 안에
끝나 있고, 여기서 확인하는 것은 "그 값이 다른 자리로 새거나 없는 값이
채워지지 않았는가" 다 (초안 §6.1).
"""

from dataclasses import fields, is_dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from app.schemas.cpl import CplFieldCode
from worker.analysis_inputs import CPL_FIELD_SOURCES, facts_at
from worker.contracts.cpl_result import (
    cpl_axis_code,
    CONFIRMED,
    CPL_DISPLAY_STATUSES,
    NEEDS_CONFIRMATION,
    NO_CONTENT,
    NO_PROFILE_FIELD,
    NOT_APPLICABLE,
    PROFILE_FIELD_STATE_MISSING,
    SERVER_RESOLVED_CHECKBOX,
    UNMAPPED_PROFILE_FIELD,
    aggregate_display,
    display_status,
)
from worker.cpl import build_cpl_result


_PROFILE_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "profile_structuring"
    / "examples"
    / "request"
    / "structured_profile_v012.json"
)


@pytest.fixture(scope="module")
def profile() -> dict:
    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def result(profile):
    return build_cpl_result(profile)


def _item(result, code: CplFieldCode):
    return next(item for item in result.items if item.field_code is code)


def _subfield(item, name: str):
    return next(sub for sub in item.subfields if sub.profile_field_name == name)


def _to_plain(value):
    """dataclass 트리를 dict/list 로 편다. ``asdict`` 와 달리 enum 을 건드리지 않는다."""

    if is_dataclass(value):
        return {f.name: _to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, list):
        return [_to_plain(row) for row in value]
    if isinstance(value, dict):
        return {key: _to_plain(row) for key, row in value.items()}
    return value


# ---------------------------------------------------------------- 13항목 골격


def test_items_are_13_in_declaration_order(result):
    assert [item.field_code for item in result.items] == list(CplFieldCode)


def test_mapping_covers_every_field_code_once(result):
    assert tuple(CPL_FIELD_SOURCES) == tuple(CplFieldCode)


def test_lineage_comes_from_processing_metadata(result, profile):
    metadata = profile["processing_metadata"]
    assert result.profile_id == profile["profile_id"]
    assert result.common_ir_document_id == metadata["common_ir_document_id"]
    assert result.common_ir_source_sha256 == metadata["common_ir_source_sha256"]
    assert result.candidate_pack_id == metadata["candidate_pack"]["candidate_pack_id"]
    assert result.pipeline_version == metadata["pipeline_version"]
    assert result.structured_schema_version == metadata["structured_schema_version"]
    assert result.model_id == metadata["model_id"]
    assert result.prompt_version == metadata["prompt_version"]


# ------------------------------------------------------------- 요청 유형


def test_request_type_keeps_checkbox_glyph(result):
    item = _item(result, CplFieldCode.REQUEST_TYPE)
    (fact,) = _subfield(item, "request_type").facts
    assert fact.value_raw  # 체크박스로 서버가 정한 값이 비어 있으면 안 된다
    assert fact.selection_glyph_raw == "☑"


# ------------------------------------------------------------- 값 보존


def test_support_scale_holds_five_grounded_facts(result):
    item = _item(result, CplFieldCode.SUPPORT_CONTENT_AND_SCALE)
    assert len(item.subfields) == 6
    facts = _subfield(item, "support_scale").facts
    assert len(facts) == 5
    assert len({fact.fact_id for fact in facts}) == 5
    for fact in facts:
        assert fact.evidence
        assert all(row.common_ir_block_id for row in fact.evidence)


def test_absent_conditions_stay_absent(result):
    """not_found 인 필드를 형제 필드 값으로 메우지 않는다 (초안 §6.1)."""

    item = _item(result, CplFieldCode.TARGET_AND_CONDITIONS)
    assert len(item.subfields) == 6
    assert _subfield(item, "beneficiary").facts  # 형제에는 값이 있다
    for name in ("eligibility_conditions", "exclusions"):
        sub = _subfield(item, name)
        assert sub.status == "not_found"
        assert sub.facts == []


# ------------------------------------------------------------- 대표 상태


def test_new_or_changed_content_has_no_profile_field(result):
    """대응 필드가 없는 항목은 `확인 필요` 다. `내용 없음` 이 아니다.

    `내용 없음` 은 "적용 대상인데 문서에서 내용을 찾지 못했다" 는 문서에 대한
    판정이다 (프론트 계약의 뜻풀이). 이 항목은 프로파일에 대응 필드가 아예
    없어서 문서를 그 항목으로 읽어본 적이 없다. 확인할 근거를 확보하지 못한
    것을 문서가 비었다는 판정으로 바꾸면 사용자가 문서를 고치러 간다.
    """

    item = _item(result, CplFieldCode.NEW_OR_CHANGED_CONTENT)
    assert item.subfields == []
    assert item.representative_status == NEEDS_CONFIRMATION
    assert item.status_reason == NO_PROFILE_FIELD
    assert any(
        diagnostic.reason_code == NO_PROFILE_FIELD
        and diagnostic.unit == CplFieldCode.NEW_OR_CHANGED_CONTENT.value
        for diagnostic in result.diagnostics
    )


def test_every_item_carries_a_display_status(result):
    """프론트가 그릴 수 있는 값은 넷뿐이다. 제5의 값을 내보내지 않는다."""

    for item in result.items:
        assert item.representative_status in CPL_DISPLAY_STATUSES, item.field_code


@pytest.mark.parametrize(
    "code,expected",
    [
        # implementation_plan(not_found) + nodes(상태 없음) + support_components
        (CplFieldCode.IMPLEMENTATION_PLAN, NO_CONTENT),
        # 여섯 중 eligibility_conditions·exclusions 가 not_found 다.
        (CplFieldCode.TARGET_AND_CONDITIONS, NO_CONTENT),
        # 여섯 하위 필드가 모두 identified 다.
        (CplFieldCode.SUPPORT_CONTENT_AND_SCALE, CONFIRMED),
        # delivery_relations 는 확인, delivery_methods 는 not_found 다.
        (CplFieldCode.DELIVERY_SYSTEM, NO_CONTENT),
        (CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE, CONFIRMED),
    ],
)
def test_multi_subfield_items_are_aggregated(result, code, expected):
    item = _item(result, code)
    assert len(item.subfields) > 1
    assert item.representative_status == expected
    assert item.status_reason is None


@pytest.mark.parametrize(
    "code,expected",
    [
        (CplFieldCode.PURPOSE_GOAL, CONFIRMED),
        (CplFieldCode.BUSINESS_NEED, NO_CONTENT),
        (CplFieldCode.BUSINESS_PERIOD, CONFIRMED),
        (CplFieldCode.BUDGET, CONFIRMED),
        (CplFieldCode.LEGAL_BASIS, CONFIRMED),
        (CplFieldCode.LINKED_POLICY, CONFIRMED),
    ],
)
def test_single_source_items_map_profile_status_to_display(result, code, expected):
    """대표값만 표시 어휘다. 하위 필드에는 프로파일 문자열이 그대로 남는다."""

    item = _item(result, code)
    (subfield,) = item.subfields
    assert subfield.status in {"identified", "not_found"}
    assert item.representative_status == expected
    assert item.status_reason is None


def test_business_period_maps_program_period_only(result):
    item = _item(result, CplFieldCode.BUSINESS_PERIOD)
    assert [sub.profile_field for sub in item.subfields] == [
        "comparison_profile.program_period"
    ]


def test_support_period_is_reported_not_dropped(result):
    assert "support_period" in result.unmapped_profile_fields
    assert any(
        diagnostic.reason_code == UNMAPPED_PROFILE_FIELD
        and diagnostic.unit == "support_period"
        for diagnostic in result.diagnostics
    )


# ------------------------------------------------------------- 금지 사항


# 초안 §6.1: 사용자에게 확인율·종합 점수를 표시하지 않는다. 필드 하나만
# 생겨도 표시 계층이 집계를 시작하므로, 계약 트리의 키 이름 자체를 막는다.
_FORBIDDEN_KEY = re.compile(r"score|percent|ratio|rate|grade|확인율|count", re.I)


def test_result_exposes_no_score_like_key(result):
    plain = _to_plain(result)

    def walk(node, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert not _FORBIDDEN_KEY.search(key), f"{trail}.{key}"
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")

    walk(plain, "CplResult")


def test_worker_cpl_import_does_not_pull_old_extractor():
    """추출을 다시 돌리지 않는다 (초안 §13 단계 2 통과 조건).

    같은 프로세스에서는 다른 테스트 모듈이 이미 ``app.services.cpl`` 을
    올려 두었을 수 있으므로, 깨끗한 인터프리터에서 확인한다.
    """

    probe = (
        "import sys; import worker.cpl; "
        "sys.exit(1 if 'app.services.cpl' in sys.modules else 0)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_no_worker_source_references_old_extractor():
    worker_root = Path(__file__).resolve().parents[1] / "worker"
    for path in worker_root.rglob("*.py"):
        assert "services.cpl" not in path.read_text(encoding="utf-8"), path


# ------------------------------------------------------------- 결손 입력


def test_missing_container_does_not_raise(profile):
    """request_context 컨테이너만 사라진 경우.

    상태는 여전히 field_states 의 문자열 그대로다. 컨테이너가 없다는 이유로
    상태를 바꾸면 프로파일이 말한 것과 다른 말을 하게 된다 (초안 §6.1).
    """

    degraded = {k: v for k, v in profile.items() if k != "request_context"}

    result = build_cpl_result(degraded)

    assert len(result.items) == 13
    item = _item(result, CplFieldCode.LEGAL_BASIS)
    sub = _subfield(item, "legal_basis")
    assert sub.facts == []
    assert sub.status == "identified"  # field_states 가 그대로 남아 있다


def test_missing_field_state_yields_none_status(profile):
    """컨테이너와 field_states 가 함께 사라지면 상태는 None + 사유다."""

    dropped = {
        "implementation_plan",
        "business_need",
        "legal_basis",
        "linked_policy",
        "expected_effect",
        "performance_indicator",
    }
    degraded = {k: v for k, v in profile.items() if k != "request_context"}
    degraded["field_states"] = [
        row for row in profile["field_states"] if row["field_name"] not in dropped
    ]

    result = build_cpl_result(degraded)

    assert len(result.items) == 13
    item = _item(result, CplFieldCode.BUSINESS_NEED)
    sub = _subfield(item, "business_need")
    assert sub.status is None
    assert sub.reason_codes == [PROFILE_FIELD_STATE_MISSING]
    assert sub.facts == []
    # 상태를 모르는 것은 확인됨이 아니다.
    assert item.representative_status == NEEDS_CONFIRMATION
    assert item.status_reason is None


def _fresh_profile() -> dict:
    """이 아래 테스트들은 프로필을 변형한다. module 스코프 fixture 를 공유하면
    다른 테스트로 변형이 새므로 매번 새로 읽는다."""

    return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))


def test_request_type_status_comes_from_the_checkbox_not_field_states():
    """요청유형은 field_states 에 없다. 누락이 아니라 Rule 판정 대상이기 때문이다.

    완전히 접지된 서버 판정에 '상태 모름' 을 붙이지 않는지, 그리고 상태 출처가
    field_states 가 아니라는 것이 reason code 로 남는지 본다.
    """

    profile = _fresh_profile()
    assert not any(
        state["field_name"] == "request_type" for state in profile["field_states"]
    ), "전제가 깨졌다: field_states 에 request_type 이 생겼다"

    item = _item(build_cpl_result(profile), CplFieldCode.REQUEST_TYPE)
    subfield = item.subfields[0]
    assert subfield.status == "identified"
    assert subfield.reason_codes == [SERVER_RESOLVED_CHECKBOX]
    assert PROFILE_FIELD_STATE_MISSING not in subfield.reason_codes
    assert item.representative_status == CONFIRMED
    assert item.status_reason is None


def test_unresolved_checkbox_is_not_reported_as_absent():
    """체크를 확정하지 못한 것과 요청유형 자체가 없는 것을 구분하는지 본다."""

    profile = _fresh_profile()
    profile["request_type"] = dict(profile["request_type"], selected_code=None)
    unresolved = _item(build_cpl_result(profile), CplFieldCode.REQUEST_TYPE)
    assert unresolved.subfields[0].status == "mentioned_unresolved"
    assert unresolved.representative_status == NEEDS_CONFIRMATION

    profile.pop("request_type")
    absent = _item(build_cpl_result(profile), CplFieldCode.REQUEST_TYPE)
    assert absent.subfields[0].status == "not_found"
    assert absent.representative_status == NO_CONTENT


# ------------------------------------------------------- 전달체계 관계 멤버


def _delivery_facts(result):
    item = _item(result, CplFieldCode.DELIVERY_SYSTEM)
    return _subfield(item, "delivery_relations").facts


def test_delivery_relation_members_keep_their_own_text(result):
    """수행기관·역할 원문이 relation id 한 줄로 눌려 사라지면 안 된다.

    값은 relation 최상위가 아니라 actor·role 안에 있다. relation 을 한 줄로
    접으면 ``value_raw`` 가 None 이 되어 프로파일이 들고 있던 원문이 없어진다.
    """

    by_member = {fact.member: fact for fact in _delivery_facts(result)}
    assert by_member["actor"].value_raw == "가상 동구청년창업지원센터"
    assert by_member["role"].value_raw == "총괄"
    for member, fact in by_member.items():
        assert fact.relation_id == "delivery:center_lead"
        assert fact.value_raw is not None, f"{member} 원문이 사라졌다"
        assert fact.evidence, f"{member} 근거가 비었다"
        assert fact.source_block_id, f"{member} value_source 좌표가 사라졌다"


def test_no_synthetic_fact_id_is_minted_for_a_relation_member(result):
    """멤버에게 없는 id 를 만들어 붙이지 않는다. 자리는 좌표로 가리킨다."""

    for fact in _delivery_facts(result):
        assert fact.fact_id is None
        assert fact.id_source_key is None
        assert fact.member in {"actor", "role", "action"}


def test_every_action_becomes_its_own_member_entry():
    """``actions`` 는 목록이다. 실제 예시가 비어 있어 직접 만들어 고정한다."""

    container = {
        "container_type": "paragraph",
        "source_block_id": "block:multi",
        "common_ir_document_id": "request:MULTI",
        "common_ir_block_id": "block:multi",
        "common_ir_occurrence_ids": ["occ:multi"],
    }
    profile = {
        "comparison_profile": {
            "delivery_relations": [
                {
                    "delivery_relation_id": "delivery:multi",
                    "actor": {"value_raw": "가상 수행기관"},
                    "role": {"value_raw": "주관"},
                    "actions": [
                        {"value_raw": "접수"},
                        {"value_raw": "심사"},
                    ],
                    "relation_container": container,
                }
            ]
        }
    }
    facts = facts_at(profile, "comparison_profile.delivery_relations")
    assert [fact.member for fact in facts] == ["actor", "role", "action", "action"]
    assert [fact.value_raw for fact in facts if fact.member == "action"] == [
        "접수",
        "심사",
    ]
    # 자기 근거가 없는 멤버는 relation 단위 접지를 그대로 쓴다.
    for fact in facts:
        assert fact.relation_id == "delivery:multi"
        assert [row.common_ir_block_id for row in fact.evidence] == ["block:multi"]


def test_delivery_system_still_has_two_subfields(result):
    """하위 필드 둘은 그대로 남고, 대표값만 집계된다."""

    item = _item(result, CplFieldCode.DELIVERY_SYSTEM)
    assert [sub.profile_field_name for sub in item.subfields] == [
        "delivery_relations",
        "delivery_methods",
    ]
    assert [sub.status for sub in item.subfields] == ["identified", "not_found"]
    assert item.representative_status == NO_CONTENT


# ------------------------------------------------------- 표시 어휘 매핑


@pytest.mark.parametrize(
    "profile_status,expected",
    [
        ("identified", CONFIRMED),
        ("not_found", NO_CONTENT),
        ("not_applicable", NOT_APPLICABLE),
        ("partial", NEEDS_CONFIRMATION),
        ("mentioned_unresolved", NEEDS_CONFIRMATION),
        # 구조화 실패를 단순 `내용 없음` 으로 바꾸지 않는다 (초안 §6.1).
        ("extraction_failed", NEEDS_CONFIRMATION),
        # 어휘가 늘거나 field_states 에 항목이 없는 경우. 확인됨으로 올리지 않는다.
        ("어휘에_없는_상태", NEEDS_CONFIRMATION),
        (None, NEEDS_CONFIRMATION),
    ],
)
def test_profile_status_maps_to_display_value(profile_status, expected):
    assert display_status(profile_status) == expected


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([NO_CONTENT, CONFIRMED], NO_CONTENT),
        ([NEEDS_CONFIRMATION, CONFIRMED], NEEDS_CONFIRMATION),
        # 해당 없음은 집계에서 부재로 다룬다: 확인 + 해당 없음이면 확인됨이다.
        ([NOT_APPLICABLE, CONFIRMED], CONFIRMED),
        ([NOT_APPLICABLE, NOT_APPLICABLE], NOT_APPLICABLE),
        ([CONFIRMED, CONFIRMED], CONFIRMED),
        # 내용 없음이 확인 필요보다 앞선다 (AGENTS.md 집계 순서).
        ([NO_CONTENT, NEEDS_CONFIRMATION, NOT_APPLICABLE], NO_CONTENT),
    ],
)
def test_aggregate_display_follows_the_agents_order(statuses, expected):
    assert aggregate_display(statuses) == expected


def test_aggregate_display_refuses_an_empty_list():
    """빈 목록이 조용히 확인됨으로 떨어지면 근거 없는 항목이 초록으로 보인다."""

    with pytest.raises(ValueError):
        aggregate_display([])


def test_no_undetermined_vocabulary_survives(result):
    """옛 미확정 어휘가 결과 어디에도 남아 있지 않은지 직렬화 전체를 훑는다."""

    dumped = json.dumps(_to_plain(result), ensure_ascii=False, default=str)
    assert "UNDETERMINED" not in dumped
    assert "DISPLAY_AGGREGATION_UNDEFINED" not in dumped


def test_cpl_axis_code_is_the_declaration_ordinal():
    """프론트 표시 코드는 CplFieldCode 선언 순번이다.

    프론트 명세 예시 두 개가 이 순서와 맞는다 — CPL-01 이 요청유형,
    CPL-11 이 지원내용·지원규모다. 17 항목짜리 POC 표는 분해가 달라
    기준으로 쓰지 않는다.
    """

    assert cpl_axis_code(CplFieldCode.REQUEST_TYPE) == "CPL-01"
    assert cpl_axis_code(CplFieldCode.SUPPORT_CONTENT_AND_SCALE) == "CPL-11"
    assert cpl_axis_code(CplFieldCode.EXPECTED_EFFECTS_AND_PERFORMANCE) == "CPL-13"
    codes = [cpl_axis_code(code) for code in CplFieldCode]
    assert codes == [f"CPL-{n:02d}" for n in range(1, 14)]
    assert len(set(codes)) == 13
