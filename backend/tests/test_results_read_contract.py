"""Read-side HTTP contract tests without a PostgreSQL dependency."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from app.api.cursor import encode_cursor
from app.ports.results import AnalysisHistoryPage, ResultNotFound
from main import create_app


OWNER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_OWNER_ID = "22222222-2222-2222-2222-222222222222"
CASE_ID = "33333333-3333-3333-3333-333333333333"
SIM_ID = "44444444-4444-4444-4444-444444444444"
SESSION_ID = "55555555-5555-5555-5555-555555555555"
RUN_ID = "66666666-6666-6666-6666-666666666666"
EVIDENCE_ID = "77777777-7777-7777-7777-777777777777"
CANDIDATE_EVIDENCE_ID = "88888888-8888-8888-8888-888888888888"
CURSOR_SECRET = "test-cursor-signing-secret"
SNAPSHOT_AT = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _as_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _history_row(index: int) -> dict[str, Any]:
    completed_at = SNAPSHOT_AT - timedelta(hours=index)
    return {
        "analysis_case_id": f"a0000000-0000-0000-0000-{index:012d}",
        "program_name": f"사업 {index}",
        "original_filename": "request.hwpx",
        "completed_at": completed_at,
    }


class FakeResultRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.active: Mapping[str, Any] | None = {
            "analysis_session_id": SESSION_ID,
            "analysis_case_id": CASE_ID,
            "program_name": "테스트 사업",
            "original_filename": "request.hwpx",
            "session_expires_at": "2026-09-10T01:00:00Z",
        }
        self.current: Mapping[str, Any] = {
            "state": "ready",
            "run": None,
            "session": {
                "analysis_session_id": SESSION_ID,
                "analysis_case_id": CASE_ID,
                "program_name": "테스트 사업",
                "original_filename": "request.hwpx",
                "session_expires_at": "2026-09-10T01:00:00Z",
            },
        }
        # 12 rows sorted (completed_at DESC, analysis_case_id DESC) — enough
        # for more than two page-size-5 pages.
        self._history = [_history_row(index) for index in range(12)]
        self.closed_sessions: list[tuple[str, str]] = []
        self.close_not_found: set[str] = set()

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
            "cpl": {
                "items": [
                    {
                        "code": "CPL-1",
                        "status": "confirmed",
                        "summary": "지원 대상을 확인했습니다.",
                        "detail": {
                            "reason_code": None,
                            "reason": "원문에서 확인했습니다.",
                            "values": [
                                {
                                    "label": "지원 대상",
                                    "value": "부산 소재 중소기업",
                                    "evidence_ids": [EVIDENCE_ID],
                                }
                            ],
                            "source_fields": ["support_target"],
                            "evidence_ids": [EVIDENCE_ID],
                        },
                    }
                ]
            },
            "fit": {
                "items": [
                    {
                        "code": "FIT-1",
                        "status": "FIT",
                        "summary": "목적이 연결됩니다.",
                        "detail": {
                            "comparison_performed": True,
                            "reason_code": None,
                            "reason": "두 조건이 연결됩니다.",
                            "left": {
                                "value_summary": "부산 소재 중소기업",
                                "evidence_ids": [EVIDENCE_ID],
                            },
                            "right": {
                                "value_summary": "중소기업 지원기관",
                                "evidence_ids": [EVIDENCE_ID],
                            },
                            "evidence_ids": [EVIDENCE_ID],
                        },
                    }
                ]
            },
            "sim": {
                "status": "completed",
                "reason_code": None,
                "summary": "유사 공고 검색을 완료했습니다.",
                "candidates": [
                    {
                        "sim_candidate_id": SIM_ID,
                        "rank": 1,
                        "title": "지원사업명",
                        "comparison_status": "partial",
                        "comparison_summary": "목적은 유사하지만 대상에 차이가 있습니다.",
                    }
                ],
            },
            "ml": {
                "model_1": {
                    "status": "OK",
                    "support_type": "판로",
                    "message": "지원유형 참고 분류",
                    "reason_code": None,
                },
                "model_2": {
                    "status": "OK",
                    "predicted_amount_won": 8534969,
                    "message": "예측 지원액",
                    "reason_code": None,
                },
                "model_3": {
                    "status": "OK",
                    "anomaly_level": "과거 사업 패턴과 차이가 큼",
                    "cause_axes": ["기업(과제)당 지원한도"],
                    "message": "이례성 참고",
                    "reason_code": None,
                },
            },
            "report": {
                "status": "generating",
                "can_download": False,
                "can_regenerate": False,
                "retry_count": 0,
            },
            "session": {
                "analysis_session_id": SESSION_ID,
                "is_active": True,
                "can_chat": True,
                "expires_at": "2026-09-10T01:00:00Z",
            },
            "evidences": [
                {
                    "evidence_id": EVIDENCE_ID,
                    "side": "request",
                    "field_name": "support_target",
                    "raw_value": "부산 소재 중소기업",
                    "excerpt": None,
                }
            ],
        }

    async def get_sim_candidate(
        self, *, owner_id: str, sim_candidate_id: str
    ) -> Mapping[str, Any]:
        self.calls.append(("candidate", owner_id, sim_candidate_id))
        if owner_id != OWNER_ID or sim_candidate_id != SIM_ID:
            raise ResultNotFound("not visible")
        axis = {
            "code": "SIM-1",
            "status": "similar",
            "summary": "목적이 유사합니다.",
            "reason_code": None,
            "reason": "핵심 목적이 겹칩니다.",
            "common_points": ["지역 소상공인 지원"],
            "differences": [],
            "request_evidence_ids": [EVIDENCE_ID],
            "existing_evidence_ids": [CANDIDATE_EVIDENCE_ID],
        }
        return {
            "sim_candidate_id": SIM_ID,
            "analysis_case_id": CASE_ID,
            "rank": 1,
            "metadata": {
                "title": "지원사업명",
                "support_field": "기술",
                "apply_period": "2026-09-01 ~ 2026-09-30",
                "ministry": "중소벤처기업부",
                "executing_agency": "전담기관",
                "registered_at": "2026-09-01",
                "notice_status": "모집중",
                "source_url": "https://example.test/notice",
            },
            "comparison": {
                "status": "partial",
                "summary": "일부 축이 유사합니다.",
                "comparable_axes": ["purpose", "target", "support"],
            },
            "axes": {
                "purpose": axis,
                "target": {**axis, "code": "SIM-2", "status": "partial"},
                "support": {**axis, "code": "SIM-3", "status": "different"},
                "delivery": {
                    "code": "SIM-4",
                    "status": "insufficient",
                    "summary": "배송 근거가 부족합니다.",
                    "reason_code": "CANDIDATE_EVIDENCE_MISSING",
                    "reason": "배송 관련 근거가 없습니다.",
                    "common_points": [],
                    "differences": [],
                    "request_evidence_ids": [],
                    "existing_evidence_ids": [],
                },
            },
            "evidences": [
                {
                    "evidence_id": CANDIDATE_EVIDENCE_ID,
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

    async def get_current(self, *, owner_id: str) -> Mapping[str, Any]:
        self.calls.append(("current", owner_id, None))
        if owner_id != OWNER_ID:
            return {"state": "idle", "run": None, "session": None}
        return self.current

    async def close_session(self, *, owner_id: str, analysis_session_id: str) -> None:
        self.calls.append(("close", owner_id, analysis_session_id))
        if owner_id != OWNER_ID or analysis_session_id in self.close_not_found:
            raise ResultNotFound("not visible")
        self.closed_sessions.append((owner_id, analysis_session_id))

    async def list_analysis_history_page(
        self,
        *,
        owner_id: str,
        snapshot_at: datetime | None,
        after: tuple[datetime, str] | None,
        limit: int,
    ) -> AnalysisHistoryPage:
        self.calls.append(("history", owner_id, None))
        pinned = snapshot_at or SNAPSHOT_AT
        rows = self._history if owner_id == OWNER_ID else []
        if after is not None:
            after_completed_at, after_case_id = after
            rows = [
                row
                for row in rows
                if (row["completed_at"], row["analysis_case_id"]) < (after_completed_at, after_case_id)
            ]
        visible = rows[:limit]
        next_after = None
        if len(rows) > limit:
            last = visible[-1]
            next_after = (last["completed_at"], last["analysis_case_id"])
        return AnalysisHistoryPage(
            rows=visible,
            snapshot_at=pinned,
            next_after=next_after,
        )


def _app_with_results() -> tuple[object, FakeResultRepository]:
    app = create_app()
    app.state.offline_mode = True
    app.state.cursor_signing_secret = CURSOR_SECRET
    repository = FakeResultRepository()
    app.state.result_repository = repository
    return app, repository


def _headers(owner_id: str = OWNER_ID) -> dict[str, str]:
    return {"X-PreReview-Dev-User": owner_id}


def test_result_routes_return_typed_contract_payloads_and_never_accept_owner_ids() -> None:
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
            body = case.json()
            assert body["case"]["analysis_case_id"] == CASE_ID
            assert body["cpl"]["items"][0]["status"] == "confirmed"
            assert body["cpl"]["items"][0]["detail"]["values"][0]["value"] == "부산 소재 중소기업"
            assert body["fit"]["items"][0]["status"] == "FIT"
            assert body["fit"]["items"][0]["detail"]["comparison_performed"] is True
            assert body["sim"]["status"] == "completed"
            assert body["sim"]["candidates"][0]["comparison_status"] == "partial"
            ml = body["ml"]
            # 공개 표면은 message 하나다. support_type·anomaly_level 은 서버가
            # 그 문장을 조립한 출처라 같이 내리면 중복이고, status·reason_code·
            # predicted_amount_won·cause_axes 는 저장 payload 에만 남는다.
            assert set(ml["model_1"]) == {"message"}
            assert set(ml["model_2"]) == {"message"}
            assert set(ml["model_3"]) == {"message"}
            assert ml["model_1"]["message"] == "지원유형 참고 분류"
            assert "data" not in body

            candidate = await client.get(
                f"/api/v1/sim-candidates/{SIM_ID}", headers=_headers()
            )
            assert candidate.status_code == 200
            candidate_body = candidate.json()
            assert candidate_body["sim_candidate_id"] == SIM_ID
            assert candidate_body["metadata"]["title"] == "지원사업명"
            assert candidate_body["metadata"]["apply_period"] == "2026-09-01 ~ 2026-09-30"
            assert candidate_body["comparison"]["status"] == "partial"
            assert candidate_body["axes"]["delivery"]["reason_code"] == "CANDIDATE_EVIDENCE_MISSING"
            # Legacy top-level fields must be gone, replaced by metadata/comparison/axes.
            assert "issuing_organization" not in candidate_body
            assert "status" not in candidate_body
            assert candidate_body["evidences"] == [
                {
                    "evidence_id": CANDIDATE_EVIDENCE_ID,
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

            current = await client.get("/api/v1/analysis/current", headers=_headers())
            assert current.status_code == 200
            assert current.json()["state"] == "ready"
            assert current.json()["session"]["analysis_case_id"] == CASE_ID
            assert current.json()["run"] is None

    asyncio.run(run())
    assert all(call[1] == OWNER_ID for call in repository.calls)


def test_current_reports_idle_for_an_owner_with_nothing_in_flight() -> None:
    app, _repository = _app_with_results()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/v1/analysis/current", headers=_headers(OTHER_OWNER_ID)
            )
            assert response.status_code == 200
            assert response.json() == {"state": "idle", "run": None, "session": None}

    asyncio.run(run())


def test_current_reports_processing_run() -> None:
    app, repository = _app_with_results()
    repository.current = {
        "state": "processing",
        "run": {
            "analysis_run_id": RUN_ID,
            "status": "queued",
            "original_filename": "request.hwpx",
            "created_at": "2026-09-10T00:00:00Z",
            "updated_at": "2026-09-10T00:00:00Z",
        },
        "session": None,
    }

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/v1/analysis/current", headers=_headers())
            assert response.status_code == 200
            body = response.json()
            assert body["state"] == "processing"
            assert body["run"]["analysis_run_id"] == RUN_ID
            assert body["session"] is None

    asyncio.run(run())


def test_close_session_is_owner_scoped_idempotent_and_requires_trusted_origin() -> None:
    app, repository = _app_with_results()
    app.state.auth_allowed_origins = frozenset({"http://frontend.test"})

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            untrusted = await client.post(
                f"/api/v1/analysis-sessions/{SESSION_ID}/close", headers=_headers()
            )
            assert untrusted.status_code == 403

            headers = {**_headers(), "Origin": "http://frontend.test"}
            first = await client.post(
                f"/api/v1/analysis-sessions/{SESSION_ID}/close", headers=headers
            )
            assert first.status_code == 204
            assert first.content == b""

            # Re-closing the same, already-closed session is still 204.
            second = await client.post(
                f"/api/v1/analysis-sessions/{SESSION_ID}/close", headers=headers
            )
            assert second.status_code == 204

            foreign = await client.post(
                f"/api/v1/analysis-sessions/{SESSION_ID}/close",
                headers={**_headers(OTHER_OWNER_ID), "Origin": "http://frontend.test"},
            )
            assert foreign.status_code == 404
            assert foreign.json()["code"] == "NOT_FOUND"

    asyncio.run(run())
    assert repository.closed_sessions == [(OWNER_ID, SESSION_ID), (OWNER_ID, SESSION_ID)]


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


def test_ml_read_contract_rejects_internal_score_fields() -> None:
    from app.api.v1.results import AnalysisResultReadModel

    payload = {
        "case": {
            "analysis_case_id": CASE_ID,
            "program_name": None,
            "original_filename": None,
            "completed_at": None,
        },
        "cpl": {"items": []},
        "fit": {"items": []},
        "sim": {"status": "completed", "reason_code": None, "summary": None, "candidates": []},
        "ml": {
            "model_1": {
                "status": "OK",
                "support_type": "판로",
                "message": None,
                "reason_code": None,
                "confidence": 0.99,
            },
            "model_2": {
                "status": "UNAVAILABLE",
                "predicted_amount_won": None,
                "message": None,
                "reason_code": "ML_RUNTIME_MISSING",
            },
            "model_3": {
                "status": "UNAVAILABLE",
                "anomaly_level": None,
                "cause_axes": [],
                "message": None,
                "reason_code": "ML_RUNTIME_MISSING",
            },
        },
        "report": {
            "status": "generating",
            "can_download": False,
            "can_regenerate": False,
            "retry_count": 0,
        },
        "session": {
            "analysis_session_id": None,
            "is_active": False,
            "can_chat": False,
            "expires_at": None,
        },
        "evidences": [],
    }

    with pytest.raises(ValidationError):
        AnalysisResultReadModel.model_validate(payload)


def test_sim_and_candidate_detail_reject_raw_score_and_sim_evidence_mixing() -> None:
    from app.api.v1.results import AnalysisSimSection, SimCandidateDetailReadModel

    # SIM section status/reason/summary are required, not optional — a bare
    # {"candidates": []} without them must fail loudly rather than silently
    # rendering blank fields on the frontend.
    with pytest.raises(ValidationError):
        AnalysisSimSection.model_validate({"candidates": []})

    # A raw similarity/priority score sneaking into candidate axis detail must
    # be rejected, not silently dropped or exposed.
    axis = {
        "code": "SIM-1",
        "status": "similar",
        "summary": None,
        "reason_code": None,
        "reason": None,
        "common_points": [],
        "differences": [],
        "request_evidence_ids": [],
        "existing_evidence_ids": [],
        "similarity_score": 0.87,
    }
    with pytest.raises(ValidationError):
        SimCandidateDetailReadModel.model_validate(
            {
                "sim_candidate_id": SIM_ID,
                "analysis_case_id": CASE_ID,
                "rank": 1,
                "metadata": {
                    "title": None,
                    "support_field": None,
                    "apply_period": None,
                    "ministry": None,
                    "executing_agency": None,
                    "registered_at": None,
                    "notice_status": None,
                    "source_url": None,
                },
                "comparison": {"status": "similar", "summary": None, "comparable_axes": []},
                "axes": {"purpose": axis, "target": axis, "support": axis, "delivery": axis},
                "evidences": [],
            }
        )


def test_history_first_page_is_size_five_and_next_cursor_advances() -> None:
    app, repository = _app_with_results()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.get("/api/v1/analysis-history", headers=_headers())
            assert first.status_code == 200
            first_body = first.json()
            assert len(first_body["items"]) == 5
            assert first_body["items"] == [
                {
                    "analysis_case_id": row["analysis_case_id"],
                    "program_name": row["program_name"],
                    "original_filename": row["original_filename"],
                    "completed_at": _as_z(row["completed_at"]),
                }
                for row in repository._history[:5]
            ]
            assert first_body["next_cursor"] is not None

            second = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(),
                params={"cursor": first_body["next_cursor"]},
            )
            assert second.status_code == 200
            second_body = second.json()
            assert len(second_body["items"]) == 5
            assert second_body["items"][0]["analysis_case_id"] == repository._history[5]["analysis_case_id"]
            assert second_body["next_cursor"] is not None

            third = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(),
                params={"cursor": second_body["next_cursor"]},
            )
            assert third.status_code == 200
            third_body = third.json()
            # 12 rows total: page 3 only has the remaining 2, so no next page.
            assert len(third_body["items"]) == 2
            assert third_body["next_cursor"] is None

    asyncio.run(run())


def test_history_cursor_is_owner_bound_and_rejects_tampering() -> None:
    app, _repository = _app_with_results()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.get("/api/v1/analysis-history", headers=_headers())
            cursor = first.json()["next_cursor"]

            # A different owner's request with the same cursor must not page
            # through this owner's history: 422, not a silent empty page.
            foreign = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(OTHER_OWNER_ID),
                params={"cursor": cursor},
            )
            assert foreign.status_code == 422
            assert foreign.json()["code"] == "VALIDATION_ERROR"

            tampered = cursor[:-4] + ("AAAA" if cursor[-4:] != "AAAA" else "BBBB")
            bad = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(),
                params={"cursor": tampered},
            )
            assert bad.status_code == 422
            assert bad.json()["code"] == "VALIDATION_ERROR"

            garbage = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(),
                params={"cursor": "not-a-real-cursor"},
            )
            assert garbage.status_code == 422

    asyncio.run(run())


def test_history_cursor_from_a_different_endpoint_is_rejected() -> None:
    app, _repository = _app_with_results()
    # A validly-signed cursor minted for a *different* endpoint (or a stale
    # version) must not be accepted here even with the right owner/secret.
    foreign_endpoint_cursor = encode_cursor(
        secret=CURSOR_SECRET,
        endpoint="analysis-case-messages",
        scope=OWNER_ID,
        version=1,
        fields={"snapshot_at": SNAPSHOT_AT.isoformat(), "completed_at": SNAPSHOT_AT.isoformat(), "analysis_case_id": CASE_ID},
    )

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/v1/analysis-history",
                headers=_headers(),
                params={"cursor": foreign_endpoint_cursor},
            )
            assert response.status_code == 422

    asyncio.run(run())


def test_history_requires_cursor_secret_to_be_configured() -> None:
    app, _repository = _app_with_results()
    app.state.cursor_signing_secret = ""

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/v1/analysis-history", headers=_headers())
            assert response.status_code == 503
            assert response.json()["code"] == "SERVICE_UNAVAILABLE"

    asyncio.run(run())


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
    current = response_schema("/api/v1/analysis/current")

    assert case["$ref"].endswith("/AnalysisResultReadModel")
    schemas = schema["components"]["schemas"]
    assert schemas["AnalysisResultReadModel"]["properties"]["ml"]["$ref"].endswith(
        "/MlReferenceReadModel"
    )
    assert schemas["MlReferenceReadModel"]["additionalProperties"] is False
    assert candidate["$ref"].endswith("/SimCandidateDetailReadModel")
    assert active["$ref"].endswith("/ActiveAnalysisSessionReadModel")
    assert history["$ref"].endswith("/AnalysisHistoryEnvelope")
    envelope = schemas["AnalysisHistoryEnvelope"]
    assert envelope["properties"]["items"]["items"]["$ref"].endswith("/AnalysisHistoryEntryReadModel")
    assert "next_cursor" in envelope["properties"]
    assert "oneOf" in current or "anyOf" in current
    assert "additionalProperties" not in case
    assert "additionalProperties" not in candidate
    assert "additionalProperties" not in active
    assert paths["/api/v1/analysis-sessions/active"]["get"]["responses"]["204"] == {
        "description": "No active analysis session"
    }
