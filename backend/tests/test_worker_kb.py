"""Slice 4b: 첨부 적재 → 프로파일 저장 → 정확 코사인 검색 → SIM 후보 비교.

**네트워크를 타지 않는다.** 내려받기는 ``httpx.MockTransport`` 로, 구조화는
팀원이 검증한 산출물(``examples/existing``)로 돌린다. 파서(rhwp/pdf_inspector)
도 타지 않는다: 이 파일이 고정하는 것은 파서 품질이 아니라 슬라이스의 계약이다.

검색·저장은 **진짜 PostgreSQL** 에 붙는다. 벡터 차원 계약(``vector_dims``
트리거)과 ``<=>`` 정렬은 가짜 DB 로는 확인되지 않는다.

여기서 고정하는 것.

1. 검증을 통과한 프로파일만 OK 로 저장되고 ``source_profile_id`` 가 보존된다.
2. 한 공고의 실패(내려받기·구조화)가 다른 공고의 프로파일을 지우지 않는다.
3. 뽑기가 구성상 유한하다: PRIMARY·hwp/hwpx/pdf 만, limit 만큼.
4. 파일 이름은 ``Content-Disposition`` 에서 오고, 크기 상한이 지켜지고,
   재실행이 ``file_asset`` 을 두 벌 만들지 않는다.
5. 차원은 DB 프로파일 행에서 오고, 검색은 코사인 거리 순이다.
6. 프로파일이 없는 후보는 reason code 로 남고 나머지 후보 결과를 지우지 않는다.
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, create_engine, text

from worker import kb_ingest
from worker.contracts.sim_result import SimStatus
from app.ports.embedding_client import (
    EmbeddingBatch,
    EmbeddingInvalidResponseError,
    EmbeddingUnavailableError,
)
from worker.embedding_call import embed as embed_texts
from worker.kb_ingest import (
    CANDIDATE_PROFILE_FAILED,
    CANDIDATE_PROFILE_MISSING,
    FETCH_FAILED,
    PROFILE_INVALID,
    compare_kb_candidates,
    ensure_fake_embedding_profile,
    fetch_attachment,
    ingest_announcement,
    pending_attachments,
    profile_search_text,
    search_candidates,
    store_profile_embedding,
    _store_profile,
)
from worker.sim_inputs import build_common_profile

from app.infrastructure.local_object_storage import LocalObjectStorage


_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _ROOT / "packages" / "profile_structuring" / "examples" / "existing"
_MIGRATION = (
    _ROOT / "backend" / "app" / "db" / "migrations" / "001_announcement_profile.sql"
)
_REQUEST_PATH = (
    _ROOT
    / "packages"
    / "profile_structuring"
    / "examples"
    / "request"
    / "structured_profile_v012.json"
)

_MODEL_PROFILE = "test-profile"
_CLASSIFY_TASK = "sim_common_key_classification"
_COMPARISON_TASK = "sim_axis_comparison"


# --------------------------------------------------------------------- fixtures


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


@pytest.fixture(scope="module", autouse=True)
def announcement_profile_table(engine: Engine):
    """schema.sql 뒤에 이 슬라이스의 마이그레이션을 얹는다. 재실행 가능하다."""

    with engine.begin() as connection:
        connection.execute(text(_MIGRATION.read_text(encoding="utf-8")))


@pytest.fixture()
def kb_cleanup(engine: Engine):
    """이 테스트가 만든 행만 지운다. 정리는 실패해도 반드시 돈다."""

    created: dict[str, list] = {
        "announcement_ids": [],
        "profile_ids": [],
        "model_ids": [],
        "asset_ids": [],
    }
    yield created
    with engine.begin() as connection:
        if created["announcement_ids"]:
            connection.execute(
                text(
                    """
                    DELETE FROM sims.announcement_version
                     WHERE announcement_id = ANY(:ids)
                    """
                ),
                {"ids": created["announcement_ids"]},
            )
            connection.execute(
                text("DELETE FROM sims.announcement WHERE id = ANY(:ids)"),
                {"ids": created["announcement_ids"]},
            )
        if created["asset_ids"]:
            connection.execute(
                text("DELETE FROM sims.file_asset WHERE id = ANY(:ids)"),
                {"ids": created["asset_ids"]},
            )
        if created["profile_ids"]:
            connection.execute(
                text(
                    "DELETE FROM sims.embedding_profile WHERE id = ANY(:ids)"
                ),
                {"ids": created["profile_ids"]},
            )
        if created["model_ids"]:
            connection.execute(
                text("DELETE FROM sims.embedding_model WHERE id = ANY(:ids)"),
                {"ids": created["model_ids"]},
            )


@pytest.fixture()
def common_ir() -> dict:
    return json.loads((_EXAMPLES / "common_ir_hwp_v1.json").read_text(encoding="utf-8"))


@pytest.fixture()
def existing_profile() -> dict:
    return json.loads(
        (_EXAMPLES / "structured_profile_v02.json").read_text(encoding="utf-8")
    )


@pytest.fixture()
def request_profile() -> dict:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


class FakeLLM:
    """호출을 기록하는 오프라인 포트 (test_worker_sim.py 와 같은 모양)."""

    def __init__(self, **scripts):
        self._scripts = {
            _CLASSIFY_TASK: scripts.get("classify"),
            _COMPARISON_TASK: scripts.get("comparison"),
        }
        self.calls: list[tuple[str, dict]] = []

    async def generate_structured(
        self, *, task_name, messages, response_schema, model_profile
    ):
        payload = json.loads(messages[-1].content)
        self.calls.append((task_name, payload))
        script = self._scripts.get(task_name)
        if callable(script):
            script = script(payload)
        if isinstance(script, BaseException):
            raise script
        if script is None:
            return response_schema.model_validate({})
        return response_schema.model_validate(script)


class FakeEmbeddingClient:
    """Deterministic test replacement for the worker's embedding port."""

    def __init__(self, *, dimension: int, model_name: str = "kb-fake-hash"):
        self.dimension = dimension
        self.model_name = model_name
        self.calls: list[list[str]] = []
        self.error: BaseException | None = None

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return EmbeddingBatch(
            model_name=self.model_name,
            vectors=[
                kb_ingest.fake_embedding(value, self.dimension) for value in texts
            ],
        )


