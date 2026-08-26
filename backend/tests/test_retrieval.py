import asyncio
import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from app.infrastructure.bizinfo_public_data_client import BizinfoPublicDataClient
from app.infrastructure.openai_embedding_client import OpenAIEmbeddingClient
from app.ports.embedding_client import EmbeddingBatch
from app.ports.public_data_client import PublicAnnouncement
from app.schemas.cpl import (
    CPL_FIELDS,
    CplAxisCode,
    CplFieldCode,
    CplItem,
    CplOccurrence,
    CplResult,
    CplStatus,
)
from app.schemas.sim import SimSemanticResponse
from app.services.retrieval.announcement_sync import (
    announcement_embedding_text,
    normalize_announcement,
    sync_announcements,
)
from app.services.retrieval.corpus_embedding import embed_current_announcements
from app.services.retrieval.retrieval import (
    RetrievalNotReadyError,
    _display_score,
    compose_inspection_embedding_text,
    retrieve_top_five,
)
from app.services.sim.sim_engine import (
    analyze_sim_candidates,
    load_sim_prompt,
    load_sim_scoring,
)


def announcement(
    pblanc_id: str = "PBLN_TEST",
    *,
    title: str = "디지털 전환 지원",
    period: str = "20990101 ~ 20991231",
    summary: str = "지역기업 생산성 향상 ☞ 중소기업 ☞ 설비·컨설팅 지원 ※ 공고문 참조",
) -> PublicAnnouncement:
    return PublicAnnouncement(
        pblanc_id=pblanc_id,
        title=title,
        url=f"https://example.com/{pblanc_id}",
        jurisdiction_name="중소벤처기업부",
        executing_name="전담기관",
        summary_html=f"<p>{summary}</p>",
        category_name="기술",
        source_created_at="2026-08-25 09:00:00",
        source_updated_at=None,
        application_period=period,
        target_name="중소기업",
        view_count="1,234",
        hashtags="기술,서울",
        request_method_papers="온라인 신청",
        reference_contact="담당부서",
        receipt_homepage_url="https://apply.example.com",
        attachment_urls="https://example.com/a.hwp@https://example.com/b.pdf",
        attachment_names="a.hwp@b.pdf",
        print_attachment_url="https://example.com/main.pdf",
        print_attachment_name="main.pdf",
        raw_payload={"pblancId": pblanc_id, "pblancNm": title},
    )


