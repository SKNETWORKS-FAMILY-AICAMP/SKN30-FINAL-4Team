"""Slice 6: 분석 결과가 ``result.*`` 에 남는지를 실제 PostgreSQL 로 본다.

인메모리 흉내로는 확인할 수 없는 것이 세 가지다.

1. ``axis_result.axis_type`` CHECK 에 ``SIM`` 이 없다는 것 — SIM 을 축 테이블에
   넣으려는 코드는 여기서만 걸린다.
2. ``sim_candidate.existing_profile_version_pk`` 가 NOT NULL 이고 ``kb`` 를
   참조한다는 것.
3. 임대를 뺏긴 워커가 축·후보·근거 **어느 것도** 남기지 못한다는 것. 그것은
   트랜잭션 경계가 진짜여야 확인된다.

공용 ``sims_test`` 는 건드리지 않는다. 팀원 스키마를 거기 올리면 보류 중인
tests/test_worker_queue.py 가 skip 에서 실패로 바뀐다 (dispatcher 테스트가
같은 이유로 자기 DB 를 만든다).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity, teammate_schema_installed
from app.ports.llm_client import LLMUnavailableError
from app.schemas.cpl import CplFieldCode
from app.schemas.fit import FitRelationId, FitStatus
from app.schemas.sim import SimAxis, SimReviewGrade, SimStatus
from worker.analysis_inputs import facts_at
from worker.contracts.fit_result import (
    FitEvidenceRef,
    FitRelationResult,
    FitResult,
    FitSide,
    PurposeAxisClassification,
)
from worker.contracts.cpl_result import CPL_DISPLAY_STATUSES
from worker.contracts.fit_result import FIT_DISPLAY_STATUSES
from worker.contracts.sim_result import (
    SIM_AXIS_IDS,
    SIM_DISPLAY_STATUSES,
    InternalRanking,
    SimAxisResult,
    SimCandidateResult,
    SimComparisonResult,
)
from worker.cpl import build_cpl_result
from worker.dispatcher import enqueue, run_once
from worker.jobs import claim_next
from worker.persistence import AnalysisResults, persist_results
from worker.sim_inputs import build_common_profile


_PERSISTENCE_DATABASE = "sims_persistence_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
_EXAMPLES = _ROOT / "packages" / "profile_structuring" / "examples"
_REQUEST_PATH = _EXAMPLES / "request" / "structured_profile_v012.json"
_EXISTING_PATH = _EXAMPLES / "existing" / "structured_profile_v02.json"
# 이미 만료된 임대. dispatcher 테스트와 같은 이유로 -1 을 준다.
_EXPIRED = -1
# 후보 공고 프로파일의 출처 id. kb 계보 픽스처가 이 id 로 심어진다.
_CANDIDATE_PROFILE_ID = "hwp:PBLN_000000000125016"
# 사용자 문구에 숫자가 새는지 보는 그물. 소수점·백분율·정수 어느 모양이든 걸린다.
_SCORE_LIKE = re.compile(r"\d")


# ------------------------------------------------------------------ DB 픽스처


@pytest.fixture(scope="session")
def engine() -> Engine:
    """sims 와 팀원 스키마를 한 DB 에 올린 이 파일 전용 DB."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for result persistence tests")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_PERSISTENCE_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_PERSISTENCE_DATABASE}"')
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=_PERSISTENCE_DATABASE))
    with engine.connect() as connection:
        # 팀원 파일의 `format('... %I', t)` 때문에 드라이버 커넥션에 원문을 넘긴다.
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
    with engine.connect() as connection:
        assert teammate_schema_installed(connection)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def profile_version_pk(engine: Engine) -> UUID:
    """후보 공고의 ``kb`` 계보 한 줄.

    ``sim_candidate.existing_profile_version_pk`` 가 NOT NULL 이라 이 행이
    없으면 후보를 저장할 수 없다. 파이프라인은 아직 ``kb.*`` 를 채우지 않으므로
    테스트가 직접 심는다.
    """
    sha = "40" * 32
    with engine.begin() as connection:
        notice_pk = connection.scalar(
            text(
                "INSERT INTO kb.notice (notice_id) VALUES (:notice_id)"
                " RETURNING notice_pk"
            ),
            {"notice_id": "bizinfo:PBLN_000000000125016"},
        )
        source_profile_pk = connection.scalar(
            text(
                "INSERT INTO kb.source_profile (notice_pk, source_profile_id, source_kind)"
                " VALUES (:notice_pk, :source_profile_id, 'hwp')"
                " RETURNING source_profile_pk"
            ),
            {"notice_pk": notice_pk, "source_profile_id": _CANDIDATE_PROFILE_ID},
        )
        source_version_pk = connection.scalar(
            text(
                "INSERT INTO kb.source_version (source_profile_pk, source_sha256)"
                " VALUES (:source_profile_pk, :sha) RETURNING source_version_pk"
            ),
            {"source_profile_pk": source_profile_pk, "sha": sha},
        )
        artifacts = [
            connection.scalar(
                text(
                    """
                    INSERT INTO kb.artifact (
                        source_version_pk, artifact_type, storage_bucket,
                        storage_object_key, content_sha256
                    ) VALUES (
                        :source_version_pk, :artifact_type, 'kb', :key, :sha
                    ) RETURNING artifact_pk
                    """
                ),
                {
                    "source_version_pk": source_version_pk,
                    "artifact_type": artifact_type,
                    "key": f"{_CANDIDATE_PROFILE_ID}/{artifact_type}",
                    "sha": sha,
                },
            )
            for artifact_type in ("candidate_pack", "structured_profile")
        ]
        return connection.scalar(
            text(
                """
                INSERT INTO kb.profile_version (
                    source_version_pk, schema_version, profile_sha256,
                    candidate_pack_artifact_pk, structured_artifact_pk
                ) VALUES (
                    :source_version_pk, 'existing_program_profile/v0.2', :sha,
                    :candidate_pack, :structured
                ) RETURNING profile_version_pk
                """
            ),
            {
                "source_version_pk": source_version_pk,
                "sha": sha,
                "candidate_pack": artifacts[0],
                "structured": artifacts[1],
            },
        )


