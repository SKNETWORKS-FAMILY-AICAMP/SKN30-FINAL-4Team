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
from worker.analysis_inputs import CPL_FIELD_SOURCES
from worker.contracts.cpl_result import (
    DISPLAY_AGGREGATION_UNDEFINED,
    NO_PROFILE_FIELD,
    PROFILE_FIELD_STATE_MISSING,
    SERVER_RESOLVED_CHECKBOX,
    UNDETERMINED,
    UNMAPPED_PROFILE_FIELD,
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
    item = _item(result, CplFieldCode.NEW_OR_CHANGED_CONTENT)
    assert item.subfields == []
    assert item.representative_status == UNDETERMINED
    assert item.undetermined_reason == NO_PROFILE_FIELD
    assert any(
        diagnostic.reason_code == NO_PROFILE_FIELD
        and diagnostic.unit == CplFieldCode.NEW_OR_CHANGED_CONTENT.value
        for diagnostic in result.diagnostics
    )


def test_multi_subfield_items_are_undetermined(result):
    for item in result.items:
        if len(item.subfields) > 1:
            assert item.representative_status == UNDETERMINED
            assert item.undetermined_reason == DISPLAY_AGGREGATION_UNDEFINED


@pytest.mark.parametrize(
    "code,expected",
    [
        (CplFieldCode.PURPOSE_GOAL, "identified"),
        (CplFieldCode.BUSINESS_NEED, "not_found"),
        (CplFieldCode.BUSINESS_PERIOD, "identified"),
        (CplFieldCode.BUDGET, "identified"),
        (CplFieldCode.LEGAL_BASIS, "identified"),
        (CplFieldCode.LINKED_POLICY, "identified"),
    ],
)
def test_single_source_items_carry_profile_status_verbatim(result, code, expected):
    item = _item(result, code)
    assert len(item.subfields) == 1
    assert item.representative_status == expected
    assert item.undetermined_reason is None


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
    assert item.representative_status is None
    assert item.undetermined_reason is None


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
    assert item.representative_status == "identified"
    assert item.undetermined_reason is None


def test_unresolved_checkbox_is_not_reported_as_absent():
    """체크를 확정하지 못한 것과 요청유형 자체가 없는 것을 구분하는지 본다."""

    profile = _fresh_profile()
    profile["request_type"] = dict(profile["request_type"], selected_code=None)
    unresolved = _item(build_cpl_result(profile), CplFieldCode.REQUEST_TYPE)
    assert unresolved.representative_status == "mentioned_unresolved"

    profile.pop("request_type")
    absent = _item(build_cpl_result(profile), CplFieldCode.REQUEST_TYPE)
    assert absent.representative_status == "not_found"