def test_embedding_port_boundary_rejects_wrong_model_and_non_finite_vectors():
    client = FakeEmbeddingClient(dimension=2, model_name="wrong-model")
    with pytest.raises(EmbeddingInvalidResponseError):
        embed_texts(
            client,
            ["text"],
            expected_dimension=2,
            expected_model_name="registered-model",
        )

    class NonFiniteClient:
        async def embed(self, texts):
            return EmbeddingBatch("registered-model", [[float("nan"), 0.0]])

    with pytest.raises(EmbeddingInvalidResponseError):
        embed_texts(
            NonFiniteClient(),
            ["text"],
            expected_dimension=2,
            expected_model_name="registered-model",
        )


def first_key_classifier(payload: dict) -> dict:
    return {
        "assignments": [
            {
                "fact_id": fact["fact_id"],
                "common_key": fact["allowed_common_keys"][0],
                "quoted_text": fact["value_raw"][:6],
            }
            for fact in payload["facts"]
        ]
    }


def verdict_script(status: str):
    def build(payload: dict) -> dict:
        return {
            "axes": [
                {
                    "axis": axis["axis"],
                    "status": status,
                    "reason_code": None,
                    "request_fact_ids": [row["fact_id"] for row in axis["request"][:1]],
                    "candidate_fact_ids": [
                        row["fact_id"] for row in axis["candidate"][:1]
                    ],
                    "common_points": ["같은 대상 표현"],
                    "differences": ["규모가 다르다"],
                }
                for axis in payload["axes"]
            ]
        }

    return build


# ------------------------------------------------------------------- seed 도구


def seed_announcement(engine: Engine, cleanup: dict, *, pblanc_id: str) -> int:
    """공고 하나와 현행 버전 하나. 첨부 없이."""

    with engine.begin() as connection:
        announcement_id = connection.execute(
            text(
                """
                INSERT INTO sims.announcement (
                    source_code, pblanc_id, first_seen_at, last_seen_at
                ) VALUES ('BIZINFO_OPEN_API', :pblanc_id, now(), now())
                RETURNING id
                """
            ),
            {"pblanc_id": pblanc_id},
        ).scalar_one()
        version_id = connection.execute(
            text(
                """
                INSERT INTO sims.announcement_version (
                    announcement_id, version_no, content_sha256_hex, pblanc_nm,
                    pblanc_url, bsns_sumry_text, purpose, target, content,
                    source_created_at, period_raw_text, period_type,
                    period_display_text
                ) VALUES (
                    :announcement_id, 1, :sha, :name, 'https://example.test/n',
                    '요약', '목적', '대상', '내용', now(), '상시', 'ALWAYS', '상시'
                )
                RETURNING id
                """
            ),
            {
                "announcement_id": announcement_id,
                "sha": f"{abs(hash(pblanc_id)):064x}"[:64],
                "name": f"공고 {pblanc_id}",
            },
        ).scalar_one()
    cleanup["announcement_ids"].append(int(announcement_id))
    return int(version_id)