@pytest.fixture
def case(engine: Engine):
    """사용자 하나와 검사 건 하나. 끝나면 자기가 만든 행만 지운다."""
    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                """
                INSERT INTO sims.app_user (login_id, email, password_hash, display_name)
                VALUES (:login_id, :email, 'x', '결과 사용자')
                RETURNING id
                """
            ),
            {
                "login_id": f"persist-{os.urandom(6).hex()}",
                "email": f"persist-{os.urandom(6).hex()}@example.test",
            },
        )
        case_id = connection.scalar(
            text(
                "INSERT INTO sims.inspection_case (owner_user_id, status)"
                " VALUES (:user_id, 'PARSING') RETURNING id"
            ),
            {"user_id": user_id},
        )
    yield case_id, user_id
    with engine.begin() as connection:
        for statement in (
            "DELETE FROM result.analysis_case WHERE user_id ="
            " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)",
            "DELETE FROM sims.inspection_case WHERE owner_user_id = :user_id",
            "DELETE FROM workspace.analysis_run WHERE user_id ="
            " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)",
            "DELETE FROM app.user_profile WHERE user_id ="
            " (SELECT external_uuid FROM sims.app_user WHERE id = :user_id)",
            "DELETE FROM sims.app_user WHERE id = :user_id",
        ):
            connection.execute(text(statement), {"user_id": user_id})


@pytest.fixture
def analysis_case_pk(engine: Engine, case) -> UUID:
    """``persist_results`` 를 직접 부르는 테스트가 쓸 결과 케이스 한 행."""
    _, user_id = case
    with engine.begin() as connection:
        external_uuid = ensure_identity(connection, user_id)
        return connection.scalar(
            text(
                """
                INSERT INTO result.analysis_case (
                    source_analysis_run_id, user_id, case_status
                ) VALUES (:run_id, :user_id, 'ready')
                RETURNING analysis_case_pk
                """
            ),
            {"run_id": uuid4(), "user_id": external_uuid},
        )


# --------------------------------------------------------------- 결과 픽스처


class _OfflineLLM:
    """포트 계약대로 실패하는 스텁. Rule 변환 결과만 남게 만든다."""

    async def generate_structured(self, **_kwargs):
        raise LLMUnavailableError("offline")


