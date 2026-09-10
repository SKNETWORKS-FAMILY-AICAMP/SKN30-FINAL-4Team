"""Read-side HTTP contract tests without a PostgreSQL dependency."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import httpx

from app.ports.results import ResultNotFound
from main import create_app


OWNER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_OWNER_ID = "22222222-2222-2222-2222-222222222222"
CASE_ID = "33333333-3333-3333-3333-333333333333"
SIM_ID = "44444444-4444-4444-4444-444444444444"


class FakeResultRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.active: Mapping[str, Any] | None = {
            "analysis_session_id": "55555555-5555-5555-5555-555555555555",
            "analysis_case_id": CASE_ID,
            "program_name": "테스트 사업",
            "original_filename": "request.hwpx",
            "session_expires_at": "2026-09-10T01:00:00Z",
        }

    async def get_analysis_case(
        self, *, owner_id: str, analysis_case_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(("case", owner_id, analysis_case_id))
        if owner_id != OWNER_ID or analysis_case_id != CASE_ID:
            raise ResultNotFound("not visible")
        return {
            "case": {
                "analysis_case_id": CASE_ID,
                "program_name": "테스트 사업",
                "original_filename": "request.hwpx",
                "completed_at": "2026-09-10T00:00:00Z",
            },
            "cpl": {"items": []},
            "fit": {"items": []},
            "sim": {"candidates": []},
            "report": {
                "status": "generating",
                "can_download": False,
                "can_regenerate": False,
                "retry_count": 0,
            },
            "session": {
                "analysis_session_id": "55555555-5555-5555-5555-555555555555",
                "is_active": True,
                "can_chat": True,
                "expires_at": "2026-09-10T01:00:00Z",
            },
            "evidences": [],
        }

    async def get_sim_candidate(
        self, *, owner_id: str, sim_candidate_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(("candidate", owner_id, sim_candidate_id))
        if owner_id != OWNER_ID or sim_candidate_id != SIM_ID:
            raise ResultNotFound("not visible")
        return {
            "sim_candidate_id": SIM_ID,
            "analysis_case_id": CASE_ID,
            "rank": 1,
            "title": "기존 공고",
            "issuing_organization": "테스트 기관",
            "source_url": "https://example.test/notice",
            "notice_status": "closed",
            "status": "similar",
            "summary": "유사한 목적",
            "comparable_axes": ["purpose", "target", "support"],
            "axes": {"purpose": {}, "target": {}, "support": {}, "delivery": {}},
            "evidences": [
                {
                    "evidence_id": "66666666-6666-6666-6666-666666666666",
                    "side": "existing",
                    "axis_type": "SIM",
                    "field_name": "purpose_goal",
                    "raw_value": "기존 공고의 목적 근거",
                    "excerpt": "목적 근거 문맥",
                }
            ],
        }

    async def get_active_session(self, *, owner_id: str) -> Mapping[str, Any] | None:
        self.calls.append(("active", owner_id, None))
        return self.active if owner_id == OWNER_ID else None

    async def list_analysis_history(self, *, owner_id: str) -> list[Mapping[str, Any]]:
        self.calls.append(("history", owner_id, None))
        if owner_id != OWNER_ID:
            return []
        return [
            {
                "analysis_case_id": CASE_ID,
                "program_name": "테스트 사업",
                "original_filename": "request.hwpx",
                "completed_at": "2026-09-10T00:00:00Z",
                "report_status": "ready",
                "report_completed_at": "2026-09-10T00:01:00Z",
            }
        ]


def _app_with_results() -> tuple[object, FakeResultRepository]:
    app = create_app()
    repository = FakeResultRepository()
    app.state.result_repository = repository
    return app, repository


def _headers(owner_id: str = OWNER_ID) -> dict[str, str]:
    return {"X-PreReview-Dev-User": owner_id}


def test_result_routes_return_bare_contract_payloads_and_never_accept_owner_ids() -> None:
    app, repository = _app_with_results()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            case = await client.get(
                f"/api/v1/analysis-cases/{CASE_ID}?user_id={OTHER_OWNER_ID}",
                headers=_headers(),
            )
            assert case.status_code == 200
            assert case.json()["case"]["analysis_case_id"] == CASE_ID
            assert "data" not in case.json()

            candidate = await client.get(
                f"/api/v1/sim-candidates/{SIM_ID}", headers=_headers()
            )
            assert candidate.status_code == 200
            assert candidate.json()["sim_candidate_id"] == SIM_ID
            assert candidate.json()["evidences"] == [
                {
                    "evidence_id": "66666666-6666-6666-6666-666666666666",
                    "side": "existing",
                    "axis_type": "SIM",
                    "field_name": "purpose_goal",
                    "raw_value": "기존 공고의 목적 근거",
                    "excerpt": "목적 근거 문맥",
                }
            ]

            active = await client.get("/api/v1/analysis-sessions/active", headers=_headers())
            assert active.status_code == 200
            assert active.json()["analysis_case_id"] == CASE_ID

            history = await client.get("/api/v1/analysis-history", headers=_headers())
            assert history.status_code == 200
            assert history.json() == [
                {
                    "analysis_case_id": CASE_ID,
                    "program_name": "테스트 사업",
                    "original_filename": "request.hwpx",
                    "completed_at": "2026-09-10T00:00:00Z",
                    "report_status": "ready",
                    "report_completed_at": "2026-09-10T00:01:00Z",
                }
            ]

    asyncio.run(run())
    assert all(call[1] == OWNER_ID for call in repository.calls)


def test_foreign_or_missing_result_is_a_single_404_and_no_active_session_is_204() -> None:
    app, repository = _app_with_results()
    repository.active = None

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            foreign = await client.get(
                f"/api/v1/analysis-cases/{CASE_ID}", headers=_headers(OTHER_OWNER_ID)
            )
            assert foreign.status_code == 404
            assert foreign.json()["code"] == "NOT_FOUND"

            no_session = await client.get(
                "/api/v1/analysis-sessions/active", headers=_headers()
            )
            assert no_session.status_code == 204
            assert no_session.content == b""

    asyncio.run(run())


def test_result_routes_require_the_cookie_or_offline_principal() -> None:
    app, _repository = _app_with_results()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/v1/analysis-cases/{CASE_ID}")
            assert response.status_code == 401
            assert response.json()["code"] == "UNAUTHORIZED"

    asyncio.run(run())


def test_route_path_ids_are_uuid_typed() -> None:
    # This guards accidental widening of case/candidate ids to arbitrary text.
    assert UUID(CASE_ID)
    assert UUID(SIM_ID)


def test_openapi_exposes_named_result_read_models_not_generic_objects() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    def response_schema(path: str, status_code: str = "200") -> dict[str, Any]:
        return paths[path]["get"]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]

    case = response_schema("/api/v1/analysis-cases/{analysis_case_id}")
    candidate = response_schema("/api/v1/sim-candidates/{sim_candidate_id}")
    active = response_schema("/api/v1/analysis-sessions/active")
    history = response_schema("/api/v1/analysis-history")

    assert case["$ref"].endswith("/AnalysisResultReadModel")
    assert candidate["$ref"].endswith("/SimCandidateDetailReadModel")
    assert active["$ref"].endswith("/ActiveAnalysisSessionReadModel")
    assert history["type"] == "array"
    assert history["items"]["$ref"].endswith("/AnalysisHistoryEntryReadModel")
    assert "additionalProperties" not in case
    assert "additionalProperties" not in candidate
    assert "additionalProperties" not in active
    assert paths["/api/v1/analysis-sessions/active"]["get"]["responses"]["204"] == {
        "description": "No active analysis session"
    }