def seed_attachment(
    engine: Engine,
    version_id: int,
    *,
    role: str = "PRIMARY",
    extension: str = "hwp",
    ordinal: int = 0,
    url: str = "https://example.test/file",
) -> dict:
    with engine.begin() as connection:
        attachment_id = connection.execute(
            text(
                """
                INSERT INTO sims.announcement_attachment (
                    announcement_version_id, attachment_role, ordinal_no,
                    source_url, original_filename, extension
                ) VALUES (
                    :version_id, :role, :ordinal, :url, :filename, :extension
                )
                RETURNING id
                """
            ),
            {
                "version_id": version_id,
                "role": role,
                "ordinal": ordinal,
                "url": url,
                "filename": f"attachment.{extension}",
                "extension": extension,
            },
        ).scalar_one()
    return {
        "id": int(attachment_id),
        "announcement_version_id": version_id,
        "source_url": url,
        "original_filename": f"attachment.{extension}",
        "extension": extension,
        "pblanc_id": f"PBLN_{version_id}",
    }


def stored_profile(engine: Engine, version_id: int) -> dict | None:
    rows = stored_profiles(engine, version_id)
    return rows[-1] if rows else None


def stored_profiles(engine: Engine, version_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                    SELECT id, source_profile_id, status, profile_json, diagnostics,
                           producer_version, source_sha256_hex, model_profile,
                           prompt_bundle_version
                      FROM sims.announcement_profile
                     WHERE announcement_version_id = :version_id
                     ORDER BY id
                    """
                ),
                {"version_id": version_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------- 1. 적재


def test_validated_common_ir_produces_a_stored_profile(
    engine, kb_cleanup, common_ir, existing_profile
):
    """팀원이 검증한 Common IR + 프로파일이 OK 로 저장되고 출처가 보존된다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-OK")
    result = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:KB-TEST-OK",
        document=common_ir,
        structure=lambda document: existing_profile,
    )

    assert result.status == "OK"
    assert result.reason_code is None
    row = stored_profile(engine, version_id)
    assert row["status"] == "OK"
    # 프로파일 자신이 들고 온 출처 식별자를 그대로 저장한다 (hwp 본과 pdf 본을
    # 구분하는 값이므로 notice_id 로 접지 않는다).
    assert row["source_profile_id"] == "hwp:PBLN_000000000125016"
    assert row["profile_json"]["schema_version"] == "existing_program_profile/v0.2"


def test_invalid_profile_is_recorded_without_touching_other_announcements(
    engine, kb_cleanup, common_ir, existing_profile
):
    """구조화 실패는 그 공고의 행에만 남는다 (초안 §9.4)."""

    good_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-GOOD")
    bad_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-BAD")
    ingest_announcement(
        engine,
        announcement_version_id=good_id,
        source_profile_id="hwp:KB-TEST-GOOD",
        document=common_ir,
        structure=lambda document: existing_profile,
    )

    broken = json.loads(json.dumps(existing_profile))
    broken["comparison_profile"]["support_items"][0]["value_raw"] = "지어낸 값"
    result = ingest_announcement(
        engine,
        announcement_version_id=bad_id,
        source_profile_id="hwp:KB-TEST-BAD",
        document=common_ir,
        structure=lambda document: broken,
    )

    assert result.status == "FAILED"
    assert result.reason_code == PROFILE_INVALID
    failed_row = stored_profile(engine, bad_id)
    assert failed_row["status"] == "FAILED"
    assert failed_row["diagnostics"][0]["reason_code"] == PROFILE_INVALID
    assert stored_profile(engine, good_id)["status"] == "OK"


def test_a_failed_rerun_does_not_erase_the_stored_profile(
    engine, kb_cleanup, common_ir, existing_profile
):
    """같은 공고를 다시 적재하다 실패해도 마지막 정상 프로파일은 남는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-RERUN")
    ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        structure=lambda document: existing_profile,
    )

    def explode(document):
        raise RuntimeError("구조화가 터졌다")

    result = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        structure=explode,
        producer_version="producer-retry",
    )

    assert result.status == "FAILED"
    rows = stored_profiles(engine, version_id)
    assert [row["status"] for row in rows] == ["OK", "FAILED"]


def test_same_generation_ok_is_reused_before_structure(
    engine, kb_cleanup, common_ir, existing_profile
):
    """동일 생성 키의 OK는 구조화 callback도 다시 호출하지 않는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-OK-REUSE")
    calls: list[int] = []

    def structure(document):
        calls.append(1)
        return existing_profile

    first = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        structure=structure,
        producer_version="producer-same",
    )
    second = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        structure=structure,
        producer_version="producer-same",
    )

    assert first.status == second.status == "OK"
    assert len(calls) == 1
    rows = stored_profiles(engine, version_id)
    assert len(rows) == 1
    assert rows[0]["model_profile"] == "not-called"
    assert rows[0]["prompt_bundle_version"] == "not-called"
    assert rows[0]["source_sha256_hex"] == common_ir["document"]["provenance"]["source_sha256"]
    assert rows[0]["profile_json"]["processing_metadata"]["generation_lineage"]["model_profile"] == "not-called"