@pytest.fixture(scope="session")
def request_profile() -> dict:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def existing_profile() -> dict:
    return json.loads(_EXISTING_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def cpl(request_profile):
    return build_cpl_result(request_profile)


@pytest.fixture(scope="session")
def fit(request_profile):
    """7관계짜리 FIT 결과. 근거는 실제 프로파일 fact 에서 가져온다."""
    facts = facts_at(request_profile, "comparison_profile.purpose_goal")
    left = FitSide(
        field_names=["purpose_goal"],
        facts=[
            FitEvidenceRef(
                fact_id=fact.fact_id or "fact:unknown",
                field_name="purpose_goal",
                value_raw=fact.value_raw,
                evidence=list(fact.evidence),
            )
            for fact in facts
        ],
    )
    return FitResult(
        relations=[
            FitRelationResult(
                relation_id=relation_id,
                status=FitStatus.FIT if index else FitStatus.INSUFFICIENT,
                reason_code=None if index else "COMPARISON_EVIDENCE_MISSING",
                left=left,
                right=FitSide(),
            )
            for index, relation_id in enumerate(FitRelationId)
        ],
        purpose_axis=PurposeAxisClassification(attempted=False),
        profile_id=request_profile.get("profile_id"),
        common_ir_document_id="request:PREREVIEW-TEST-2027-03",
        model_profile="test-profile",
        ruleset_version="fit/test",
        prompt_version="fit/test",
    )


@pytest.fixture(scope="session")
def sim_parts(request_profile, existing_profile):
    """SIM 비교 결과와 근거 접지에 쓸 공통 프로파일 두 개."""
    llm = _OfflineLLM()
    request_common = build_common_profile(
        request_profile, llm, model_profile="test-profile"
    )
    candidate_common = build_common_profile(
        existing_profile, llm, model_profile="test-profile"
    )
    axes = [
        SimAxisResult(
            axis=axis,
            axis_id=SIM_AXIS_IDS[axis],
            status=(
                SimStatus.INSUFFICIENT if axis is SimAxis.DELIVERY else SimStatus.PARTIAL
            ),
            reason_code=(
                "CANDIDATE_EVIDENCE_MISSING"
                if axis is SimAxis.DELIVERY
                else "PARTIAL_OVERLAP"
            ),
        )
        for axis in SimAxis
    ]
    sim = SimComparisonResult(
        request_profile_id=request_common.source_profile_id,
        candidates=[
            SimCandidateResult(
                candidate_profile_id=candidate_common.source_profile_id,
                candidate_notice_id=candidate_common.notice_id,
                axes=axes,
                internal_ranking=InternalRanking(
                    weighted_score=0.62,
                    review_grade=SimReviewGrade.GENERAL_REVIEW,
                    assessable_axis_count=3,
                    axis_weights={"SIM-1": 0.4},
                    scoring_version="sim/test",
                ),
            )
        ],
        model_profile="test-profile",
        ruleset_version="sim/test",
        prompt_version="sim/test",
        scoring_version="sim/test",
    )
    profiles = {
        request_common.source_profile_id: request_common,
        candidate_common.source_profile_id: candidate_common,
    }
    return sim, profiles


@pytest.fixture
def results(cpl, fit, sim_parts) -> AnalysisResults:
    sim, profiles = sim_parts
    return AnalysisResults(cpl=cpl, fit=fit, sim=sim, sim_profiles=profiles)


# ------------------------------------------------------------------- 읽기 도구


def _rows(engine: Engine, statement: str, analysis_case_pk: UUID) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(statement), {"pk": analysis_case_pk}
            ).mappings()
        ]


def _axes(engine: Engine, analysis_case_pk: UUID, axis_type: str) -> list[dict]:
    return [
        row
        for row in _rows(
            engine,
            "SELECT axis_type, axis_code, status, summary_text, result_data, ordinal"
            "  FROM result.axis_result WHERE analysis_case_pk = :pk ORDER BY ordinal",
            analysis_case_pk,
        )
        if row["axis_type"] == axis_type
    ]


def _all_axes(engine: Engine, analysis_case_pk: UUID) -> list[dict]:
    return _rows(
        engine,
        "SELECT axis_type, axis_code, summary_text FROM result.axis_result"
        " WHERE analysis_case_pk = :pk",
        analysis_case_pk,
    )


def _candidates(engine: Engine, analysis_case_pk: UUID) -> list[dict]:
    return _rows(
        engine,
        "SELECT rank_no, status, similarity_score, priority_score,"
        "       purpose_result, target_result, support_result, delivery_result,"
        "       existing_profile_version_pk"
        "  FROM result.sim_candidate WHERE analysis_case_pk = :pk ORDER BY rank_no",
        analysis_case_pk,
    )


