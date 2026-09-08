"""PostgreSQL contract tests for the owner-scoped result RPCs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


_BACKEND = Path(__file__).resolve().parents[1]
_SCHEMA = _BACKEND / "app" / "db" / "schema.sql"
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_SUPABASE_MIGRATIONS = _MIGRATIONS / "supabase"
_DATABASE = "sims_result_api_rpc_test"
_FORBIDDEN_SCORE_KEYS = {
    "similarity_score",
    "priority_score",
    "weighted_score",
    "percent",
    "score",
}


def _contains_forbidden_score_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in _FORBIDDEN_SCORE_KEYS or _contains_forbidden_score_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_score_key(item) for item in value)
    return False


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for result API RPC tests")

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

    value = create_engine(url.set(database=_DATABASE))
    with value.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute(_SCHEMA.read_text(encoding="utf-8"))
        for path in sorted(_SUPABASE_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            raw.execute(path.read_text(encoding="utf-8"))
        raw.commit()
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> dict[str, Any]:
    owner_id = uuid4()
    other_user_id = uuid4()
    case_id = uuid4()
    other_case_id = uuid4()
    no_session_case_id = uuid4()
    expired_artifact_case_id = uuid4()
    cpl_axis_id = uuid4()
    fit_axis_id = uuid4()
    sim_candidate_id = uuid4()
    second_candidate_id = uuid4()
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO auth.users (id) VALUES (:id), (:other_id)"),
            {"id": owner_id, "other_id": other_user_id},
        )

        profile_ids: list[UUID] = []
        for number in (1, 2):
            notice_pk = connection.scalar(
                text(
                    "INSERT INTO kb.notice (notice_id) VALUES (:notice_id) "
                    "RETURNING notice_pk"
                ),
                {"notice_id": f"rpc-notice-{number}"},
            )
            source_profile_pk = connection.scalar(
                text(
                    "INSERT INTO kb.source_profile "
                    "(notice_pk, source_profile_id, source_kind) "
                    "VALUES (:notice_pk, :profile_id, 'markdown_fixture') "
                    "RETURNING source_profile_pk"
                ),
                {
                    "notice_pk": notice_pk,
                    "profile_id": f"rpc-profile-{number}",
                },
            )
            source_version_pk = connection.scalar(
                text(
                    "INSERT INTO kb.source_version "
                    "(source_profile_pk, source_sha256) "
                    "VALUES (:source_profile_pk, :sha) "
                    "RETURNING source_version_pk"
                ),
                {
                    "source_profile_pk": source_profile_pk,
                    "sha": f"{number}" * 64,
                },
            )
            artifacts = []
            for artifact_type in ("candidate_pack", "structured_profile"):
                artifacts.append(
                    connection.scalar(
                        text(
                            "INSERT INTO kb.artifact "
                            "(source_version_pk, artifact_type, storage_bucket, "
                            "storage_object_key, content_sha256) "
                            "VALUES (:source_version_pk, :artifact_type, 'kb', "
                            ":storage_object_key, :sha) RETURNING artifact_pk"
                        ),
                        {
                            "source_version_pk": source_version_pk,
                            "artifact_type": artifact_type,
                            "storage_object_key": f"rpc/{number}/{artifact_type}",
                            "sha": f"{number + 2}" * 64,
                        },
                    )
                )
            profile_ids.append(
                connection.scalar(
                    text(
                        "INSERT INTO kb.profile_version "
                        "(source_version_pk, schema_version, profile_sha256, "
                        "candidate_pack_artifact_pk, structured_artifact_pk) "
                        "VALUES (:source_version_pk, 'rpc-test', :profile_sha, "
                        ":candidate_pack, :structured) "
                        "RETURNING profile_version_pk"
                    ),
                    {
                        "source_version_pk": source_version_pk,
                        "profile_sha": f"{number + 4}" * 64,
                        "candidate_pack": artifacts[0],
                        "structured": artifacts[1],
                    },
                )
            )

        connection.execute(
            text(
                "INSERT INTO result.analysis_case ("
                "analysis_case_pk, source_analysis_run_id, user_id, case_status, "
                "analysis_completed_at, program_name, original_filename, report_status"
                ") VALUES ("
                ":case_id, :run_id, :owner_id, 'ready', :completed_at, "
                ":program_name, :filename, 'ready'"
                ")"
            ),
            {
                "case_id": case_id,
                "run_id": uuid4(),
                "owner_id": owner_id,
                "completed_at": now,
                "program_name": "RPC 테스트 사업",
                "filename": "request.hwpx",
            },
        )
        connection.execute(
            text(
                "INSERT INTO result.analysis_case ("
                "analysis_case_pk, source_analysis_run_id, user_id, case_status, "
                "report_status"
                ") VALUES (:case_id, :run_id, :owner_id, 'ready', 'generating')"
            ),
            {
                "case_id": other_case_id,
                "run_id": uuid4(),
                "owner_id": other_user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO result.analysis_case ("
                "analysis_case_pk, source_analysis_run_id, user_id, case_status, "
                "report_status"
                ") VALUES (:case_id, :run_id, :owner_id, 'ready', 'ready')"
            ),
            {
                "case_id": no_session_case_id,
                "run_id": uuid4(),
                "owner_id": owner_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO result.analysis_case ("
                "analysis_case_pk, source_analysis_run_id, user_id, case_status, "
                "report_status"
                ") VALUES (:case_id, :run_id, :owner_id, 'ready', 'ready')"
            ),
            {
                "case_id": expired_artifact_case_id,
                "run_id": uuid4(),
                "owner_id": owner_id,
            },
        )

        connection.execute(
            text(
                "INSERT INTO result.axis_result ("
                "axis_result_pk, analysis_case_pk, axis_type, axis_code, status, "
                "summary_text, result_data, ordinal"
                ") VALUES ("
                ":axis_id, :case_id, 'CPL', :code, 'confirmed', :summary, "
                "CAST(:data AS jsonb), :ordinal"
                ")"
            ),
            [
                {
                    "axis_id": uuid4(),
                    "case_id": case_id,
                    "code": "CPL-11",
                    "summary": "두 번째 CPL",
                    "data": json.dumps({"reason": "두 번째", "values": []}),
                    "ordinal": 2,
                },
                {
                    "axis_id": cpl_axis_id,
                    "case_id": case_id,
                    "code": "CPL-01",
                    "summary": "첫 번째 CPL",
                    "data": json.dumps({"reason": "첫 번째", "values": []}),
                    "ordinal": 1,
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO result.axis_result ("
                "axis_result_pk, analysis_case_pk, axis_type, axis_code, status, "
                "summary_text, result_data, ordinal"
                ") VALUES ("
                ":axis_id, :case_id, 'FIT', 'FIT-07', 'INSUFFICIENT', :summary, "
                "CAST(:data AS jsonb), 7"
                ")"
            ),
            {
                "axis_id": fit_axis_id,
                "case_id": case_id,
                "summary": "FIT 정보 부족",
                "data": json.dumps(
                    {
                        "reason_code": "SINGLE_SIDED_NO_CONFLICT",
                        "left": {"field_names": ["support_content"]},
                        "right": {"field_names": ["support_scale"]},
                    }
                ),
            },
        )

        purpose = {
            "axis_id": "SIM-1",
            "status": "similar",
            "summary": "목적 유사",
            "score": 91,
            "nested": {"percent": 88},
        }
        target = {
            "axis_id": "SIM-2",
            "status": "insufficient",
            "summary": "대상 정보 부족",
        }
        support = {
            "axis_id": "SIM-3",
            "status": "partial",
            "summary": "지원 일부 유사",
            "weighted_score": 0.5,
        }
        delivery = {
            "axis_id": "SIM-4",
            "status": "different",
            "summary": "수행체계 차이",
        }
        connection.execute(
            text(
                "INSERT INTO result.sim_candidate ("
                "sim_candidate_pk, analysis_case_pk, existing_profile_version_pk, "
                "rank_no, similarity_score, priority_score, status, title, "
                "result_summary, purpose_result, target_result, support_result, "
                "delivery_result"
                ") VALUES ("
                ":candidate_id, :case_id, :profile_id, :rank_no, 0.87, 0.4, "
                ":status, :title, :result_summary, CAST(:purpose AS jsonb), "
                "CAST(:target AS jsonb), CAST(:support AS jsonb), CAST(:delivery AS jsonb)"
                ")"
            ),
            [
                {
                    "candidate_id": second_candidate_id,
                    "case_id": case_id,
                    "profile_id": profile_ids[1],
                    "rank_no": 2,
                    "status": "GENERAL_REVIEW",
                    "title": "두 번째 공고",
                    "result_summary": "두 번째 요약",
                    "purpose": json.dumps({"status": "insufficient"}),
                    "target": json.dumps({"status": "insufficient"}),
                    "support": json.dumps({"status": "insufficient"}),
                    "delivery": json.dumps({"status": "insufficient"}),
                },
                {
                    "candidate_id": sim_candidate_id,
                    "case_id": case_id,
                    "profile_id": profile_ids[0],
                    "rank_no": 1,
                    "status": "GENERAL_REVIEW",
                    "title": "첫 번째 공고",
                    "result_summary": "주요 내용이 유사합니다.",
                    "purpose": json.dumps(purpose),
                    "target": json.dumps(target),
                    "support": json.dumps(support),
                    "delivery": json.dumps(delivery),
                },
            ],
        )

        evidence_rows = [
            (uuid4(), "CPL", cpl_axis_id, None, "REQUEST", None, "지원 기업 40개사", "CPL 발췌"),
            (uuid4(), "FIT", fit_axis_id, None, "REQUEST", "LEFT", "지원 내용", "FIT 좌측"),
            (uuid4(), "FIT", fit_axis_id, None, "REQUEST", "RIGHT", "지원 규모", "FIT 우측"),
            (uuid4(), "SIM", None, sim_candidate_id, "EXISTING", None, "기존 공고 근거", "기존 공고 발췌"),
            (uuid4(), "SIM", None, None, "REQUEST", None, "요청서 공통 근거", "요청서 발췌"),
        ]
        connection.execute(
            text(
                "INSERT INTO result.evidence_snapshot ("
                "evidence_snapshot_pk, analysis_case_pk, axis_type, axis_result_pk, "
                "sim_candidate_pk, side, raw_value, context_excerpt, comparison_side"
                ") VALUES ("
                ":evidence_id, :case_id, :axis_type, :axis_id, :candidate_id, "
                ":side, :raw_value, :excerpt, :comparison_side"
                ")"
            ),
            [
                {
                    "evidence_id": evidence_id,
                    "case_id": case_id,
                    "axis_type": axis_type,
                    "axis_id": axis_id,
                    "candidate_id": candidate_id,
                    "side": side,
                    "raw_value": raw_value,
                    "excerpt": excerpt,
                    "comparison_side": comparison_side,
                }
                for evidence_id, axis_type, axis_id, candidate_id, side, comparison_side, raw_value, excerpt in evidence_rows
            ],
        )
        connection.execute(
            text(
                "INSERT INTO result.report_artifact ("
                "analysis_case_pk, report_type, storage_bucket, storage_object_key, "
                "content_sha256, expires_at"
                ") VALUES (:case_id, 'pdf', 'reports', 'rpc/report.pdf', :sha, :expires_at)"
            ),
            {
                "case_id": case_id,
                "sha": "a" * 64,
                "expires_at": now + timedelta(days=1),
            },
        )
        connection.execute(
            text(
                "INSERT INTO result.analysis_session ("
                "analysis_case_pk, status, expires_at"
                ") VALUES (:case_id, 'active', :expires_at)"
            ),
            {"case_id": case_id, "expires_at": now + timedelta(minutes=30)},
        )
        connection.execute(
            text(
                "INSERT INTO result.report_artifact ("
                "analysis_case_pk, report_type, storage_bucket, storage_object_key, "
                "content_sha256, expires_at"
                ") VALUES (:case_id, 'pdf', 'reports', 'rpc/expired-report.pdf', "
                ":sha, :expires_at)"
            ),
            {
                "case_id": expired_artifact_case_id,
                "sha": "b" * 64,
                "expires_at": now - timedelta(minutes=1),
            },
        )

    return {
        "owner_id": owner_id,
        "other_user_id": other_user_id,
        "case_id": case_id,
        "other_case_id": other_case_id,
        "no_session_case_id": no_session_case_id,
        "expired_artifact_case_id": expired_artifact_case_id,
        "candidate_id": sim_candidate_id,
    }


def _rpc(
    engine: Engine,
    function_name: str,
    parameter_name: str,
    value: UUID,
    user_id: UUID | None,
) -> dict[str, Any] | None:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE authenticated"))
        connection.execute(
            text("SELECT pg_catalog.set_config('request.jwt.claim.sub', :value, false)"),
            {"value": str(user_id) if user_id is not None else ""},
        )
        result = connection.scalar(
            text(
                f"SELECT api.{function_name}(:{parameter_name})"
            ),
            {parameter_name: value},
        )
    return result


def test_result_rpc_returns_contract_order_and_evidence_links(
    engine: Engine, seeded: dict[str, Any]
) -> None:
    result = _rpc(
        engine,
        "rpc_get_analysis_result",
        "p_analysis_case_id",
        seeded["case_id"],
        seeded["owner_id"],
    )
    assert result is not None
    assert set(result) == {"case", "cpl", "fit", "sim", "report", "session", "evidences"}
    assert [item["code"] for item in result["cpl"]["items"]] == ["CPL-01", "CPL-11"]
    assert [item["code"] for item in result["fit"]["items"]] == ["FIT-07"]
    assert all("evidence_ids" in item["detail"] for item in result["cpl"]["items"])

    fit_detail = result["fit"]["items"][0]["detail"]
    assert fit_detail["left"]["evidence_ids"]
    assert fit_detail["right"]["evidence_ids"]
    assert set(fit_detail["left"]["evidence_ids"]).isdisjoint(
        fit_detail["right"]["evidence_ids"]
    )
    assert result["report"] == {"status": "ready", "can_download": True}
    assert result["session"]["can_chat"] is True
    assert result["session"]["expires_at"] is not None
    assert {"evidence_id", "raw_value", "excerpt"} == set(result["evidences"][0])
    assert [candidate["rank"] for candidate in result["sim"]["candidates"]] == [1, 2]
    assert set(result["sim"]["candidates"][0]) == {
        "sim_candidate_id",
        "rank",
        "title",
    }


def test_result_rpc_handles_missing_session_and_expired_report_artifact(
    engine: Engine, seeded: dict[str, Any]
) -> None:
    without_session = _rpc(
        engine,
        "rpc_get_analysis_result",
        "p_analysis_case_id",
        seeded["no_session_case_id"],
        seeded["owner_id"],
    )
    assert without_session is not None
    assert without_session["session"] == {"can_chat": False, "expires_at": None}
    assert without_session["report"] == {"status": "ready", "can_download": False}

    expired_report = _rpc(
        engine,
        "rpc_get_analysis_result",
        "p_analysis_case_id",
        seeded["expired_artifact_case_id"],
        seeded["owner_id"],
    )
    assert expired_report is not None
    assert expired_report["report"] == {"status": "ready", "can_download": False}
    assert expired_report["session"] == {"can_chat": False, "expires_at": None}


def test_sim_detail_hides_scores_and_includes_candidate_and_shared_request_evidence(
    engine: Engine, seeded: dict[str, Any]
) -> None:
    result = _rpc(
        engine,
        "rpc_get_sim_candidate_detail",
        "p_sim_candidate_id",
        seeded["candidate_id"],
        seeded["owner_id"],
    )
    assert result is not None
    assert set(result) == {
        "sim_candidate_id",
        "rank",
        "title",
        "result_summary",
        "comparable_axes",
        "axes",
        "observations",
        "evidences",
    }
    assert result["comparable_axes"] == ["purpose", "support", "delivery"]
    assert result["observations"] == []
    assert set(result["axes"]) == {"purpose", "target", "support", "delivery"}
    assert {item["raw_value"] for item in result["evidences"]} == {
        "기존 공고 근거",
        "요청서 공통 근거",
    }
    assert not _contains_forbidden_score_key(result)


def test_result_rpcs_are_owner_scoped_and_null_without_auth(
    engine: Engine, seeded: dict[str, Any]
) -> None:
    assert (
        _rpc(
            engine,
            "rpc_get_analysis_result",
            "p_analysis_case_id",
            seeded["case_id"],
            seeded["other_user_id"],
        )
        is None
    )
    assert (
        _rpc(
            engine,
            "rpc_get_sim_candidate_detail",
            "p_sim_candidate_id",
            seeded["candidate_id"],
            seeded["other_user_id"],
        )
        is None
    )
    assert (
        _rpc(
            engine,
            "rpc_get_analysis_result",
            "p_analysis_case_id",
            seeded["case_id"],
            None,
        )
        is None
    )
    assert (
        _rpc(
            engine,
            "rpc_get_analysis_result",
            "p_analysis_case_id",
            uuid4(),
            seeded["owner_id"],
        )
        is None
    )


def test_result_api_migration_is_idempotent_and_grants_only_api_access(
    engine: Engine,
) -> None:
    migration = (_SUPABASE_MIGRATIONS / "102_result_api.sql").read_text(
        encoding="utf-8"
    )
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        raw.execute(migration)
        raw.commit()
        grants = connection.execute(
            text(
                "SELECT "
                "has_schema_privilege('authenticated', 'api', 'USAGE'), "
                "has_schema_privilege('anon', 'api', 'USAGE'), "
                "has_function_privilege('authenticated', "
                "'api.rpc_get_analysis_result(uuid)', 'EXECUTE'), "
                "has_function_privilege('anon', "
                "'api.rpc_get_analysis_result(uuid)', 'EXECUTE')"
            )
        ).one()
    assert tuple(grants) == (True, False, True, False)