def test_bizinfo_adapter_uses_official_json_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/bizinfoApi.do")
        assert request.url.params["crtfcKey"] == "secret"
        assert request.url.params["dataType"] == "json"
        assert request.url.params["searchCnt"] == "0"
        return httpx.Response(
            200,
            json={
                "jsonArray": {
                    "item": {
                        "pblancId": "PBLN_1",
                        "pblancNm": "지원 공고",
                        "pblancUrl": "https://example.com/1",
                        "jrsdInsttNm": "기관",
                        "bsnsSumryCn": "사업 개요",
                        "creatPnttm": "2026-08-25 09:00:00",
                        "reqstBeginEndDe": "20260801 ~ 20260831",
                    }
                }
            },
        )

    client = BizinfoPublicDataClient(
        api_key="secret",
        base_url="https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    items = asyncio.run(client.list_current_announcements())
    assert len(items) == 1
    assert items[0].pblanc_id == "PBLN_1"
    assert items[0].title == "지원 공고"


def test_openai_embedding_adapter_preserves_input_order_and_logs_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer secret"
        body: dict[str, Any] = __import__("json").loads(request.content)
        assert body == {
            "model": "text-embedding-3-small",
            "input": ["first", "second"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
            },
        )

    client = OpenAIEmbeddingClient(
        api_key="secret",
        base_url="https://api.openai.com/v1",
        model_name="text-embedding-3-small",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    caplog.set_level("INFO")
    batch = asyncio.run(client.embed(["first", "second"]))
    assert batch.model_name == "text-embedding-3-small"
    assert batch.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert "prompt_tokens=3" in caplog.text
    assert "secret" not in caplog.text


def test_bizinfo_adapter_retries_transient_error_once() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"jsonArray": {"item": []}})

    client = BizinfoPublicDataClient(
        api_key="secret",
        base_url="https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(client.list_current_announcements()) == []
    assert calls == 2


def test_normalization_preserves_source_axes_and_cleans_only_embedding_input() -> None:
    normalized = normalize_announcement(announcement(), date(2026, 8, 25))
    assert normalized.purpose == "지역기업 생산성 향상"
    assert normalized.target == "중소기업"
    assert normalized.content.endswith("※ 공고문 참조")
    assert normalized.detail_ref_fields == ["content"]
    assert normalized.period_type == "FIXED"
    assert normalized.search_status == "OPEN"
    assert normalized.view_count == 1234
    assert len(normalized.attachments) == 3
    embedding_input = announcement_embedding_text(normalized)
    assert "공고문 참조" not in embedding_input
    assert "지원내용: 설비·컨설팅 지원" in embedding_input


class FakePublicDataClient:
    def __init__(self, items: list[PublicAnnouncement]) -> None:
        self.items = items
        self.calls = 0

    async def list_current_announcements(self) -> list[PublicAnnouncement]:
        self.calls += 1
        return self.items


class FakeEmbeddingClient:
    def __init__(self, vectors_by_marker: dict[str, list[float]]) -> None:
        self.vectors_by_marker = vectors_by_marker
        self.embed_calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.embed_calls += 1
        vectors: list[list[float]] = []
        for value in texts:
            matched = next(
                (vector for marker, vector in self.vectors_by_marker.items() if marker in value),
                None,
            )
            if matched is None:
                raise AssertionError(f"No fake vector for: {value}")
            vectors.append(matched)
        return EmbeddingBatch(
            model_name="text-embedding-3-small",
            vectors=vectors,
        )


class FakeSimLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(self, **kwargs) -> SimSemanticResponse:
        self.calls += 1
        call_no = self.calls
        payload = json.loads(kwargs["messages"][1].content)
        axes = {}
        for axis, values in payload["axes"].items():
            request_refs = [item["evidence_ref"] for item in values["request_evidence"]]
            candidate_refs = [
                item["evidence_ref"] for item in values["candidate_evidence"]
            ]
            assessable = bool(request_refs and candidate_refs)
            axes[axis] = {
                "status": "SIMILAR" if assessable else "INSUFFICIENT",
                "summary": (
                    "양쪽 근거에서 공통점을 확인했습니다."
                    if assessable
                    else "비교 근거가 부족합니다."
                ),
                "common_points": ["명시된 공통점"] if assessable else [],
                "differences": [],
                "request_evidence_refs": (
                    ["request:invalid"]
                    if call_no == 1 and axis == "purpose"
                    else request_refs[:1]
                ),
                "candidate_evidence_refs": candidate_refs[:1],
                "reason_code": None if assessable else "COMPARISON_EVIDENCE_MISSING",
            }
        return SimSemanticResponse.model_validate(
            {"axes": axes, "comparison_summary": "후보별 4축 비교 결과입니다."}
        )


def sim_cpl_result() -> CplResult:
    axes = {
        CplFieldCode.PURPOSE_GOAL: (
            CplAxisCode.PURPOSE_SPECIFIC_OBJECTIVE,
            "지역기업 생산성 향상",
        ),
        CplFieldCode.TARGET_AND_CONDITIONS: (
            CplAxisCode.TARGET_GROUP,
            "중소기업",
        ),
        CplFieldCode.SUPPORT_CONTENT_AND_SCALE: (
            CplAxisCode.SUPPORT_ACTIVITY,
            "설비·컨설팅 지원",
        ),
        CplFieldCode.DELIVERY_SYSTEM: (
            CplAxisCode.DELIVERY_ORG_NAME,
            "전담기관",
        ),
    }
    return CplResult(
        ruleset_version="cpl-alpha-v0.2",
        prompt_version="cpl-semantic-v0.2",
        items=[
            CplItem(
                field_code=field_code,
                status=CplStatus.PRESENT if field_code in axes else CplStatus.MISSING,
                occurrences=(
                    [
                        CplOccurrence(
                            raw_text=axes[field_code][1],
                            normalized_value={"text": axes[field_code][1]},
                            axis_code=axes[field_code][0],
                            page_no=1,
                            section_path=[field_code.value],
                            source_locator={"paragraph_index": 0},
                            block_id=f"block:{field_code.value}",
                            extraction_method="LLM",
                        )
                    ]
                    if field_code in axes
                    else []
                ),
            )
            for field_code in CPL_FIELDS
        ],
    )


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        (-0.1, 0),
        (0.0049, 0),
        (0.005, 1),
        (0.8949, 89),
        (0.895, 90),
        (1.1, 100),
    ],
)
def test_similarity_display_clamps_and_rounds_half_up(
    similarity: float,
    expected: int,
) -> None:
    assert _display_score(similarity) == expected


@pytest.fixture(scope="module")
def engine() -> Engine:
    database_url = os.getenv("TEST_DATABASE_URL")
    if database_url is None:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration")
    value = create_engine(database_url)
    try:
        yield value
    finally:
        value.dispose()