def _evidence(engine: Engine, analysis_case_pk: UUID) -> list[dict]:
    return _rows(
        engine,
        "SELECT axis_type, side, usage_scope, field_name, raw_value, source_sha256,"
        "       candidate_pack_block_id, start_char, end_char,"
        "       common_ir_document_id, common_ir_block_id, common_ir_occurrence_ids,"
        "       axis_result_pk, sim_candidate_pk,"
        "       existing_profile_version_pk, existing_fact_pk"
        "  FROM result.evidence_snapshot WHERE analysis_case_pk = :pk",
        analysis_case_pk,
    )


def _persist(engine: Engine, analysis_case_pk: UUID, results: AnalysisResults) -> None:
    with engine.begin() as connection:
        persist_results(
            connection,
            analysis_case_pk=analysis_case_pk,
            cpl=results.cpl,
            fit=results.fit,
            sim=results.sim,
            sim_profiles=results.sim_profiles,
        )


def _complete_case(engine: Engine, case_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sims.inspection_case"
                "   SET status = 'COMPLETED', completed_at = now(),"
                "       result_frozen_at = now()"
                " WHERE id = :case_id"
            ),
            {"case_id": case_id},
        )


# --------------------------------------------------------------------- CPL·FIT


def test_CPL_13항목이_선언_순서로_axis_result_에_남는다(
    engine, analysis_case_pk, results, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)

    rows = _axes(engine, analysis_case_pk, "CPL")
    # 프론트 표시 코드는 선언 순번이다. 내부 어휘 이름은 result_data 에 남는다.
    assert [row["axis_code"] for row in rows] == [
        f"CPL-{n:02d}" for n in range(1, len(list(CplFieldCode)) + 1)
    ]
    assert [row["ordinal"] for row in rows] == list(range(len(CplFieldCode)))
    # 이 컬럼은 프론트 RPC 가 읽는 자리다. 표시 어휘 넷 밖의 값은 나가지 않는다.
    assert {row["status"] for row in rows} <= CPL_DISPLAY_STATUSES
    assert "UNDETERMINED" not in {row["status"] for row in rows}
    # 하위 필드의 프로파일 상태 원본은 result_data 안에 그대로 남는다.
    subfield_statuses = {
        sub["status"]
        for row in rows
        for sub in row["result_data"]["subfields"]
        if sub["status"]
    }
    assert "identified" in subfield_statuses


def test_FIT_7관계가_FIT_코드로_남는다(
    engine, analysis_case_pk, results, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)

    rows = _axes(engine, analysis_case_pk, "FIT")
    # 프론트 계약은 ``FIT-07`` 로 받는다. 내부 id 는 ``FIT-7`` 그대로다.
    assert [row["axis_code"] for row in rows] == [f"FIT-{n:02d}" for n in range(1, 8)]
    assert [relation.value for relation in FitRelationId] == [
        f"FIT-{n}" for n in range(1, 8)
    ]
    assert rows[0]["status"] == FitStatus.INSUFFICIENT.value
    assert {row["status"] for row in rows} <= FIT_DISPLAY_STATUSES


# ------------------------------------------------------------------------ SIM


def test_SIM_은_후보_행에만_남고_axis_result_에는_들어가지_않는다(
    engine, analysis_case_pk, results, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)

    # CHECK 이 거부했을 값이다. 코드가 시도조차 하지 않았음을 고정한다.
    assert [row for row in _all_axes(engine, analysis_case_pk) if row["axis_type"] == "SIM"] == []

    candidates = _candidates(engine, analysis_case_pk)
    assert len(candidates) == 1
    row = candidates[0]
    assert row["rank_no"] == 1
    assert row["existing_profile_version_pk"] == profile_version_pk
    # 4축은 네 jsonb 컬럼이 자리다.
    assert row["purpose_result"]["axis_id"] == "SIM-1"
    assert row["target_result"]["axis_id"] == "SIM-2"
    assert row["support_result"]["axis_id"] == "SIM-3"
    assert row["delivery_result"]["axis_id"] == "SIM-4"
    # 축 상태는 소문자로 나간다. 내부 SimStatus 는 대문자 그대로다.
    assert row["delivery_result"]["status"] == "insufficient"
    assert SimStatus.INSUFFICIENT.value == "INSUFFICIENT"
    assert {
        row[column]["status"]
        for column in (
            "purpose_result",
            "target_result",
            "support_result",
            "delivery_result",
        )
    } <= SIM_DISPLAY_STATUSES
    # 축 jsonb 안의 내부 상세는 그대로 남는다.
    assert "reason_code" in row["delivery_result"]