def test_production_generation_key_reuses_and_splits_model_or_prompt(
    engine, kb_cleanup, common_ir, existing_profile, monkeypatch
):
    """Fake production producer는 동일 model/prompt에서만 조기 재사용된다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-PRODUCTION-REUSE")
    calls: list[str] = []

    def fake_producer(document, llm_client, *, model_profile):
        calls.append(model_profile)
        return existing_profile

    monkeypatch.setattr(kb_ingest, "structure_announcement_profile", fake_producer)
    original_prompt_bundle = kb_ingest.PROMPT_BUNDLE_VERSION
    fake_llm = object()
    first = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        llm_client=fake_llm,
        model_profile="model-one",
        producer_version="producer-same",
    )
    second = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        llm_client=fake_llm,
        model_profile="model-one",
        producer_version="producer-same",
    )

    monkeypatch.setattr(kb_ingest, "PROMPT_BUNDLE_VERSION", "prompt-other")
    third = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        llm_client=fake_llm,
        model_profile="model-one",
        producer_version="producer-same",
    )
    fourth = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:PBLN_000000000125016",
        document=common_ir,
        llm_client=fake_llm,
        model_profile="model-two",
        producer_version="producer-same",
    )

    assert first.status == second.status == third.status == fourth.status == "OK"
    assert calls == ["model-one", "model-one", "model-two"]
    rows = stored_profiles(engine, version_id)
    assert len(rows) == 3
    assert {row["model_profile"] for row in rows} == {"model-one", "model-two"}
    assert {row["prompt_bundle_version"] for row in rows} == {
        original_prompt_bundle,
        "prompt-other",
    }


def test_an_existing_ok_profile_is_immutable_on_a_second_ok(
    engine, kb_cleanup, common_ir, existing_profile
):
    """재실행 성공도 같은 입력·계약의 기존 OK 산출물을 바꾸지 않는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-OK-IMMUTABLE")
    source_profile_id = "hwp:PBLN_000000000125016"
    first = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id=source_profile_id,
        document=common_ir,
        structure=lambda document: existing_profile,
        producer_version="producer-first",
    )
    before = stored_profiles(engine, version_id)[0]
    changed = json.loads(json.dumps(existing_profile))
    changed["identity"]["source_url"] = "https://example.test/changed"
    second = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id=source_profile_id,
        document=common_ir,
        structure=lambda document: changed,
        producer_version="producer-second",
    )

    assert first.status == second.status == "OK"
    rows = stored_profiles(engine, version_id)
    assert len(rows) == 2
    assert {row["producer_version"] for row in rows} == {
        "producer-first",
        "producer-second",
    }
    after = next(row for row in rows if row["producer_version"] == "producer-first")
    assert after["profile_json"] == before["profile_json"]
    newer = next(row for row in rows if row["producer_version"] == "producer-second")
    assert newer["profile_json"] != before["profile_json"]


