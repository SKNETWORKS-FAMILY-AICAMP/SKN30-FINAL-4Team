"""Slice 6a: 분석 조립기와 큐 결과 저장의 좁은 검증."""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import teammate_schema_installed
from app.ports.llm_client import LLMUnavailableError
from app.schemas.sim import SimAxis, SimReviewGrade, SimStatus
from app.infrastructure.local_object_storage import LocalObjectStorage
from worker import analysis
from worker.contracts.profile_snapshot import CommonIrArtifact, ProfileSnapshot
from worker.contracts.sim_result import (
    InternalRanking,
    SimAxisResult,
    SimCandidateResult,
)
from worker.dispatcher import enqueue, run_once
from worker.jobs import claim_next
from worker.kb_ingest import CandidateComparison, KbCandidate
from worker.persistence import AnalysisResults
from worker.profiles import StageError
from worker.sim_inputs import build_common_profile


_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
_EXAMPLES = _ROOT / "packages" / "profile_structuring" / "examples"
_REQUEST_PATH = _EXAMPLES / "request" / "structured_profile_v012.json"
_COMMON_IR_PATH = _EXAMPLES / "existing" / "common_ir_hwp_v1.json"
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
_DATABASE = "sims_analysis_slice6a_test"
_CANDIDATE_ID = "hwp:SLICE6A-CANDIDATE"


class _OfflineLLM:
    async def generate_structured(self, **_kwargs: Any) -> Any:
        raise LLMUnavailableError("offline")


def _request_profile() -> dict[str, Any]:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


def _common_ir_artifact() -> CommonIrArtifact:
    document = json.loads(_COMMON_IR_PATH.read_text(encoding="utf-8"))
    meta = document["document"]
    return CommonIrArtifact(
        run_dir="test-run",
        notice_id="SLICE6A",
        source_kind=meta["source_kind"],
        source_path=meta["provenance"]["source_location"],
        source_sha256=meta["provenance"]["source_sha256"],
        common_ir_path="test/common_ir.json",
        common_ir_document_id=meta["document_id"],
        manifest={},
        block_count=len(document.get("blocks", [])),
        document=document,
    )


def _candidate_result(profile_id: str) -> SimCandidateResult:
    axes = [
        SimAxisResult(
            axis=axis,
            axis_id=f"SIM-{index}",
            status=SimStatus.INSUFFICIENT,
            reason_code="CANDIDATE_EVIDENCE_MISSING",
        )
        for index, axis in enumerate(SimAxis, start=1)
    ]
    return SimCandidateResult(
        candidate_profile_id=profile_id,
        candidate_notice_id="bizinfo:SLICE6A",
        axes=axes,
        internal_ranking=InternalRanking(
            weighted_score=None,
            review_grade=SimReviewGrade.ON_HOLD,
            assessable_axis_count=0,
            scoring_version="sim-alpha-v0.2",
        ),
    )


def _candidate_common():
    profile = json.loads(
        (_EXAMPLES / "existing" / "structured_profile_v02.json").read_text(
            encoding="utf-8"
        )
    )
    profile["source_profile_id"] = _CANDIDATE_ID
    profile["notice_id"] = "bizinfo:SLICE6A"
    return build_common_profile(
        profile,
        _OfflineLLM(),
        model_profile="test-profile",
    )