def test_핵심축이_불충분한_후보는_점수가_없고_등급은_보류다(
    engine, analysis_case_pk, cpl, sim_parts, profile_version_pk
):
    """Slice 4a 계약: 부족한 근거를 낮은 점수로 바꿔치기하지 않는다."""
    sim, profiles = sim_parts
    held = SimCandidateResult(
        candidate_profile_id=sim.candidates[0].candidate_profile_id,
        candidate_notice_id=sim.candidates[0].candidate_notice_id,
        axes=sim.candidates[0].axes,
        internal_ranking=InternalRanking(
            weighted_score=None,
            review_grade=SimReviewGrade.ON_HOLD,
            assessable_axis_count=2,
            scoring_version="sim/test",
        ),
    )
    _persist(
        engine,
        analysis_case_pk,
        AnalysisResults(
            cpl=cpl,
            sim=SimComparisonResult(
                request_profile_id=sim.request_profile_id,
                candidates=[held],
                model_profile="test-profile",
                ruleset_version="sim/test",
                prompt_version="sim/test",
                scoring_version="sim/test",
            ),
            sim_profiles=profiles,
        ),
    )

    row = _candidates(engine, analysis_case_pk)[0]
    assert row["similarity_score"] is None
    assert row["status"] == SimReviewGrade.ON_HOLD.value


# ------------------------------------------------------------------------ 근거


def test_근거는_실제_블록과_오프셋을_들고_요청서_공고를_구분한다(
    engine, analysis_case_pk, results, request_profile, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)
    rows = _evidence(engine, analysis_case_pk)

    assert rows and all(row["usage_scope"] == "RESULT" for row in rows)
    assert {row["side"] for row in rows} == {"REQUEST", "EXISTING"}
    # 공고 근거는 후보 행에 붙고, 요청서 근거는 붙지 않는다.
    assert all(
        row["sim_candidate_pk"] is not None
        for row in rows
        if row["side"] == "EXISTING"
    )
    assert all(row["axis_type"] == "SIM" for row in rows if row["side"] == "EXISTING")
    # 요청서 쪽 SIM 근거는 후보마다 복제하지 않고 한 벌만 남는다.
    sim_request = [
        row for row in rows if row["axis_type"] == "SIM" and row["side"] == "REQUEST"
    ]
    assert sim_request and all(row["sim_candidate_pk"] is None for row in sim_request)

    # 좌표는 픽스처 프로파일의 것 그대로다. 지어낸 값이 아니다.
    fact = facts_at(request_profile, "comparison_profile.purpose_goal")[0]
    grounded = [
        row
        for row in rows
        if row["axis_type"] == "CPL" and row["raw_value"] == fact.value_raw
    ]
    assert grounded
    assert grounded[0]["common_ir_block_id"] == fact.evidence[0].common_ir_block_id
    assert grounded[0]["common_ir_document_id"] == fact.evidence[0].common_ir_document_id
    assert grounded[0]["start_char"] == fact.start_char
    assert grounded[0]["end_char"] == fact.end_char
    assert grounded[0]["candidate_pack_block_id"] == fact.evidence[0].source_block_id
    assert grounded[0]["source_sha256"] == results.cpl.common_ir_source_sha256
    assert grounded[0]["common_ir_occurrence_ids"] == list(
        fact.evidence[0].common_ir_occurrence_ids
    )

    # kb 는 아직 이 경로로 채워지지 않는다. 링크를 지어내지 않고 NULL 로 둔다.
    assert all(row["existing_profile_version_pk"] is None for row in rows)
    assert all(row["existing_fact_pk"] is None for row in rows)
    # 원문 없는 근거 행은 만들지 않는다 (raw_value 는 NOT NULL 이다).
    assert all(row["raw_value"] for row in rows)