def test_sync_versioning_embedding_profile_and_exact_top_five(engine: Engine) -> None:
    marker = uuid.uuid4().hex[:10]
    profile_name = f"retrieval-test-{marker}"
    ids = [f"PBLN_{marker}_{index}" for index in range(7)]
    titles = [f"후보-{marker}-{index}" for index in range(7)]
    items = [
        announcement(
            ids[index],
            title=titles[index],
            period=("예산 소진시까지" if index == 1 else "20990101 ~ 20991231"),
        )
        for index in range(6)
    ] + [
        announcement(ids[6], title=titles[6], period="20200101 ~ 20200131")
    ]
    sync_date = date(2030, 1, 1) + timedelta(days=int(marker[:4], 16) % 3000)
    public_client = FakePublicDataClient(items)
    sync_result = asyncio.run(
        sync_announcements(engine, public_client, sync_date_kst=sync_date)
    )
    assert sync_result.rows_inserted == 7

    reused = asyncio.run(
        sync_announcements(engine, public_client, sync_date_kst=sync_date)
    )
    assert reused.reused_success is True
    assert public_client.calls == 1

    vectors = {
        titles[0]: [1.0, 0.0, 0.0],
        titles[1]: [0.9, 0.1, 0.0],
        titles[2]: [0.8, 0.2, 0.0],
        titles[3]: [0.7, 0.3, 0.0],
        titles[4]: [0.6, 0.4, 0.0],
        titles[5]: [0.5, 0.5, 0.0],
        titles[6]: [1.0, 0.0, 0.0],
        "요청 목적": [1.0, 0.0, 0.0],
    }
    embedding_client = FakeEmbeddingClient(vectors)
    embedded = asyncio.run(
        embed_current_announcements(
            engine,
            embedding_client,
            requested_model_name="text-embedding-3-small",
            profile_name=profile_name,
            profile_version=1,
            preprocessing_version="detail-ref-v1",
            batch_size=3,
        )
    )
    assert embedded.embedded_count == 7

    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                """
                INSERT INTO sims.app_user (login_id, email, password_hash)
                VALUES (:login_id, :email, 'test-hash') RETURNING id
                """
            ),
            {"login_id": f"retrieval-{marker}", "email": f"retrieval-{marker}@example.com"},
        )
        case_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_case (owner_user_id, status)
                VALUES (:user_id, 'CHECKING') RETURNING id
                """
            ),
            {"user_id": user_id},
        )
        wrong_top_k_case_id = connection.scalar(
            text(
                """
                INSERT INTO sims.inspection_case (owner_user_id, status, top_k_used)
                VALUES (:user_id, 'CHECKING', 6) RETURNING id
                """
            ),
            {"user_id": user_id},
        )
    assert case_id is not None
    input_text = compose_inspection_embedding_text(
        purpose="요청 목적",
        target="중소기업",
        content="설비 지원",
    )
    calls_before_retrieval = embedding_client.embed_calls
    with pytest.raises(RetrievalNotReadyError, match="Top-5"):
        asyncio.run(
            retrieve_top_five(
                engine,
                embedding_client,
                case_id=wrong_top_k_case_id,
                input_text=input_text,
            )
        )
    assert embedding_client.embed_calls == calls_before_retrieval
    result = asyncio.run(
        retrieve_top_five(
            engine,
            embedding_client,
            case_id=case_id,
            input_text=input_text,
        )
    )
    assert len(result.candidates) == 5
    assert result.top_k_used == 5
    assert [candidate.rank for candidate in result.candidates] == [1, 2, 3, 4, 5]
    assert result.candidates[0].title == titles[0]
    assert result.candidates[0].semantic_similarity_display == 100
    unknown = next(
        candidate for candidate in result.candidates if candidate.title == titles[1]
    )
    assert unknown.search_status == "UNKNOWN"
    assert titles[6] not in {candidate.title for candidate in result.candidates}

    sim_llm = FakeSimLlm()
    sim_results = asyncio.run(
        analyze_sim_candidates(
            engine,
            result,
            sim_cpl_result(),
            sim_llm,
            scoring=load_sim_scoring(Path("config/sim_scoring.json")),
            prompt=load_sim_prompt(Path("config/prompts/sim-v0.1.txt")),
            ruleset_version="sim-v0.1",
            prompt_version="sim-v0.1",
            model_profile="gpt-4o-mini",
        )
    )
    assert len(sim_results) == 5
    assert sim_llm.calls == 5
    assert [item.rank for item in sim_results] == [1, 2, 3, 4, 5]
    assert sim_results[0].review_grade == "ON_HOLD"
    assert all(item.review_grade == "FOCUS_REVIEW" for item in sim_results[1:])
    assert (
        sim_results[0].announcement_version_id
        == result.candidates[0].announcement_version_id
    )
    with engine.connect() as connection:
        persisted_sim = connection.execute(
            text(
                """
                SELECT rc.rank_no, rc.comparison_result,
                       count(ce.id) AS evidence_count
                FROM sims.retrieval_candidate rc
                LEFT JOIN sims.candidate_evidence ce
                  ON ce.retrieval_candidate_id = rc.id
                WHERE rc.retrieval_run_id = :retrieval_run_id
                GROUP BY rc.id, rc.rank_no
                ORDER BY rc.rank_no
                """
            ),
            {"retrieval_run_id": result.retrieval_run_id},
        ).mappings().all()
    assert len(persisted_sim) == 5
    assert (
        persisted_sim[0]["comparison_result"]["axes"]["purpose"]["axis_id"]
        == "SIM-1"
    )
    assert all(row["evidence_count"] > 0 for row in persisted_sim)
    assert persisted_sim[0]["comparison_result"]["warnings"][-1].endswith(
        "LLM_INVALID_RESPONSE"
    )
    assert "unparsed detail attachment" in persisted_sim[1]["comparison_result"][
        "warnings"
    ][0]

    changed_items = list(items)
    changed_items[0] = announcement(ids[0], title=f"{titles[0]}-변경")
    vectors[f"{titles[0]}-변경"] = [1.0, 0.0, 0.0]
    next_sync = asyncio.run(
        sync_announcements(
            engine,
            FakePublicDataClient(changed_items),
            sync_date_kst=sync_date + timedelta(days=1),
        )
    )
    assert next_sync.rows_versioned == 1
    reembedded = asyncio.run(
        embed_current_announcements(
            engine,
            embedding_client,
            requested_model_name="text-embedding-3-small",
            profile_name=profile_name,
            profile_version=1,
            preprocessing_version="detail-ref-v1",
            batch_size=3,
        )
    )
    assert reembedded.embedded_count == 1
    with engine.connect() as connection:
        versions = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM sims.announcement_version av
                JOIN sims.announcement a ON a.id = av.announcement_id
                WHERE a.pblanc_id = :pblanc_id
                """
            ),
            {"pblanc_id": ids[0]},
        )
        dimension = connection.scalar(
            text(
                """
                SELECT m.dimension
                FROM sims.embedding_profile p
                JOIN sims.embedding_model m ON m.id = p.embedding_model_id
                WHERE p.id = :profile_id
                """
            ),
            {"profile_id": embedded.embedding_profile_id},
        )
        profile_configuration = connection.scalar(
            text(
                "SELECT configuration FROM sims.embedding_profile WHERE id = :profile_id"
            ),
            {"profile_id": embedded.embedding_profile_id},
        )
        retrieval_snapshot = connection.execute(
            text(
                """
                SELECT top_k_used, filter_snapshot
                FROM sims.retrieval_run
                WHERE id = :retrieval_run_id
                """
            ),
            {"retrieval_run_id": result.retrieval_run_id},
        ).mappings().one()
        unknown_verification = connection.scalar(
            text(
                """
                SELECT rc.status_verification
                FROM sims.retrieval_candidate rc
                JOIN sims.announcement_version av
                  ON av.id = rc.announcement_version_id
                WHERE rc.retrieval_run_id = :retrieval_run_id
                  AND av.pblanc_nm = :title
                """
            ),
            {"retrieval_run_id": result.retrieval_run_id, "title": titles[1]},
        )
    assert versions == 2
    assert dimension == 3
    assert profile_configuration["query_field_codes"] == [
        "purpose",
        "target",
        "content",
    ]
    assert retrieval_snapshot["top_k_used"] == 5
    assert retrieval_snapshot["filter_snapshot"]["search_status"] == [
        "OPEN",
        "UNKNOWN",
    ]
    assert unknown_verification == "NEEDS_CONFIRMATION"

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM sims.app_user WHERE id = :user_id"),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                """
                DELETE FROM sims.announcement_version av
                USING sims.announcement a
                WHERE av.announcement_id = a.id AND a.pblanc_id = ANY(:ids)
                """
            ),
            {"ids": ids},
        )
        connection.execute(
            text("DELETE FROM sims.announcement WHERE pblanc_id = ANY(:ids)"),
            {"ids": ids},
        )
        connection.execute(
            text("DELETE FROM sims.api_sync_run WHERE sync_date_kst IN (:first, :second)"),
            {"first": sync_date, "second": sync_date + timedelta(days=1)},
        )
        connection.execute(
            text("DELETE FROM sims.embedding_profile WHERE profile_name = :profile_name"),
            {"profile_name": profile_name},
        )
        connection.execute(
            text(
                """
                DELETE FROM sims.embedding_model m
                WHERE NOT EXISTS (
                    SELECT 1 FROM sims.embedding_profile p
                    WHERE p.embedding_model_id = m.id
                )
                """
            )
        )