def test_run_analysis_assembles_existing_stages(monkeypatch):
    candidate = KbCandidate(
        announcement_version_id=7,
        pblanc_nm="테스트 공고",
        source_profile_id=_CANDIDATE_ID,
        distance=0.1,
    )
    comparison = CandidateComparison(
        announcement_version_id=7,
        source_profile_id=_CANDIDATE_ID,
        result=_candidate_result(_CANDIDATE_ID),
        candidate_common=_candidate_common(),
    )
    monkeypatch.setattr(analysis, "search_candidates", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(
        analysis,
        "compare_kb_candidates",
        lambda *args, **kwargs: [comparison],
    )

    result = analysis.run_analysis(
        _request_profile(),
        _common_ir_artifact(),
        cast(Engine, object()),
        _OfflineLLM(),
        embedding_profile_id=11,
    )

    assert result.cpl is not None
    assert len(result.cpl.items) == 13
    assert result.fit is not None
    assert len(result.fit.relations) == 7
    assert result.sim is not None
    assert [row.candidate_profile_id for row in result.sim.candidates] == [_CANDIDATE_ID]
    assert result.sim.candidates[0].title == "테스트 공고"
    assert result.sim_profiles


def test_run_analysis_keeps_cpl_fit_when_embedding_is_disabled():
    result = analysis.run_analysis(
        _request_profile(),
        None,
        cast(Engine, object()),
        None,
        embedding_profile_id=None,
    )

    assert result.cpl is not None
    assert result.fit is not None
    assert result.sim is not None
    assert result.sim.candidates == []
    assert any(
        diagnostic.reason_code == "RETRIEVAL_NOT_READY"
        for diagnostic in result.sim.diagnostics
    )


def test_run_analysis_keeps_candidate_failure_local(monkeypatch):
    candidate = KbCandidate(
        announcement_version_id=8,
        pblanc_nm="실패 공고",
        source_profile_id=_CANDIDATE_ID,
        distance=0.2,
    )
    comparison = CandidateComparison(
        announcement_version_id=8,
        source_profile_id=_CANDIDATE_ID,
        reason_code="CANDIDATE_PROFILE_MISSING",
    )
    monkeypatch.setattr(analysis, "search_candidates", lambda *args, **kwargs: [candidate])
    monkeypatch.setattr(
        analysis,
        "compare_kb_candidates",
        lambda *args, **kwargs: [comparison],
    )

    result = analysis.run_analysis(
        _request_profile(),
        None,
        cast(Engine, object()),
        None,
        embedding_profile_id=11,
    )

    assert result.cpl is not None
    assert result.fit is not None
    assert result.sim is not None
    assert len(result.sim.candidates) == 1
    assert all(
        axis.status is SimStatus.INSUFFICIENT
        for axis in result.sim.candidates[0].axes
    )
    assert result.sim.candidates[0].axes[0].reason_code == "CANDIDATE_PROFILE_MISSING"


def test_run_analysis_routes_fit_and_sim_model_profiles(monkeypatch):
    """분석 조립기의 단일 model_profile 호환성과 분리 프로필 배선을 고정한다."""

    seen: dict[str, str] = {}
    original_fit = analysis.analyze_fit
    original_build = analysis.build_common_profile

    def fit_with_profile(*args, **kwargs):
        seen["fit"] = kwargs["model_profile"]
        return original_fit(*args, **kwargs)

    def build_with_profile(*args, **kwargs):
        seen["build"] = kwargs["model_profile"]
        return original_build(*args, **kwargs)

    candidate = KbCandidate(
        announcement_version_id=9,
        pblanc_nm="프로필 배선 공고",
        source_profile_id=_CANDIDATE_ID,
        distance=0.1,
    )

    def compare_with_profile(*args, **kwargs):
        seen["compare"] = kwargs["model_profile"]
        return []

    monkeypatch.setattr(analysis, "analyze_fit", fit_with_profile)
    monkeypatch.setattr(analysis, "build_common_profile", build_with_profile)
    monkeypatch.setattr(
        analysis, "search_candidates", lambda *args, **kwargs: [candidate]
    )
    monkeypatch.setattr(analysis, "compare_kb_candidates", compare_with_profile)

    result = analysis.run_analysis(
        _request_profile(),
        None,
        cast(Engine, object()),
        _OfflineLLM(),
        embedding_profile_id=11,
        model_profile="legacy-profile",
        cpl_model_profile="cpl-profile",
        fit_model_profile="fit-profile",
        sim_model_profile="sim-profile",
    )

    assert seen == {
        "fit": "fit-profile",
        "build": "sim-profile",
        "compare": "sim-profile",
    }
    assert result.sim is not None
    assert result.sim.model_profile == "sim-profile"


@pytest.fixture(scope="module")
def pg_engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for Slice 6a persistence test")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_DATABASE}"')
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=_DATABASE))
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted((_MIGRATIONS / "supabase").glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        assert teammate_schema_installed(connection)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_case(pg_engine: Engine) -> tuple[int, int]:
    with pg_engine.begin() as connection:
        user_id = connection.scalar(
            text(
                "INSERT INTO sims.app_user (login_id, email, password_hash, display_name)"
                " VALUES (:login_id, :email, 'x', 'Slice 6a') RETURNING id"
            ),
            {
                "login_id": f"slice6a-{os.urandom(6).hex()}",
                "email": f"slice6a-{os.urandom(6).hex()}@example.test",
            },
        )
        case_id = connection.scalar(
            text(
                "INSERT INTO sims.inspection_case (owner_user_id, status)"
                " VALUES (:user_id, 'PARSING') RETURNING id"
            ),
            {"user_id": user_id},
        )
    yield int(case_id), int(user_id)
    with pg_engine.begin() as connection:
        external_uuid = connection.scalar(
            text("SELECT external_uuid FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )
        connection.execute(
            text("DELETE FROM result.analysis_case WHERE user_id = :user_id"),
            {"user_id": external_uuid},
        )
        connection.execute(
            text("DELETE FROM workspace.analysis_run WHERE user_id = :user_id"),
            {"user_id": external_uuid},
        )
        connection.execute(
            text("DELETE FROM sims.inspection_case WHERE owner_user_id = :user_id"),
            {"user_id": user_id},
        )
        connection.execute(
            text("DELETE FROM app.user_profile WHERE user_id = :user_id"),
            {"user_id": external_uuid},
        )
        connection.execute(
            text("DELETE FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )


@pytest.fixture(scope="module")
def candidate_profile_version(pg_engine: Engine) -> UUID:
    sha = "61" * 32
    with pg_engine.begin() as connection:
        notice_pk = connection.scalar(
            text("INSERT INTO kb.notice (notice_id) VALUES (:id) RETURNING notice_pk"),
            {"id": "bizinfo:SLICE6A"},
        )
        source_profile_pk = connection.scalar(
            text(
                "INSERT INTO kb.source_profile (notice_pk, source_profile_id, source_kind)"
                " VALUES (:notice_pk, :profile_id, 'hwp') RETURNING source_profile_pk"
            ),
            {"notice_pk": notice_pk, "profile_id": _CANDIDATE_ID},
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
                    "INSERT INTO kb.artifact (source_version_pk, artifact_type, storage_bucket,"
                    " storage_object_key, content_sha256) VALUES (:version_pk, :kind, 'kb',"
                    " :key, :sha) RETURNING artifact_pk"
                ),
                {
                    "version_pk": source_version_pk,
                    "kind": kind,
                    "key": f"{_CANDIDATE_ID}/{kind}",
                    "sha": sha,
                },
            )
            for kind in ("candidate_pack", "structured_profile")
        ]
        profile_version_pk = connection.scalar(
            text(
                "INSERT INTO kb.profile_version (source_version_pk, schema_version,"
                " profile_sha256, candidate_pack_artifact_pk, structured_artifact_pk)"
                " VALUES (:version_pk, 'existing_program_profile/v0.2', :sha,"
                " :candidate_pack, :structured) RETURNING profile_version_pk"
            ),
            {
                "version_pk": source_version_pk,
                "sha": sha,
                "candidate_pack": artifacts[0],
                "structured": artifacts[1],
            },
        )
        fact_pk = connection.scalar(
            text(
                "INSERT INTO kb.fact_occurrence ("
                " profile_version_pk, fact_id, fact_scope, field_name, value_raw,"
                " status, scope, source_block_id, start_char, end_char, text_basis, ordinal"
                ") VALUES ("
                " :profile_version_pk, 'fact:activity', 'comparison',"
                " 'support_activities', '사업활동 지원', 'identified', 'notice',"
                " 'block:activity', 0, 8, 'common_ir_v1_candidate_pack', 0"
                ") RETURNING fact_pk"
            ),
            {"profile_version_pk": profile_version_pk},
        )
        connection.execute(
            text(
                "INSERT INTO kb.fact_evidence ("
                " fact_pk, source_block_id, section_id, common_ir_document_id,"
                " common_ir_block_id, common_ir_occurrence_ids, ordinal"
                ") VALUES ("
                " :fact_pk, 'block:activity', 'main_notice', :document_id,"
                " 'block:activity', ARRAY['occ:activity'], 0"
                ")"
            ),
            {"fact_pk": fact_pk, "document_id": _CANDIDATE_ID},
        )
        return profile_version_pk


def test_run_once_persists_analysis_results(
    pg_engine: Engine,
    pg_case: tuple[int, int],
    candidate_profile_version: UUID,
    monkeypatch,
):
    candidate = KbCandidate(
        announcement_version_id=7,
        pblanc_nm="테스트 공고",
        source_profile_id=_CANDIDATE_ID,
        distance=0.1,
        kb_profile_version_pk=candidate_profile_version,
    )
    monkeypatch.setattr(analysis, "search_candidates", lambda *args, **kwargs: [candidate])

    case_id, _ = pg_case
    run_id = enqueue(pg_engine, case_id)

    async def analyse(claimed_case_id: int):
        with pg_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sims.inspection_case"
                    "   SET status = 'COMPLETED', completed_at = now(),"
                    "       result_frozen_at = now()"
                    " WHERE id = :id"
                ),
                {"id": claimed_case_id},
            )
        return await asyncio.to_thread(
            analysis.run_analysis,
            _request_profile(),
            _common_ir_artifact(),
            pg_engine,
            _OfflineLLM(),
            None,
            embedding_profile_id=11,
        )

    assert asyncio.run(run_once(pg_engine, analyse, worker_id="slice6a")) == run_id
    with pg_engine.connect() as connection:
        analysis_case_pk = connection.scalar(
            text(
                "SELECT analysis_case_pk FROM result.analysis_case"
                " WHERE source_analysis_run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        assert analysis_case_pk is not None
        axes = dict(
            connection.execute(
                text(
                    "SELECT axis_type, count(*) FROM result.axis_result"
                    " WHERE analysis_case_pk = :pk GROUP BY axis_type"
                ),
                {"pk": analysis_case_pk},
            ).all()
        )
        # CPL 13항목·FIT 7관계가 그대로 자리를 잡아야 조립기가 돈 것이다.
        assert axes == {"CPL": 13, "FIT": 7}
        assert connection.scalar(
            text(
                "SELECT count(*) FROM result.sim_candidate WHERE analysis_case_pk = :pk"
            ),
            {"pk": analysis_case_pk},
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM result.evidence_snapshot"
                " WHERE analysis_case_pk = :pk AND side = 'EXISTING'"
            ),
            {"pk": analysis_case_pk},
        ) > 0


def _result_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in (
                "result.axis_result",
                "result.sim_candidate",
                "result.evidence_snapshot",
            )
        }


def test_임대를_뺏긴_워커는_조립_결과도_한_행도_남기지_못한다(
    pg_engine: Engine,
    pg_case: tuple[int, int],
    candidate_profile_version: UUID,
    monkeypatch,
):
    """조립기를 붙인 뒤에도 펜싱은 그대로다 (초안 §10).

    A 가 조립까지 끝냈더라도 임대를 뺏겼으면 케이스·축·후보·근거 어느 것도
    남지 않는다. 네 테이블은 한 트랜잭션 안에 있다.
    """

    candidate = KbCandidate(
        announcement_version_id=7,
        pblanc_nm="테스트 공고",
        source_profile_id=_CANDIDATE_ID,
        distance=0.1,
        kb_profile_version_pk=candidate_profile_version,
    )
    monkeypatch.setattr(analysis, "search_candidates", lambda *args, **kwargs: [candidate])

    case_id, _ = pg_case
    before = _result_counts(pg_engine)
    run_id = enqueue(pg_engine, case_id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def analyse(claimed_case_id: int):
        started.set()
        await release.wait()
        with pg_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE sims.inspection_case SET status = 'COMPLETED',"
                    " completed_at = now(), result_frozen_at = now() WHERE id = :id"
                ),
                {"id": claimed_case_id},
            )
        return await asyncio.to_thread(
            analysis.run_analysis,
            _request_profile(),
            _common_ir_artifact(),
            pg_engine,
            _OfflineLLM(),
            None,
            embedding_profile_id=11,
        )

    async def scenario() -> UUID | None:
        worker_a = asyncio.create_task(
            run_once(pg_engine, analyse, worker_id="A", lease_seconds=-1)
        )
        await started.wait()
        with pg_engine.begin() as connection:
            stolen = claim_next(connection, worker_id="B", lease_seconds=300)
        assert stolen is not None and stolen.analysis_run_pk == run_id
        release.set()
        return await worker_a

    assert asyncio.run(scenario()) == run_id

    with pg_engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM result.analysis_case"
                " WHERE source_analysis_run_id = :run_id"
            ),
            {"run_id": run_id},
        ) == 0
        assert _result_counts(pg_engine) == before