def test_점수는_summary_text_로_새지_않고_result_data_와_순위_컬럼에_산다(
    engine, analysis_case_pk, results, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)

    for row in _all_axes(engine, analysis_case_pk):
        assert row["summary_text"] is None or not _SCORE_LIKE.search(row["summary_text"])

    candidate = _candidates(engine, analysis_case_pk)[0]
    # NUMERIC 은 Decimal 로 돌아온다. 점수가 있어야 할 자리에 있다는 것만 본다.
    assert float(candidate["similarity_score"]) == pytest.approx(0.62)
    assert candidate["priority_score"] is None
    # 축 jsonb 에는 점수 필드가 아예 없다 (Slice 4a 계약).
    assert "weighted_score" not in candidate["purpose_result"]

    # CPL 세부는 result_data 에 통째로 남는다 — 표시 문구가 아니라 데이터다.
    cpl_rows = _axes(engine, analysis_case_pk, "CPL")
    assert cpl_rows[0]["result_data"]["field_code"] == CplFieldCode.REQUEST_TYPE.value
    assert cpl_rows[0]["result_data"]["subfields"]


# --------------------------------------------------------------------- 재실행


def test_같은_케이스를_다시_저장해도_행이_늘지_않는다(
    engine, analysis_case_pk, results, profile_version_pk
):
    _persist(engine, analysis_case_pk, results)
    first = (
        len(_all_axes(engine, analysis_case_pk)),
        len(_candidates(engine, analysis_case_pk)),
        len(_evidence(engine, analysis_case_pk)),
    )

    _persist(engine, analysis_case_pk, results)

    assert first == (
        len(_all_axes(engine, analysis_case_pk)),
        len(_candidates(engine, analysis_case_pk)),
        len(_evidence(engine, analysis_case_pk)),
    )


def test_FIT_이_없으면_FIT_행만_없고_CPL_은_그대로_남는다(
    engine, analysis_case_pk, cpl
):
    """초안 §9.4 부분 결과 보존. 빠진 단계는 예외가 아니다."""
    _persist(engine, analysis_case_pk, AnalysisResults(cpl=cpl))

    rows = _all_axes(engine, analysis_case_pk)
    assert len(_axes(engine, analysis_case_pk, "CPL")) == len(CplFieldCode)
    assert [row for row in rows if row["axis_type"] == "FIT"] == []
    assert _candidates(engine, analysis_case_pk) == []


# ------------------------------------------------------------------ 펜싱 왕복


def _case_pk(engine: Engine, run_id: UUID) -> UUID | None:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT analysis_case_pk FROM result.analysis_case"
                " WHERE source_analysis_run_id = :run_id"
            ),
            {"run_id": run_id},
        )


def test_run_once_는_한_트랜잭션에서_케이스와_결과를_함께_쓴다(
    engine, case, results, profile_version_pk
):
    case_id, _ = case
    run_id = enqueue(engine, case_id)

    async def analyse(claimed_case_id: int) -> AnalysisResults:
        _complete_case(engine, claimed_case_id)
        return results

    assert asyncio.run(run_once(engine, analyse, worker_id="w1")) == run_id

    analysis_case_pk = _case_pk(engine, run_id)
    assert analysis_case_pk is not None
    assert len(_all_axes(engine, analysis_case_pk)) == len(CplFieldCode) + len(
        FitRelationId
    )
    assert len(_candidates(engine, analysis_case_pk)) == 1
    assert _evidence(engine, analysis_case_pk)


def test_임대를_뺏긴_워커는_축_후보_근거_어느_것도_남기지_못한다(
    engine, case, results, profile_version_pk
):
    """가장 중요한 테스트다. 결과 쓰기가 펜싱 밖으로 새면 여기서 걸린다."""
    case_id, _ = case
    run_id = enqueue(engine, case_id)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_analysis(claimed_case_id: int) -> AnalysisResults:
        started.set()
        await release.wait()
        _complete_case(engine, claimed_case_id)
        return results

    async def scenario() -> UUID | None:
        worker_a = asyncio.create_task(
            run_once(engine, slow_analysis, worker_id="A", lease_seconds=_EXPIRED)
        )
        await started.wait()
        with engine.begin() as connection:
            stolen = claim_next(connection, worker_id="B", lease_seconds=300)
        assert stolen is not None and stolen.analysis_run_pk == run_id
        release.set()
        return await worker_a

    assert asyncio.run(scenario()) == run_id

    # 케이스 행부터 없다 — 그러므로 축·후보·근거도 있을 수 없다.
    assert _case_pk(engine, run_id) is None
    with engine.connect() as connection:
        for table in (
            "result.axis_result",
            "result.sim_candidate",
            "result.evidence_snapshot",
        ):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