def test_generation_lineage_changes_create_new_ok_rows_without_mutating_old(
    engine, kb_cleanup
):
    """producer/model/prompt/source 계보가 달라지면 OK 행을 분리한다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-GENERATION-KEY")
    base = {
        "announcement_version_id": version_id,
        "source_profile_id": "hwp:KB-TEST-GENERATION-KEY",
        "status": "OK",
        "reason_code": None,
        "profile": {"marker": "base"},
        "producer_version": "producer-1",
        "source_sha256_hex": "a" * 64,
        "model_profile": "model-1",
        "prompt_bundle_version": "prompt-1",
        "diagnostics": [],
    }
    _store_profile(engine, **base)
    for label, changes in (
        ("producer", {"producer_version": "producer-2"}),
        ("model", {"model_profile": "model-2"}),
        ("prompt", {"prompt_bundle_version": "prompt-2"}),
        ("source", {"source_sha256_hex": "b" * 64}),
    ):
        variant = dict(base)
        variant.update(changes)
        variant["profile"] = {"marker": label}
        _store_profile(engine, **variant)

    rows = stored_profiles(engine, version_id)
    assert len(rows) == 5
    original = next(row for row in rows if row["producer_version"] == "producer-1")
    assert original["profile_json"] == {"marker": "base"}
    assert {row["status"] for row in rows} == {"OK"}


# ----------------------------------------------------------------- 2. 내려받기


def test_pending_attachments_take_only_primary_parsable_files(engine, kb_cleanup):
    """zip·png 와 AUXILIARY 는 애초에 뽑히지 않는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-SELECT")
    wanted = seed_attachment(engine, version_id, extension="hwp", ordinal=0)
    seed_attachment(engine, version_id, extension="zip", ordinal=1)
    seed_attachment(engine, version_id, extension="png", ordinal=2)
    seed_attachment(
        engine, version_id, extension="pdf", ordinal=0, role="AUXILIARY"
    )

    rows = pending_attachments(engine, limit=50)
    mine = [row for row in rows if row["announcement_version_id"] == version_id]
    assert [row["id"] for row in mine] == [wanted["id"]]

    with pytest.raises(ValueError):
        pending_attachments(engine, limit=0)


def test_download_honours_content_disposition_and_is_idempotent(
    engine, kb_cleanup, tmp_path
):
    """파일 이름은 URL 이 아니라 헤더에서 온다. 재실행이 자산을 두 벌 만들지 않는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-FETCH")
    attachment = seed_attachment(engine, version_id, extension="pdf")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # 실제 bizinfo 응답과 같은 모양: 헤더 값이 UTF-8 바이트다.
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers=[
                (
                    b"content-disposition",
                    'attachment; filename="공고문(모집).pdf"'.encode("utf-8"),
                ),
                (b"content-type", b"application/pdf"),
            ],
        )

    storage = LocalObjectStorage(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = fetch_attachment(engine, storage, attachment, client=client)
    second = fetch_attachment(engine, storage, attachment, client=client)
    client.close()
    kb_cleanup["asset_ids"].append(first.file_asset_id)

    assert first.status == "DOWNLOADED"
    assert first.filename == "공고문(모집).pdf"
    assert (tmp_path / first.storage_key).read_bytes() == b"%PDF-1.4 fake"
    assert second.status == "SKIPPED"
    assert len(calls) == 1

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT aa.fetch_status, aa.file_asset_id, fa.original_filename,
                           fa.size_bytes,
                           (SELECT count(*) FROM sims.file_asset
                             WHERE source_url = aa.source_url) AS asset_count
                      FROM sims.announcement_attachment aa
                      JOIN sims.file_asset fa ON fa.id = aa.file_asset_id
                     WHERE aa.id = :id
                    """
                ),
                {"id": attachment["id"]},
            )
            .mappings()
            .one()
        )
    assert row["fetch_status"] == "DOWNLOADED"
    assert row["original_filename"] == "공고문(모집).pdf"
    assert row["size_bytes"] == len(b"%PDF-1.4 fake")
    assert row["asset_count"] == 1


def test_download_stops_at_the_size_cap(engine, kb_cleanup, tmp_path):
    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-CAP")
    attachment = seed_attachment(engine, version_id, extension="hwp")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_attachment(
        engine,
        LocalObjectStorage(tmp_path),
        attachment,
        client=client,
        max_bytes=1024,
    )
    client.close()

    assert result.status == "FAILED"
    assert result.reason_code == FETCH_FAILED
    assert result.file_asset_id is None
    assert list(tmp_path.rglob("*")) == []


def test_failed_download_records_the_reason_and_leaves_others_alone(
    engine, kb_cleanup, tmp_path, common_ir, existing_profile
):
    good_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-DL-GOOD")
    ingest_announcement(
        engine,
        announcement_version_id=good_id,
        source_profile_id="hwp:KB-TEST-DL-GOOD",
        document=common_ir,
        structure=lambda document: existing_profile,
    )
    bad_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-DL-BAD")
    attachment = seed_attachment(engine, bad_id, extension="hwp")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = kb_ingest.ingest_attachment(
        engine,
        LocalObjectStorage(tmp_path),
        attachment,
        work_dir=tmp_path / "work",
        structure=lambda document: existing_profile,
        client=client,
    )
    client.close()

    assert result.status == "FAILED"
    assert result.reason_code == FETCH_FAILED
    assert stored_profile(engine, bad_id)["status"] == "FAILED"
    assert stored_profile(engine, good_id)["status"] == "OK"
    with engine.connect() as connection:
        fetch_status = connection.execute(
            text(
                "SELECT fetch_status FROM sims.announcement_attachment WHERE id = :id"
            ),
            {"id": attachment["id"]},
        ).scalar_one()
    assert fetch_status == "FAILED"


