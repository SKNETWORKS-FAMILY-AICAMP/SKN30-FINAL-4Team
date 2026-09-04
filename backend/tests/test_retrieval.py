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
from app.ports.public_data_client import (
    PublicAnnouncement,
    PublicDataInvalidResponseError,
    PublicDataUnavailableError,
)
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


PORTAL_URL = "https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService"


def _page(
    items: list[dict[str, Any]],
    *,
    total_count: int,
    result_code: str = "00",
    page_size: int = 500,
) -> dict[str, Any]:
    """공공데이터포털 응답 봉투를 만든다."""
    return {
        "response": {
            "header": {"resultCode": result_code, "resultMsg": "MSG"},
            "body": {
                "totalCount": total_count,
                "pageNo": 1,
                "numOfRows": page_size,
                "items": {"item": items},
            },
        }
    }


def _item(index: int) -> dict[str, Any]:
    return {
        "pblancId": f"PBLN_{index}",
        "pblancNm": f"공고 {index}",
        "pblancUrl": f"https://example.com/{index}",
        "creatPnttm": "2026-08-25 09:00:00",
        "reqstBeginEndDe": "20260801 ~ 20260831",
        "bsnsSumryCn": "사업 개요",
    }


def test_bizinfo_adapter_uses_official_json_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pblancBsnsService")
        assert request.url.params["serviceKey"] == "secret"
        assert request.url.params["dataType"] == "json"
        assert request.url.params["pageNo"] == "1"
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                    "body": {
                        "totalCount": 1,
                        "pageNo": 1,
                        "numOfRows": 500,
                        "items": {
                            "item": {
                                "pblancId": "PBLN_1",
                                "pblancNm": "지원 공고",
                                "pblancUrl": "https://example.com/1",
                                "jrsdInsttNm": "기관",
                                "bsnsSumryCn": "사업 개요",
                                "creatPnttm": "2026-08-25 09:00:00",
                                "reqstBeginEndDe": "20260801 ~ 20260831",
                            }
                        },
                    },
                }
            },
        )

    client = BizinfoPublicDataClient(
        api_key="secret",
        base_url="https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService",
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
        return httpx.Response(200, json=_page([], total_count=0))

    client = BizinfoPublicDataClient(
        api_key="secret",
        base_url="https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService",
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
    """이 테스트가 만든 공고에만 정해진 벡터를 요구한다.

    임베딩 대상은 새 프로필에 임베딩이 없는 현재 공고 전부다. 새 프로필을
    만들면 DB 에 이미 있던 공고까지 대상이 되므로, 코퍼스가 비어 있다고
    가정하면 남의 공고에서 멈춘다.

    그런 공고에는 질의와 직교하는 벡터를 준다. 유사도가 0 이라 이 테스트가
    만든 후보들보다 항상 뒤에 오고, 상위 후보 검사에 끼어들지 않는다.
    """

    # 질의 벡터는 [1, 0, 0] 이다. 이 값은 그것과 직교한다.
    OUTSIDE_CORPUS_VECTOR = [0.0, 0.0, 1.0]

    def __init__(
        self,
        vectors_by_marker: dict[str, list[float]],
        *,
        owned_marker: str,
    ) -> None:
        self.vectors_by_marker = vectors_by_marker
        self.owned_marker = owned_marker
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
                if self.owned_marker in value:
                    raise AssertionError(f"No fake vector for: {value}")
                matched = self.OUTSIDE_CORPUS_VECTOR
            vectors.append(matched)
        return EmbeddingBatch(
            # 테스트마다 고유한 identity를 사용해 기존 모델의
            # is_enabled를 ON CONFLICT로 바꾸지 않게 한다.
            model_name=f"text-embedding-3-small-test-{self.owned_marker}",
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


@pytest.fixture
def retrieval_cleanup(engine: Engine):
    """이 테스트가 만든 것을 반드시 지운다.

    정리를 본문 끝에 두면 단언이 하나만 실패해도 돌지 않아 공용 DB 에
    공고가 쌓인다. 실제로 이 테스트가 실패하는 동안 273건이 남았다.
    """
    created: dict[str, list] = {
        "users": [],
        "announcement_ids": [],
        "sync_run_ids": [],
        "profile_ids": [],
        "embedding_model_ids": [],
    }
    yield created
    with engine.begin() as connection:
        if created["users"]:
            connection.execute(
                text("DELETE FROM sims.app_user WHERE id = ANY(:ids)"),
                {"ids": created["users"]},
            )
        if created["announcement_ids"]:
            connection.execute(
                text(
                    """
                    DELETE FROM sims.announcement_embedding ae
                    USING sims.announcement_version av
                    WHERE ae.announcement_version_id = av.id
                      AND av.announcement_id = ANY(:ids)
                    """
                ),
                {"ids": created["announcement_ids"]},
            )
            connection.execute(
                text(
                    "DELETE FROM sims.announcement_version "
                    "WHERE announcement_id = ANY(:ids)"
                ),
                {"ids": created["announcement_ids"]},
            )
            connection.execute(
                text("DELETE FROM sims.announcement WHERE id = ANY(:ids)"),
                {"ids": created["announcement_ids"]},
            )
        if created["sync_run_ids"]:
            connection.execute(
                text("DELETE FROM sims.api_sync_run WHERE id = ANY(:ids)"),
                {"ids": created["sync_run_ids"]},
            )
        if created["profile_ids"]:
            # 이 프로필로 만들어진 임베딩은 남의 공고에도 붙는다. 새 프로필을
            # 만들면 코퍼스 전체가 임베딩 대상이 되기 때문이다. 프로필을
            # 지우려면 그것들부터 지워야 한다.
            for table, column in (
                ("announcement_embedding", "embedding_profile_id"),
                ("inspection_embedding", "embedding_profile_id"),
                ("chunk_embedding", "embedding_profile_id"),
            ):
                connection.execute(
                    text(
                        f"""
                        DELETE FROM sims.{table}
                        WHERE {column} = ANY(:ids)
                        """
                    ),
                    {"ids": created["profile_ids"]},
                )
            connection.execute(
                text("DELETE FROM sims.embedding_profile WHERE id = ANY(:ids)"),
                {"ids": created["profile_ids"]},
            )
        if created["embedding_model_ids"]:
            connection.execute(
                text("DELETE FROM sims.embedding_model WHERE id = ANY(:ids)"),
                {"ids": created["embedding_model_ids"]},
            )


def test_sync_versioning_embedding_profile_and_exact_top_five(
    engine: Engine,
    retrieval_cleanup: dict,
) -> None:
    marker = uuid.uuid4().hex[:10]
    profile_name = f"retrieval-test-{marker}"
    ids = [f"PBLN_{marker}_{index}" for index in range(7)]
    titles = [f"후보-{marker}-{index}" for index in range(7)]
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM sims.announcement "
                "WHERE pblanc_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).scalar_one() == 0
        existing_profile_id = connection.scalar(
            text(
                "SELECT id FROM sims.embedding_profile "
                "WHERE profile_name = :profile_name AND version_no = 1"
            ),
            {"profile_name": profile_name},
        )
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
    if not sync_result.reused_success:
        retrieval_cleanup["sync_run_ids"].append(sync_result.sync_run_id)
    with engine.connect() as connection:
        retrieval_cleanup["announcement_ids"] = list(
            connection.execute(
                text(
                    "SELECT id FROM sims.announcement "
                    "WHERE pblanc_id = ANY(:ids)"
                ),
                {"ids": ids},
            ).scalars()
        )
    assert sync_result.rows_inserted == 7

    reused = asyncio.run(
        sync_announcements(engine, public_client, sync_date_kst=sync_date)
    )
    if not reused.reused_success:
        retrieval_cleanup["sync_run_ids"].append(reused.sync_run_id)
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
    embedding_client = FakeEmbeddingClient(vectors, owned_marker=marker)
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
    if (
        embedded.embedding_profile_id is not None
        and embedded.embedding_profile_id != existing_profile_id
    ):
        retrieval_cleanup["profile_ids"].append(embedded.embedding_profile_id)
        with engine.connect() as connection:
            model_id = connection.scalar(
                text(
                    "SELECT embedding_model_id FROM sims.embedding_profile "
                    "WHERE id = :profile_id"
                ),
                {"profile_id": embedded.embedding_profile_id},
            )
        if model_id is not None:
            retrieval_cleanup["embedding_model_ids"].append(model_id)
    # 임베딩 대상은 이 프로필에 임베딩이 없는 현재 공고 전부다. 새 프로필을
    # 만들었으니 DB 에 이미 있던 공고까지 포함된다. 이 테스트가 만든 7건이
    # 모두 임베딩됐는지만 확인한다.
    with engine.connect() as connection:
        owned_embedded = connection.scalar(
            text(
                """
                SELECT count(*)
                FROM sims.announcement_embedding ae
                JOIN sims.announcement_version av
                  ON av.id = ae.announcement_version_id
                JOIN sims.announcement a ON a.id = av.announcement_id
                WHERE a.pblanc_id = ANY(:ids)
                """
            ),
            {"ids": ids},
        )
    assert owned_embedded == 7
    assert embedded.embedded_count >= 7

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
        retrieval_cleanup["users"] = [user_id]
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
    if not next_sync.reused_success:
        retrieval_cleanup["sync_run_ids"].append(next_sync.sync_run_id)
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


def _portal_client(handler) -> BizinfoPublicDataClient:
    return BizinfoPublicDataClient(
        api_key="secret",
        base_url=PORTAL_URL,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )


def test_bizinfo_adapter_collects_every_page() -> None:
    page_size = 2
    pages = {1: [_item(1), _item(2)], 2: [_item(3), _item(4)], 3: [_item(5)]}

    async def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["pageNo"])
        return httpx.Response(
            200,
            json=_page(pages[page_no], total_count=5, page_size=page_size),
        )

    items = asyncio.run(_portal_client(handler).list_current_announcements())
    assert [item.pblanc_id for item in items] == [f"PBLN_{n}" for n in range(1, 6)]


def test_bizinfo_adapter_deduplicates_overlapping_pages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        page_no = int(request.url.params["pageNo"])
        # 2페이지가 1페이지의 마지막 항목을 다시 돌려준다.
        items = [_item(1), _item(2)] if page_no == 1 else [_item(2), _item(3)]
        return httpx.Response(200, json=_page(items, total_count=3, page_size=2))

    items = asyncio.run(_portal_client(handler).list_current_announcements())
    assert [item.pblanc_id for item in items] == ["PBLN_1", "PBLN_2", "PBLN_3"]


def test_bizinfo_adapter_stops_when_a_page_repeats_itself() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # pageNo 를 무시하고 늘 같은 페이지를 주는 서버.
        return httpx.Response(
            200, json=_page([_item(1), _item(2)], total_count=999, page_size=2)
        )

    items = asyncio.run(_portal_client(handler).list_current_announcements())
    assert len(items) == 2
    assert calls == 2


def test_bizinfo_adapter_returns_empty_list_when_no_announcements() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        # 0건이면 items 가 빈 문자열로 오기도 한다.
        body = _page([], total_count=0)
        body["response"]["body"]["items"] = ""
        return httpx.Response(200, json=body)

    assert asyncio.run(_portal_client(handler).list_current_announcements()) == []


def test_bizinfo_adapter_does_not_retry_daily_quota_exceeded() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_page([], total_count=0, result_code="22"))

    with pytest.raises(PublicDataUnavailableError):
        asyncio.run(_portal_client(handler).list_current_announcements())
    assert calls == 1


def test_bizinfo_adapter_retries_rate_limited_result_then_fails() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_page([], total_count=0, result_code="23"))

    with pytest.raises(PublicDataUnavailableError):
        asyncio.run(_portal_client(handler).list_current_announcements())
    assert calls == 2


def test_bizinfo_adapter_rejects_a_broken_envelope() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    with pytest.raises(PublicDataInvalidResponseError):
        asyncio.run(_portal_client(handler).list_current_announcements())