@pytest.fixture
def uploaded_case(pg_engine: Engine, pg_case: tuple[int, int], tmp_path):
    """업로드된 원본 하나를 가진 케이스. 정리는 pg_case 의 케이스 삭제가 한다."""
    case_id, user_id = pg_case
    storage = LocalObjectStorage(tmp_path)
    storage_key = f"cases/{case_id}/source.hwpx"
    asyncio.run(storage.put(storage_key, io.BytesIO(b"hwpx-bytes")))
    with pg_engine.begin() as connection:
        asset_id = connection.scalar(
            text(
                "INSERT INTO sims.file_asset (asset_scope, owner_user_id,"
                " inspection_case_id, storage_key, original_filename, extension)"
                " VALUES ('USER', :user_id, :case_id, :key, '요청서.hwpx', 'hwpx')"
                " RETURNING id"
            ),
            {"user_id": user_id, "case_id": case_id, "key": storage_key},
        )
        connection.execute(
            text(
                "INSERT INTO sims.uploaded_document (inspection_case_id,"
                " file_asset_id, declared_format) VALUES (:case_id, :asset_id, 'HWPX')"
            ),
            {"case_id": case_id, "asset_id": asset_id},
        )
    return case_id, storage


def _stub_profile_chain(monkeypatch, artifact: CommonIrArtifact, status: str):
    """파싱·구조화는 이 테스트의 대상이 아니다. 앞뒤 배선만 남기고 고정한다."""

    seen: dict[str, Any] = {}

    def fake_parse(*, input_path, notice_id, source_kind, run_dir):
        seen["source"] = Path(input_path).read_bytes()
        seen["source_kind"] = source_kind
        seen["notice_id"] = notice_id
        return artifact

    monkeypatch.setattr(analysis, "parse_to_common_ir", fake_parse)
    monkeypatch.setattr(analysis, "build_pack", lambda document: object())
    monkeypatch.setattr(analysis, "make_vllm_selector", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        analysis,
        "structure_request_profile",
        lambda **kwargs: ProfileSnapshot(
            profile_id=kwargs["profile_id"],
            status=status,
            profile=_request_profile() if status == "OK" else None,
            candidate_pack_id="pack",
            common_ir=artifact,
            model_id=kwargs["model_id"],
            prompt_version="test",
            profile_contract_version="test",
            selection_attempts=1,
        ),
    )
    return seen