# ----------------------------------------------------------------- 3. 검색


def test_search_orders_candidates_by_cosine_distance(
    engine, kb_cleanup, common_ir, existing_profile
):
    """차원은 DB 프로파일 행에서 오고, 정렬은 정확 코사인이다."""

    near_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-NEAR")
    far_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-FAR")
    other = json.loads(json.dumps(existing_profile))
    for version_id, profile in ((near_id, existing_profile), (far_id, other)):
        ingest_announcement(
            engine,
            announcement_version_id=version_id,
            source_profile_id=f"hwp:{version_id}",
            document=common_ir,
            structure=lambda document, value=profile: value,
        )

    profile_id = ensure_fake_embedding_profile(engine, dimension=2560)
    kb_cleanup["profile_ids"].append(profile_id)
    with engine.connect() as connection:
        model_id, dimension = connection.execute(
            text(
                """
                SELECT m.id, m.dimension
                  FROM sims.embedding_profile p
                  JOIN sims.embedding_model m ON m.id = p.embedding_model_id
                 WHERE p.id = :profile_id
                """
            ),
            {"profile_id": profile_id},
        ).one()
    kb_cleanup["model_ids"].append(model_id)
    assert dimension == 2560, "차원은 DB 가 정한다 (HNSW 상한 2000 과 무관하다)"

    # far 후보는 다른 원문으로 임베딩한다. near 는 질의문과 같은 원문이다.
    near_text = profile_search_text(existing_profile)
    embedding_client = FakeEmbeddingClient(dimension=dimension)
    assert store_profile_embedding(
        engine,
        announcement_version_id=near_id,
        embedding_profile_id=profile_id,
        embedding_client=embedding_client,
    )
    _replace_embedding(engine, far_id, profile_id, "전혀 다른 사업 설명", dimension)

    found = search_candidates(
        engine,
        embedding_profile_id=profile_id,
        query_text=near_text,
        top_k=5,
        embedding_client=embedding_client,
    )
    mine = [row for row in found if row.announcement_version_id in (near_id, far_id)]
    assert [row.announcement_version_id for row in mine] == [near_id, far_id]
    assert mine[0].distance < mine[1].distance
    assert mine[0].distance == pytest.approx(0.0, abs=1e-6)
    assert mine[0].source_profile_id == "hwp:PBLN_000000000125016"


