"""Slice 6: 워커가 만든 CPL·FIT·SIM 결과를 ``result.*`` 에 남긴다 (초안 §10).

여기서 판정하지 않는다. 이미 확정된 결과 계약을 스키마가 가진 자리에
그대로 옮기는 일뿐이다. 그래서 이 모듈에는 의미 판단이 없고 전부 Rule 이다
(CLAUDE.md "Rule·LLM 선택 원칙" — 입력 문법과 대상 컬럼이 닫혀 있다).

자리 배치는 스키마가 정한다. 문서가 아니라 DDL 이 계약이다.

- CPL 13항목 → ``axis_result`` (``axis_type='CPL'``)
- FIT 7관계 → ``axis_result`` (``axis_type='FIT'``)
- SIM 후보 → ``sim_candidate`` 한 행. **``axis_result`` 에 넣지 않는다.**
  ``axis_result.axis_type`` CHECK 에 ``SIM`` 이 없다. 4축은 후보 행의
  ``purpose_result``·``target_result``·``support_result``·``delivery_result``
  네 jsonb 컬럼이 자리다.
- 근거 → ``evidence_snapshot``. 요청서는 ``side='REQUEST'``, 공고는
  ``'EXISTING'``. 결과 스냅샷이므로 ``usage_scope='RESULT'`` 다.

아래를 지킨다.

1. **점수는 사용자 문구로 새지 않는다** (초안 §6.1, §7.2). ``summary_text``
   에는 아무 숫자도 넣지 않는다. 내부 점수는 ``result_data`` jsonb 와
   ``sim_candidate`` 의 순위 컬럼 안에만 산다.
2. **표시 어휘로 나간다.** ``axis_result.status`` 와 ``sim_candidate`` 의 네
   ``*_result`` 컬럼은 프론트 RPC 가 그대로 읽는 자리다 (프론트 계약
   "상태 → 화면 표시 매핑"). 내부 상세 — 하위 필드의 프로파일 상태 원본,
   reason code, 인용한 fact id — 는 같은 행의 ``result_data`` 와 축 jsonb 안에
   그대로 남는다. 화면 어휘로 옮기는 일이 정보를 지우지는 않는다.
3. **좌표를 지어내지 않는다** (초안 §10). ``common_ir_block_id`` ·
   ``start_char`` · ``end_char`` · ``source_sha256`` 는 결과 계약이 실제로
   들고 온 값이고, 없으면 NULL 이다. 형제 근거에서 빌려오지 않는다.

트랜잭션은 열지 않는다. 호출자가 준 커넥션에 쓰기만 하므로, 임대를 뺏긴
워커의 결과는 호출자의 펜싱 롤백에 함께 딸려 사라진다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.schemas.cpl import CplFieldCode
from app.schemas.fit import FitRelationId
from app.schemas.sim import SimAxis

from .contracts.cpl_result import CplEvidence, CplResult, cpl_axis_code
from .contracts.fit_result import FitResult, fit_axis_code
from .contracts.ml_result import MlReferenceResult
from .contracts.sim_result import (
    SimAxisResult,
    SimCommonProfile,
    SimComparisonResult,
    sim_display_status,
)

__all__ = ["AnalysisResults", "persist_results"]


# 선언 순서가 곧 표시 순서다. 결과 목록의 순서에 기대지 않고 어휘에서 뽑는다.
_CPL_ORDINALS = {code: index for index, code in enumerate(CplFieldCode)}
_FIT_ORDINALS = {code: index for index, code in enumerate(FitRelationId)}

# SIM 4축이 들어갈 후보 행의 컬럼. CONTENT(지원내용)가 support_result 다.
_SIM_AXIS_COLUMNS: dict[SimAxis, str] = {
    SimAxis.PURPOSE: "purpose_result",
    SimAxis.TARGET: "target_result",
    SimAxis.CONTENT: "support_result",
    SimAxis.DELIVERY: "delivery_result",
}


@dataclass(frozen=True, slots=True)
class AnalysisResults:
    """한 검사 건의 결과 묶음. ``run_analysis`` 가 이것을 돌려주면 저장된다.

    ``sim_profiles`` 는 근거 접지에만 쓴다. SIM 축 결과는 fact id 만 들고
    있어서, 원문·Common IR 블록은 비교에 쓰인 공통 프로파일에만 있다. 키는
    ``source_profile_id`` 이고 요청서·후보 프로파일을 함께 담는다.

    ``ml``은 CPL 결과를 입력으로 삼은 참고정보다. 현재 DB DDL에는
    전용 저장 자리가 없으므로 결과 조립과 실행 진단에만 보존한다.
    """

    cpl: CplResult | None = None
    fit: FitResult | None = None
    sim: SimComparisonResult | None = None
    ml: MlReferenceResult | None = None
    sim_profiles: Mapping[str, SimCommonProfile] = field(default_factory=dict)
    # Result 화면 복원을 위한 입력 스냅샷. 값이 구조화 프로필에 없으면
    # None으로 남기며 파일명·heading에서 추측하지 않는다.
    program_name: str | None = None
    original_filename: str | None = None


# ------------------------------------------------------------------ SQL

# 케이스 단위 삭제 후 재삽입이다. upsert 가 아닌 이유는 자연키다:
# axis_result 만 (case, type, code) 유니크를 갖고, evidence_snapshot 에는
# 유니크 키가 아예 없다. 없는 키를 지어내 ON CONFLICT 를 만들면 그 키가
# 계약이 되어버린다. 삭제와 삽입이 호출자의 한 트랜잭션 안이라
# 중간 상태는 밖에서 보이지 않는다.
#
# 대화 근거(usage_scope='CONVERSATION')는 분석 결과가 아니라 세션이 만든
# 것이므로 재분석이 지우지 않는다.
_DELETE_STATEMENTS = (
    text(
        "DELETE FROM result.evidence_snapshot"
        " WHERE analysis_case_pk = :analysis_case_pk AND usage_scope = 'RESULT'"
    ),
    text("DELETE FROM result.sim_candidate WHERE analysis_case_pk = :analysis_case_pk"),
    text("DELETE FROM result.axis_result WHERE analysis_case_pk = :analysis_case_pk"),
)

_INSERT_AXIS = text(
    """
    INSERT INTO result.axis_result (
        axis_result_pk, analysis_case_pk, axis_type, axis_code,
        status, summary_text, result_data, ordinal
    ) VALUES (
        :axis_result_pk, :analysis_case_pk, :axis_type, :axis_code,
        :status, :summary_text, CAST(:result_data AS jsonb), :ordinal
    )
    """
)

_INSERT_CANDIDATE = text(
    """
    INSERT INTO result.sim_candidate (
        sim_candidate_pk, analysis_case_pk, existing_profile_version_pk,
        rank_no, similarity_score, priority_score, status, title, result_summary,
        purpose_result, target_result, support_result, delivery_result
    ) VALUES (
        :sim_candidate_pk, :analysis_case_pk, :existing_profile_version_pk,
        :rank_no, :similarity_score, :priority_score, :status, :title, :result_summary,
        CAST(:purpose_result AS jsonb), CAST(:target_result AS jsonb),
        CAST(:support_result AS jsonb), CAST(:delivery_result AS jsonb)
    )
    """
)

_INSERT_EVIDENCE = text(
    """
    INSERT INTO result.evidence_snapshot (
        analysis_case_pk, axis_type, axis_result_pk, sim_candidate_pk,
        side, usage_scope, field_name, raw_value, comparison_side, source_sha256,
        candidate_pack_block_id, start_char, end_char,
        common_ir_document_id, common_ir_block_id, common_ir_occurrence_ids
    ) VALUES (
        :analysis_case_pk, :axis_type, :axis_result_pk, :sim_candidate_pk,
        :side, 'RESULT', :field_name, :raw_value, :comparison_side, :source_sha256,
        :candidate_pack_block_id, :start_char, :end_char,
        :common_ir_document_id, :common_ir_block_id,
        CAST(:common_ir_occurrence_ids AS text[])
    )
    """
)

# 후보 행의 existing_profile_version_pk 는 NOT NULL 이고 kb.profile_version 을
# 참조한다. 그래서 후보 프로파일 id 로 실제 행을 찾는다. 없으면 그 후보는
# 저장하지 않는다 — kb 행을 지어내는 것이 더 나쁘다.
_PROFILE_VERSIONS = text(
    """
    SELECT sp.source_profile_id, pv.profile_version_pk
      FROM kb.profile_version pv
      JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk
      JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk
     WHERE sp.source_profile_id = ANY(:source_profile_ids)
       AND pv.is_current
    """
)


# ---------------------------------------------------------------- 직렬화


def _plain(value: Any) -> Any:
    """dataclass 트리를 json 이 아는 모양으로 편다. 값을 바꾸지 않는다."""

    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {_plain(key): _plain(row) for key, row in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(row) for row in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False)


# ------------------------------------------------------------------ 표시 문구


_CPL_STATUS_SUMMARY = {
    "confirmed": "값과 원문 근거를 확인했습니다.",
    "needs_confirmation": "일부 값이나 원문 확인이 필요합니다.",
    "no_content": "적용 대상이지만 원문에서 내용을 찾지 못했습니다.",
    "not_applicable": "이 요청에는 적용되지 않는 항목입니다.",
}
_CPL_REASON_SUMMARY = {
    "NO_PROFILE_FIELD": "대응하는 프로파일 필드가 없습니다.",
    "PROFILE_FIELD_STATE_MISSING": "프로파일 상태를 확인할 수 없습니다.",
    "UNMAPPED_PROFILE_FIELD": "프로파일 필드 연결이 확인되지 않았습니다.",
    "SERVER_RESOLVED_CHECKBOX": "요청유형 체크박스를 서버 규칙으로 확인했습니다.",
}


def _cpl_summary(item: Any) -> str:
    reason = _CPL_REASON_SUMMARY.get(item.status_reason)
    if reason:
        return reason
    return _CPL_STATUS_SUMMARY.get(
        item.representative_status,
        "항목 상태를 확인할 수 없어 추가 확인이 필요합니다.",
    )


_FIT_STATUS_SUMMARY = {
    "FIT": "두 측면의 연결을 확인했습니다.",
    "NEEDS_REVIEW": "비교는 가능하지만 추가 검토가 필요합니다.",
    "CONFLICT": "원문상 충돌이 확인되었습니다.",
    "INSUFFICIENT": "비교에 필요한 근거가 부족합니다.",
    "NOT_APPLICABLE": "이 비교축은 적용되지 않는 항목입니다.",
}
_FIT_REASON_SUMMARY = {
    "HIERARCHY_COMPARISON_NOT_AVAILABLE": "계층 비교 기준이 없어 정보가 부족합니다.",
    "NO_CONDITIONS_SPECIFIED": "조건이 명시되지 않아 비교할 수 없습니다.",
    "COMPARISON_EVIDENCE_MISSING": "비교할 원문 근거가 부족합니다.",
    "COMPARISON_VALUE_INVALID": "비교값을 정규화하지 못했습니다.",
    "NUMERIC_MISMATCH": "정량값이 일치하지 않습니다.",
    "SINGLE_SIDED_NO_CONFLICT": "한쪽 정보만 있어 비교가 성립하지 않습니다.",
    "LLM_INVALID_RESPONSE": "비교 응답을 확인할 수 없습니다.",
    "LLM_TIMEOUT": "비교 응답 시간이 초과되었습니다.",
    "LLM_UNAVAILABLE": "비교 모델을 사용할 수 없습니다.",
}


def _fit_summary(relation: Any) -> str:
    reason = _FIT_REASON_SUMMARY.get(relation.reason_code)
    if reason:
        return reason
    return _FIT_STATUS_SUMMARY.get(
        relation.status.value,
        "비교 상태를 확인할 수 없어 추가 확인이 필요합니다.",
    )


_SIM_STATUS_SUMMARY = {
    "similar": "공통점이 확인되었습니다.",
    "partial": "공통점과 차이점이 함께 확인되었습니다.",
    "different": "비교한 내용에서 차이점이 확인되었습니다.",
    "insufficient": "비교에 필요한 정보가 부족합니다.",
}


def _sim_axis_summary(result: SimAxisResult) -> str:
    """구조화 상태만으로 만드는 화면 한 줄 문구.

    ``common_points``·``differences`` 원문은 JSON 상세에 그대로 보존하되,
    모델이 만든 자유 문장을 사용자용 요약으로 재사용하지 않는다.
    """

    status = sim_display_status(result.status)
    if status == "similar" and not result.common_points:
        return "비교 결과가 유사하지만 공통 근거가 비어 있습니다."
    if status == "partial" and not (result.common_points or result.differences):
        return "비교 결과가 부분적으로 일치하지만 상세 근거가 부족합니다."
    return _SIM_STATUS_SUMMARY.get(status, "비교 상태를 확인할 수 없습니다.")


def _sim_result_summary(axes: Sequence[SimAxisResult]) -> str:
    statuses = {sim_display_status(axis.status) for axis in axes}
    if "insufficient" in statuses:
        return "비교에 필요한 정보가 일부 부족합니다."
    if "different" in statuses:
        return "비교한 축에서 차이점이 확인되었습니다."
    if "partial" in statuses:
        return "비교한 축에서 공통점과 차이점이 함께 확인되었습니다."
    if statuses and statuses <= {"similar"}:
        return "주요 비교 축의 공통점이 확인되었습니다."
    return "비교 결과가 없습니다."


# ------------------------------------------------------------------ 근거


def _evidence_rows(
    *,
    analysis_case_pk: UUID,
    axis_type: str,
    axis_result_pk: UUID | None = None,
    sim_candidate_pk: UUID | None = None,
    side: str,
    comparison_side: str | None = None,
    field_name: str | None,
    raw_value: str | None,
    evidence: Sequence[CplEvidence],
    start_char: int | None = None,
    end_char: int | None = None,
    source_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """근거 한 건을 접지 수만큼 편다.

    ``raw_value`` 는 NOT NULL 이다. 원문이 없으면 행을 만들지 않는다 —
    빈 문자열을 넣으면 "근거는 있는데 원문이 비었다" 로 읽힌다.

    접지가 하나도 없으면 좌표 없는 한 줄로 남긴다. 원문을 버리지 않으면서
    Common IR 좌표를 지어내지도 않는 유일한 모양이다 (초안 §10).
    """

    if raw_value is None:
        return []
    common = {
        "analysis_case_pk": analysis_case_pk,
        "axis_type": axis_type,
        "axis_result_pk": axis_result_pk,
        "sim_candidate_pk": sim_candidate_pk,
        "side": side,
        "comparison_side": comparison_side,
        "field_name": field_name,
        "raw_value": raw_value,
        "source_sha256": source_sha256,
        "start_char": start_char,
        "end_char": end_char,
    }
    if not evidence:
        return [
            {
                **common,
                "candidate_pack_block_id": None,
                "common_ir_document_id": None,
                "common_ir_block_id": None,
                "common_ir_occurrence_ids": None,
            }
        ]
    return [
        {
            **common,
            "candidate_pack_block_id": row.source_block_id,
            "common_ir_document_id": row.common_ir_document_id,
            "common_ir_block_id": row.common_ir_block_id,
            # 빈 배열과 "근거 id 가 없음" 을 구분한다.
            "common_ir_occurrence_ids": list(row.common_ir_occurrence_ids) or None,
        }
        for row in evidence
    ]


# ------------------------------------------------------------------ 축별 변환


def _cpl_rows(
    cpl: CplResult, analysis_case_pk: UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    axes: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for item in cpl.items:
        axis_result_pk = uuid4()
        axes.append(
            {
                "axis_result_pk": axis_result_pk,
                "analysis_case_pk": analysis_case_pk,
                "axis_type": "CPL",
                # 프론트 표시 코드다. 내부 어휘 이름은 result_data 에 남긴다.
                "axis_code": cpl_axis_code(item.field_code),
                # 표시 어휘 그대로다. 하위 필드의 프로파일 상태 원본은
                # result_data 안 subfields[] 에 남는다.
                "status": item.representative_status,
                "summary_text": _cpl_summary(item),
                "result_data": _json(item),
                "ordinal": _CPL_ORDINALS[item.field_code],
            }
        )
        for subfield in item.subfields:
            for fact in subfield.facts:
                evidence.extend(
                    _evidence_rows(
                        analysis_case_pk=analysis_case_pk,
                        axis_type="CPL",
                        axis_result_pk=axis_result_pk,
                        side="REQUEST",
                        field_name=subfield.profile_field,
                        raw_value=fact.value_raw,
                        evidence=fact.evidence,
                        start_char=fact.start_char,
                        end_char=fact.end_char,
                        source_sha256=cpl.common_ir_source_sha256,
                    )
                )
    return axes, evidence


def _fit_rows(
    fit: FitResult, analysis_case_pk: UUID
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    axes: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for relation in fit.relations:
        axis_result_pk = uuid4()
        axes.append(
            {
                "axis_result_pk": axis_result_pk,
                "analysis_case_pk": analysis_case_pk,
                "axis_type": "FIT",
                # 프론트 계약이 ``FIT-07`` 로 받는다. 내부 id 는 ``FIT-7`` 그대로다.
                "axis_code": fit_axis_code(relation.relation_id),
                "status": relation.status.value,
                "summary_text": _fit_summary(relation),
                "result_data": _json(relation),
                "ordinal": _FIT_ORDINALS[relation.relation_id],
            }
        )
        # FIT 은 좌우 근거를 **요청서 프로파일에서** 만든다 (초안 §7.1).
        # 그래서 양쪽 모두 side='REQUEST' 다. 공고 쪽 근거가 아니다.
        for comparison_side, side_input in (
            ("LEFT", relation.left),
            ("RIGHT", relation.right),
        ):
            for ref in side_input.facts:
                evidence.extend(
                    _evidence_rows(
                        analysis_case_pk=analysis_case_pk,
                        axis_type="FIT",
                        axis_result_pk=axis_result_pk,
                        side="REQUEST",
                        comparison_side=comparison_side,
                        field_name=ref.field_name,
                        raw_value=ref.value_raw,
                        evidence=ref.evidence,
                    )
                )
    return axes, evidence


def _sim_axis_json(result: SimAxisResult) -> str:
    """축 결과 한 칸. ``status`` 만 표시 어휘로 낮추고 나머지는 그대로다.

    이 네 컬럼이 프론트가 읽는 자리라 대문자 ``SIMILAR`` 를 그대로 실으면
    화면이 라벨·색을 찾지 못한다. reason code · fact id · 공통점 · 차이점 같은
    내부 상세는 같은 jsonb 안에 남는다.
    """

    row = _plain(result)
    row["status"] = sim_display_status(result.status)
    row["summary"] = _sim_axis_summary(result)
    return json.dumps(row, ensure_ascii=False)


def _profile_versions(
    connection: Connection, source_profile_ids: list[str]
) -> dict[str, UUID]:
    if not source_profile_ids:
        return {}
    rows = connection.execute(
        _PROFILE_VERSIONS, {"source_profile_ids": source_profile_ids}
    ).all()
    return {row[0]: row[1] for row in rows}


def _sim_evidence(
    profile: SimCommonProfile | None,
    *,
    analysis_case_pk: UUID,
    side: str,
    sim_candidate_pk: UUID | None,
) -> list[dict[str, Any]]:
    """공통 프로파일의 항목을 근거로 편다. 좌표는 항목이 들고 온 것뿐이다.

    공통 프로파일 항목에는 문자 오프셋이 없다 (Slice 4a 계약이 Common IR
    블록까지만 들고 온다). ``start_char``·``end_char`` 는 NULL 이다.
    """

    if profile is None:
        return []
    rows: list[dict[str, Any]] = []
    for axis in SimAxis:
        for entry in profile.entries(axis):
            rows.extend(
                _evidence_rows(
                    analysis_case_pk=analysis_case_pk,
                    axis_type="SIM",
                    sim_candidate_pk=sim_candidate_pk,
                    side=side,
                    field_name=entry.source_field,
                    raw_value=entry.value_raw,
                    evidence=entry.evidence,
                )
            )
    return rows


def _sim_rows(
    connection: Connection,
    sim: SimComparisonResult,
    sim_profiles: Mapping[str, SimCommonProfile],
    analysis_case_pk: UUID,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    versions = _profile_versions(
        connection,
        [c.candidate_profile_id for c in sim.candidates if c.candidate_profile_id],
    )
    candidates: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for rank_no, candidate in enumerate(sim.candidates, start=1):
        version_pk = versions.get(candidate.candidate_profile_id or "")
        if version_pk is None:
            # kb.profile_version 이 아직 없는 후보다. FK 를 만족시킬 방법이
            # 없으므로 저장하지 않는다. 순위 번호는 원래 자리를 유지한다.
            continue
        sim_candidate_pk = uuid4()
        axes = {result.axis: result for result in candidate.axes}
        ranking = candidate.internal_ranking
        candidates.append(
            {
                "sim_candidate_pk": sim_candidate_pk,
                "analysis_case_pk": analysis_case_pk,
                "existing_profile_version_pk": version_pk,
                "rank_no": rank_no,
                # 내부 순위 컬럼이다. 핵심 축이 불충분이면 Slice 4a 계약대로
                # 점수가 None 이고 등급은 보류다. 없는 점수를 0 으로 바꾸지 않는다.
                "similarity_score": ranking.weighted_score,
                # ponytail: 우선순위 점수는 아직 계약에 없다. NULL 로 둔다.
                "priority_score": None,
                "status": _plain(ranking.review_grade),
                "title": candidate.title,
                "result_summary": _sim_result_summary(candidate.axes),
                **{
                    column: (_sim_axis_json(axes[axis]) if axis in axes else None)
                    for axis, column in _SIM_AXIS_COLUMNS.items()
                },
            }
        )
        evidence.extend(
            _sim_evidence(
                sim_profiles.get(candidate.candidate_profile_id or ""),
                analysis_case_pk=analysis_case_pk,
                side="EXISTING",
                sim_candidate_pk=sim_candidate_pk,
            )
        )
    # 요청서 쪽 SIM 근거는 후보마다 같은 것이라 후보에 붙이지 않고 한 번만
    # 남긴다. 후보 수만큼 복제하면 같은 원문이 순위표처럼 읽힌다.
    evidence.extend(
        _sim_evidence(
            sim_profiles.get(sim.request_profile_id or ""),
            analysis_case_pk=analysis_case_pk,
            side="REQUEST",
            sim_candidate_pk=None,
        )
    )
    return candidates, evidence


# ------------------------------------------------------------------ 진입점


def persist_results(
    connection: Connection,
    *,
    analysis_case_pk: UUID,
    cpl: CplResult | None = None,
    fit: FitResult | None = None,
    sim: SimComparisonResult | None = None,
    sim_profiles: Mapping[str, SimCommonProfile] | None = None,
) -> None:
    """한 케이스의 결과를 ``result.*`` 에 남긴다.

    **트랜잭션을 열지 않는다.** 호출자가 준 커넥션에 그대로 쓰므로, 호출자가
    펜싱으로 되돌리면 여기서 쓴 행도 함께 사라진다.

    빠진 단계는 예외가 아니다. FIT 이 돌지 않았으면 FIT 행이 없을 뿐,
    CPL 은 그대로 남는다 (초안 §9.4 부분 결과 보존).
    """

    parameters = {"analysis_case_pk": analysis_case_pk}
    for statement in _DELETE_STATEMENTS:
        connection.execute(statement, parameters)

    axes: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if cpl is not None:
        cpl_axes, cpl_evidence = _cpl_rows(cpl, analysis_case_pk)
        axes += cpl_axes
        evidence += cpl_evidence
    if fit is not None:
        fit_axes, fit_evidence = _fit_rows(fit, analysis_case_pk)
        axes += fit_axes
        evidence += fit_evidence

    candidates: list[dict[str, Any]] = []
    if sim is not None:
        candidates, sim_evidence = _sim_rows(
            connection, sim, dict(sim_profiles or {}), analysis_case_pk
        )
        evidence += sim_evidence

    if axes:
        connection.execute(_INSERT_AXIS, axes)
    if candidates:
        connection.execute(_INSERT_CANDIDATE, candidates)
    if evidence:
        connection.execute(_INSERT_EVIDENCE, evidence)