def test_analyse_case는_업로드_원본에서_결과를_조립한다(
    pg_engine: Engine, uploaded_case, monkeypatch
):
    case_id, storage = uploaded_case
    seen = _stub_profile_chain(monkeypatch, _common_ir_artifact(), "OK")

    result = analysis.analyse_case(pg_engine, storage, _OfflineLLM(), case_id)

    # 원본은 케이스 → 업로드 문서 → 파일 자산을 타고 저장소에서 온다.
    assert seen["source"] == b"hwpx-bytes"
    assert seen["source_kind"] == "hwpx"
    assert result.cpl is not None and len(result.cpl.items) == 13
    assert result.fit is not None and len(result.fit.relations) == 7
    # 프로필의 identity.title_raw가 비어 있으므로 파일명·계층명으로 보완하지 않는다.
    assert result.program_name is None
    assert result.original_filename == "요청서.hwpx"


def test_analyse_case_uses_cpl_profile_for_request_selection(
    pg_engine: Engine, uploaded_case, monkeypatch
):
    case_id, storage = uploaded_case
    artifact = _common_ir_artifact()
    _stub_profile_chain(monkeypatch, artifact, "OK")
    seen: dict[str, str] = {}

    def selector(*args, **kwargs):
        seen["selector"] = kwargs["model_profile"]
        return object()

    def structure(**kwargs):
        seen["structure"] = kwargs["model_id"]
        return ProfileSnapshot(
            profile_id=kwargs["profile_id"],
            status="OK",
            profile=_request_profile(),
            candidate_pack_id="pack",
            common_ir=artifact,
            model_id=kwargs["model_id"],
            prompt_version="test",
            profile_contract_version="test",
            selection_attempts=1,
        )

    def assemble(*args, **kwargs):
        seen["base"] = kwargs["model_profile"]
        seen["fit"] = kwargs["fit_model_profile"]
        seen["sim"] = kwargs["sim_model_profile"]
        return AnalysisResults(cpl=analysis.build_cpl_result(_request_profile()))

    monkeypatch.setattr(analysis, "make_vllm_selector", selector)
    monkeypatch.setattr(analysis, "structure_request_profile", structure)
    monkeypatch.setattr(analysis, "run_analysis", assemble)

    result = analysis.analyse_case(
        pg_engine,
        storage,
        _OfflineLLM(),
        case_id,
        embedding_profile_id=11,
        model_profile="legacy-profile",
        cpl_model_profile="cpl-profile",
        fit_model_profile="fit-profile",
        sim_model_profile="sim-profile",
    )

    assert isinstance(result, AnalysisResults)
    assert seen == {
        "selector": "cpl-profile",
        "structure": "cpl-profile",
        "base": "legacy-profile",
        "fit": "fit-profile",
        "sim": "sim-profile",
    }


def test_요청서_프로파일이_실패하면_반쪽_결과를_만들지_않는다(
    pg_engine: Engine, uploaded_case, monkeypatch
):
    case_id, storage = uploaded_case
    _stub_profile_chain(monkeypatch, _common_ir_artifact(), "FAILED")

    with pytest.raises(StageError):
        analysis.analyse_case(pg_engine, storage, _OfflineLLM(), case_id)