def test_search_and_compare_keep_the_embedded_profile_generation(
    engine, kb_cleanup, common_ir, existing_profile, request_profile
):
    """여러 OK 계보가 있어도 임베딩·검색·비교가 같은 행을 소비한다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-PROFILE-LINEAGE")
    first = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:KB-TEST-PROFILE-LINEAGE",
        document=common_ir,
        structure=lambda document: existing_profile,
        producer_version="profile-old",
    )
    assert first.profile_row_id is not None
    profile_id = ensure_fake_embedding_profile(
        engine, dimension=32, profile_name="kb-fake-lineage", version_no=1
    )
    kb_cleanup["profile_ids"].append(profile_id)
    with engine.connect() as connection:
        kb_cleanup["model_ids"].append(
            connection.execute(
                text(
                    "SELECT embedding_model_id FROM sims.embedding_profile "
                    "WHERE id = :id"
                ),
                {"id": profile_id},
            ).scalar_one()
        )
    embedding_client = FakeEmbeddingClient(dimension=32)
    assert store_profile_embedding(
        engine,
        announcement_version_id=version_id,
        embedding_profile_id=profile_id,
        embedding_client=embedding_client,
    )

    changed = json.loads(json.dumps(existing_profile))
    changed["identity"]["source_url"] = "https://example.test/new-generation"
    second = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:KB-TEST-PROFILE-LINEAGE",
        document=common_ir,
        structure=lambda document: changed,
        producer_version="profile-new",
    )
    assert second.profile_row_id is not None
    assert first.profile_row_id != second.profile_row_id
    # The conflict key is still (version, embedding profile), so a rerun moves
    # the vector lineage to the newly produced profile rather than adding a
    # duplicate embedding row.
    assert store_profile_embedding(
        engine,
        announcement_version_id=version_id,
        embedding_profile_id=profile_id,
        embedding_client=embedding_client,
    )

    candidates = search_candidates(
        engine,
        embedding_profile_id=profile_id,
        query_text=profile_search_text(changed),
        embedding_client=embedding_client,
    )
    mine = [row for row in candidates if row.announcement_version_id == version_id]
    assert len(mine) == 1
    assert mine[0].announcement_profile_id == second.profile_row_id
    with engine.connect() as connection:
        embedded_profile_id = connection.execute(
            text(
                "SELECT announcement_profile_id FROM sims.announcement_embedding "
                "WHERE announcement_version_id = :version_id "
                "AND embedding_profile_id = :profile_id"
            ),
            {"version_id": version_id, "profile_id": profile_id},
        ).scalar_one()
    assert embedded_profile_id == second.profile_row_id

    llm = FakeLLM(classify=first_key_classifier, comparison=verdict_script("SIMILAR"))
    request_common = build_common_profile(
        request_profile, llm, model_profile=_MODEL_PROFILE
    )
    comparison = compare_kb_candidates(
        engine,
        request_common,
        mine,
        llm,
        model_profile=_MODEL_PROFILE,
    )[0]
    assert comparison.announcement_profile_id == second.profile_row_id


def test_embedding_failure_does_not_replace_existing_vector(
    engine, kb_cleanup, common_ir, existing_profile
):
    """provider 실패는 기존 임베딩과 저장 프로파일을 훼손하지 않는다."""

    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-EMBED-FAIL")
    ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:KB-TEST-EMBED-FAIL",
        document=common_ir,
        structure=lambda document: existing_profile,
    )
    profile_id = ensure_fake_embedding_profile(
        engine, dimension=16, profile_name="kb-fake-failure", version_no=1
    )
    kb_cleanup["profile_ids"].append(profile_id)
    with engine.connect() as connection:
        kb_cleanup["model_ids"].append(
            connection.execute(
                text(
                    "SELECT embedding_model_id FROM sims.embedding_profile "
                    "WHERE id = :id"
                ),
                {"id": profile_id},
            ).scalar_one()
        )
    embedding_client = FakeEmbeddingClient(dimension=16)
    assert store_profile_embedding(
        engine,
        announcement_version_id=version_id,
        embedding_profile_id=profile_id,
        embedding_client=embedding_client,
    )
    with engine.connect() as connection:
        before = dict(
            connection.execute(
                text(
                    "SELECT input_sha256_hex, embedding::text AS embedding "
                    "FROM sims.announcement_embedding "
                    "WHERE announcement_version_id = :version_id "
                    "AND embedding_profile_id = :profile_id"
                ),
                {"version_id": version_id, "profile_id": profile_id},
            ).mappings().one()
        )

    embedding_client.error = RuntimeError("provider offline")
    with pytest.raises(EmbeddingUnavailableError):
        store_profile_embedding(
            engine,
            announcement_version_id=version_id,
            embedding_profile_id=profile_id,
            embedding_client=embedding_client,
        )
    with engine.connect() as connection:
        after = dict(
            connection.execute(
                text(
                    "SELECT input_sha256_hex, embedding::text AS embedding "
                    "FROM sims.announcement_embedding "
                    "WHERE announcement_version_id = :version_id "
                    "AND embedding_profile_id = :profile_id"
                ),
                {"version_id": version_id, "profile_id": profile_id},
            ).mappings().one()
        )
    assert after == before


def _replace_embedding(engine, version_id, profile_id, input_text, dimension):
    """후보 하나의 벡터를 다른 원문의 것으로 바꾼다 (거리 차이를 만든다)."""

    import hashlib

    from app.services.retrieval.corpus_embedding import vector_literal

    with engine.begin() as connection:
        announcement_profile_id = connection.execute(
            text(
                """
                SELECT id
                  FROM sims.announcement_profile
                 WHERE announcement_version_id = :version_id
                   AND status = 'OK'
                 ORDER BY id DESC
                 LIMIT 1
                """
            ),
            {"version_id": version_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO sims.announcement_embedding (
                    announcement_version_id, announcement_profile_id,
                    embedding_profile_id,
                    input_text, input_sha256_hex, embedding
                ) VALUES (
                    :version_id, :announcement_profile_id, :profile_id,
                    :input_text, :digest,
                    CAST(:embedding AS vector)
                )
                """
            ),
            {
                "version_id": version_id,
                "announcement_profile_id": announcement_profile_id,
                "profile_id": profile_id,
                "input_text": input_text,
                "digest": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                "embedding": vector_literal(
                    kb_ingest.fake_embedding(input_text, dimension)
                ),
            },
        )


# -------------------------------------------------------------- 4. SIM 후보 비교


def test_candidate_without_a_profile_keeps_the_other_results(
    engine, kb_cleanup, common_ir, existing_profile, request_profile
):
    """정보 부족 후보는 reason code 로 남고 나머지 비교를 지우지 않는다 (초안 §7.2)."""

    ok_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-SIM-OK")
    failed_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-SIM-FAIL")
    missing_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-SIM-NONE")
    ingest_announcement(
        engine,
        announcement_version_id=ok_id,
        source_profile_id="hwp:KB-TEST-SIM-OK",
        document=common_ir,
        structure=lambda document: existing_profile,
    )
    ingest_announcement(
        engine,
        announcement_version_id=failed_id,
        source_profile_id="hwp:KB-TEST-SIM-FAIL",
        document=common_ir,
        structure=lambda document: (_ for _ in ()).throw(RuntimeError("구조화 실패")),
    )

    llm = FakeLLM(classify=first_key_classifier, comparison=verdict_script("SIMILAR"))
    request_common = build_common_profile(
        request_profile, llm, model_profile=_MODEL_PROFILE
    )
    comparisons = compare_kb_candidates(
        engine,
        request_common,
        [ok_id, failed_id, missing_id],
        llm,
        model_profile=_MODEL_PROFILE,
    )

    by_id = {row.announcement_version_id: row for row in comparisons}
    assert by_id[failed_id].reason_code == CANDIDATE_PROFILE_FAILED
    assert by_id[failed_id].result is None
    # 왜 못 봤는지가 남는다: 후보 상태와 그 후보를 만들다 실패한 이유 둘 다.
    assert PROFILE_INVALID in [
        diagnostic.reason_code for diagnostic in by_id[failed_id].diagnostics
    ]
    assert by_id[missing_id].reason_code == CANDIDATE_PROFILE_MISSING
    # 나머지 후보는 그대로 4축 결과를 갖는다.
    assert by_id[ok_id].reason_code is None
    assert [axis.axis_id for axis in by_id[ok_id].result.axes] == [
        "SIM-1",
        "SIM-2",
        "SIM-3",
        "SIM-4",
    ]


def test_end_to_end_search_then_compare_produces_axis_results(
    engine, kb_cleanup, common_ir, existing_profile, request_profile
):
    """적재 → 검색 → 비교 한 줄이 실제로 축 결과까지 간다.

    이 DB 에는 팀원 kb 스키마가 없다. 후보 적재는 ``sims.announcement_profile``
    한 곳에만 남고, 후보 로딩도 그 legacy 경로로 되돌아간다 — kb 로 옮긴 뒤에도
    전환기 DB 가 오류 없이 같은 결과를 내는지가 여기서 고정된다.
    """

    assert not kb_ingest._kb_schema_available(engine)
    version_id = seed_announcement(engine, kb_cleanup, pblanc_id="KB-TEST-E2E")
    ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id="hwp:KB-TEST-E2E",
        document=common_ir,
        structure=lambda document: existing_profile,
    )
    profile_id = ensure_fake_embedding_profile(
        engine, dimension=64, profile_name="kb-fake-e2e", version_no=1
    )
    kb_cleanup["profile_ids"].append(profile_id)
    with engine.connect() as connection:
        kb_cleanup["model_ids"].append(
            connection.execute(
                text(
                    "SELECT embedding_model_id FROM sims.embedding_profile "
                    "WHERE id = :id"
                ),
                {"id": profile_id},
            ).scalar_one()
        )
    embedding_client = FakeEmbeddingClient(dimension=64)
    assert store_profile_embedding(
        engine,
        announcement_version_id=version_id,
        embedding_profile_id=profile_id,
        embedding_client=embedding_client,
    )

    candidates = search_candidates(
        engine,
        embedding_profile_id=profile_id,
        query_text=profile_search_text(existing_profile),
        top_k=5,
        embedding_client=embedding_client,
    )
    assert [row.announcement_version_id for row in candidates] == [version_id]

    llm = FakeLLM(classify=first_key_classifier, comparison=verdict_script("SIMILAR"))
    request_common = build_common_profile(
        request_profile, llm, model_profile=_MODEL_PROFILE
    )
    comparisons = compare_kb_candidates(
        engine,
        request_common,
        candidates,
        llm,
        model_profile=_MODEL_PROFILE,
    )

    result = comparisons[0].result
    assert result is not None
    assert result.candidate_profile_id == "hwp:PBLN_000000000125016"
    statuses = {axis.axis_id: axis.status for axis in result.axes}
    assert statuses["SIM-2"] is SimStatus.SIMILAR
    # 공고 v0.2 에는 전달체계 원문이 없다. 없는 축은 판정이 아니라 정보 부족이다.
    assert statuses["SIM-4"] is SimStatus.INSUFFICIENT
