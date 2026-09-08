"""``kb.*`` 적재를 실제 PostgreSQL 로 본다.

인메모리 흉내로는 확인할 수 없는 것들이다.

1. ``kb.profile_version`` 이 아티팩트 **둘** 을 NOT NULL 로 요구하고, 그 둘이
   같은 ``source_version`` 에 매인다는 것 (복합 FK).
2. ``uq_kb_*_one_current`` 부분 유니크가 "현재 판은 하나" 를 강제한다는 것 —
   새 판을 넣기 전에 이전 판을 내리지 않으면 여기서만 걸린다.
3. ``fact_occurrence`` 의 CHECK 다발(``fact_scope``·``status``·``scope``·
   ``text_basis``·``end_char > start_char``).
4. 적재한 공고가 실제로 ``result.sim_candidate`` 로 이어진다는 것.

공용 ``sims_test`` 는 건드리지 않는다. 팀원 스키마를 거기 올리면 보류 중인
tests/test_worker_queue.py 가 skip 에서 실패로 바뀐다.
"""

from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.db.identity_bridge import ensure_identity, teammate_schema_installed
from app.ports.embedding_client import EmbeddingBatch
from app.ports.llm_client import LLMUnavailableError
from worker.contracts.sim_result import SimComparisonResult
from app.infrastructure.local_object_storage import LocalObjectStorage
from semantic_structuring.common_ir_v1 import prepare_common_ir_v1
from semantic_structuring.request_profile_v012 import candidate_pack_artifact
from worker.kb_ingest import (
    KB_STORE_FAILED,
    KbCandidate,
    _load_kb_profile,
    compare_kb_candidates,
    ensure_fake_embedding_profile,
    ingest_announcement,
    profile_search_text,
    search_candidates,
    store_profile_embedding,
)
from worker.kb_store import ProfileStorageError, StoredProfile, store_existing_profile
from worker.persistence import persist_results
from worker.sim_inputs import build_common_profile


_KB_DATABASE = "sims_kb_store_test"
_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
_SCHEMA = _BACKEND / "app/db/schema.sql"
_MIGRATIONS = _BACKEND / "app/db/migrations"
_EXAMPLES = _ROOT / "packages" / "profile_structuring" / "examples"
_EXISTING_PATH = _EXAMPLES / "existing" / "structured_profile_v02.json"
_COMMON_IR_PATH = _EXAMPLES / "existing" / "common_ir_hwp_v1.json"
_REQUEST_PATH = _EXAMPLES / "request" / "structured_profile_v012.json"

_NOTICE_ID = "bizinfo:PBLN_000000000125016"
_SOURCE_PROFILE_ID = "hwp:PBLN_000000000125016"
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")

# 프로파일이 실제로 들고 있는 fact 하나. 값과 좌표가 그대로 살아 있는지를
# 개수가 아니라 이 한 줄로 고정한다.
_PURPOSE_VALUE = "청년의 사회연대경제 분야 진출 지원 및 사회연대경제 활성화"


# ------------------------------------------------------------------ DB 픽스처


@pytest.fixture(scope="session")
def engine() -> Engine:
    """sims 와 팀원 스키마를 한 DB 에 올린 이 파일 전용 DB."""
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for kb store tests")

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{_KB_DATABASE}" WITH (FORCE)'
            )
            connection.exec_driver_sql(f'CREATE DATABASE "{_KB_DATABASE}"')
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=_KB_DATABASE))
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


@pytest.fixture(autouse=True)
def clean_kb(engine: Engine):
    """테스트마다 빈 kb 로 시작한다. 이 DB 는 이 파일만 쓴다."""
    yield
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE kb.notice, kb.source_profile, kb.source_version,"
                " kb.artifact, kb.profile_version, kb.support_component,"
                " kb.fact_occurrence, kb.fact_evidence CASCADE"
            )
        )


@pytest.fixture(scope="session")
def existing_profile() -> dict:
    return json.loads(_EXISTING_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def common_ir() -> dict:
    return json.loads(_COMMON_IR_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def request_profile() -> dict:
    return json.loads(_REQUEST_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------- 읽기 도구


@pytest.fixture
def storage() -> LocalObjectStorage:
    root = pathlib.Path(tempfile.mkdtemp(prefix=".kb-storage-", dir=_ROOT))
    try:
        yield LocalObjectStorage(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _FailingStorage:
    async def open(self, _key: str):
        raise FileNotFoundError(_key)

    async def put(self, _key: str, _content):
        raise OSError("object storage offline")

    async def delete(self, _key: str) -> None:
        return None


def _profile_with_candidate_pack(profile: dict, common_ir: dict) -> dict:
    stored_profile = copy.deepcopy(profile)
    prepared, _projection = prepare_common_ir_v1(common_ir)
    stored_profile.setdefault("processing_metadata", {})[
        "candidate_pack_artifact"
    ] = candidate_pack_artifact(prepared.router_pack(), common_ir)
    return stored_profile


def _store(
    engine: Engine,
    profile: dict,
    common_ir: dict,
    storage: LocalObjectStorage,
    **kwargs,
) -> StoredProfile:
    stored_profile = _profile_with_candidate_pack(profile, common_ir)
    with engine.begin() as connection:
        return store_existing_profile(
            connection,
            profile=stored_profile,
            common_ir=common_ir,
            storage=storage,
            **kwargs,
        )


def _ingest(
    engine: Engine,
    profile: dict,
    common_ir: dict,
    storage: LocalObjectStorage,
    **kwargs,
) -> UUID:
    return _store(engine, profile, common_ir, storage, **kwargs).profile_version_pk


def _rows(engine: Engine, statement: str, **params) -> list[dict]:
    with engine.connect() as connection:
        return [
            dict(row) for row in connection.execute(text(statement), params).mappings()
        ]


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(text(f"SELECT count(*) FROM {table}"))


# --------------------------------------------------------------------- 계보


def test_검증된_공고_프로파일이_계보_한_줄로_남는다(
    engine, existing_profile, common_ir, storage
):
    profile_version_pk = _ingest(engine, existing_profile, common_ir, storage)

    assert isinstance(profile_version_pk, UUID)
    for table in (
        "kb.notice",
        "kb.source_profile",
        "kb.source_version",
        "kb.profile_version",
    ):
        assert _count(engine, table) == 1, table

    # notice_id 와 source_profile_id 는 서로 다른 값이고 둘 다 살아 있어야
    # 한다. hwp: / pdf: 접두사가 같은 공고의 두 원본을 가르는 유일한 키다.
    row = _rows(
        engine,
        "SELECT n.notice_id, sp.source_profile_id, sp.source_kind, sv.source_sha256"
        "  FROM kb.source_profile sp"
        "  JOIN kb.notice n ON n.notice_pk = sp.notice_pk"
        "  JOIN kb.source_version sv ON sv.source_profile_pk = sp.source_profile_pk",
    )[0]
    assert row["notice_id"] == _NOTICE_ID
    assert row["source_profile_id"] == _SOURCE_PROFILE_ID
    assert row["source_kind"] == "hwp"
    assert _SHA256.fullmatch(row["source_sha256"])
    assert _count(engine, "kb.support_component") == 2


def test_프로파일_판은_서로_다른_아티팩트_둘을_가리킨다(
    engine, existing_profile, common_ir, storage
):
    _ingest(engine, existing_profile, common_ir, storage)

    row = _rows(
        engine,
        "SELECT candidate_pack_artifact_pk, structured_artifact_pk, is_current,"
        "       schema_version, profile_sha256 FROM kb.profile_version",
    )[0]
    assert row["is_current"] is True
    assert row["schema_version"] == "existing_program_profile/v0.2"
    assert _SHA256.fullmatch(row["profile_sha256"])
    assert row["candidate_pack_artifact_pk"] != row["structured_artifact_pk"]

    artifacts = _rows(
        engine,
        "SELECT artifact_type, content_sha256, size_bytes FROM kb.artifact"
        " ORDER BY artifact_type",
    )
    assert [a["artifact_type"] for a in artifacts] == [
        "candidate_pack",
        "structured_profile",
    ]
    assert all(_SHA256.fullmatch(a["content_sha256"]) for a in artifacts)
    # 두 아티팩트는 다른 바이트다. 같은 해시면 계보가 무의미해진다.
    assert artifacts[0]["content_sha256"] != artifacts[1]["content_sha256"]
    assert all(a["size_bytes"] > 0 for a in artifacts)


# ----------------------------------------------------------------- fact·근거


def test_fact_가_원문과_문자_오프셋을_그대로_들고_온다(
    engine, existing_profile, common_ir, storage
):
    _ingest(engine, existing_profile, common_ir, storage)

    facts = _rows(
        engine,
        "SELECT fact_id, fact_scope, field_name, value_raw, status, scope,"
        "       source_block_id, start_char, end_char, text_basis, ordinal"
        "  FROM kb.fact_occurrence WHERE fact_id = :fact_id",
        fact_id="fact:purpose",
    )
    assert len(facts) == 1
    fact = facts[0]
    assert fact["field_name"] == "purpose_goal"
    assert fact["fact_scope"] == "comparison"
    assert fact["value_raw"] == _PURPOSE_VALUE
    assert fact["status"] == "identified"
    assert fact["scope"] == "notice"
    assert fact["source_block_id"] == "hwp:b13"
    assert (fact["start_char"], fact["end_char"]) == (9, 41)
    assert fact["text_basis"] == "common_ir_v1_candidate_pack"

    # payment_terms 는 비교 축이 아니라 공고 고유 항목이다. CHECK 가 두 집합을
    # 가르고 있어 잘못 넣으면 여기서 터진다.
    scopes = {
        row["field_name"]: row["fact_scope"]
        for row in _rows(
            engine, "SELECT DISTINCT field_name, fact_scope FROM kb.fact_occurrence"
        )
    }
    assert scopes["payment_terms"] == "existing_specific"
    assert scopes["duplicate_support_conditions"] == "existing_specific"
    assert scopes["support_scale"] == "comparison"
    assert _count(engine, "kb.fact_occurrence") == 30


def test_근거가_common_ir_블록을_그대로_들고_온다(
    engine, existing_profile, common_ir, storage
):
    _ingest(engine, existing_profile, common_ir, storage)

    rows = _rows(
        engine,
        "SELECT e.source_block_id, e.section_id, e.common_ir_document_id,"
        "       e.common_ir_block_id, e.common_ir_occurrence_ids, e.ordinal"
        "  FROM kb.fact_evidence e"
        "  JOIN kb.fact_occurrence f ON f.fact_pk = e.fact_pk"
        " WHERE f.fact_id = :fact_id ORDER BY e.ordinal",
        fact_id="fact:purpose",
    )
    assert len(rows) == 1
    assert rows[0]["common_ir_block_id"] == "hwp:b13"
    assert rows[0]["common_ir_document_id"] == "hwp:PBLN_000000000125016"
    assert rows[0]["section_id"] == "main_notice"
    assert rows[0]["common_ir_occurrence_ids"] == ["occ:rhwp:p13"]
    assert _count(engine, "kb.fact_evidence") >= 30


def test_NOT_NULL_이_빈_fact_는_건너뛰고_진단으로_남는다(
    engine, existing_profile, common_ir, storage
):
    broken = copy.deepcopy(existing_profile)
    # 값을 지어내는 대신 그 fact 하나만 버려야 한다. 나머지는 그대로 들어간다.
    broken["comparison_profile"]["purpose_goal"][0]["value_source"] = None
    broken["comparison_profile"]["program_period"][0]["value_raw"] = None
    diagnostics: list = []

    stored = _store(engine, broken, common_ir, storage, diagnostics=diagnostics)

    # 적재는 되지만 온전하지 않다. pk 하나만 돌려주면 이 둘이 호출자에게
    # 같아 보인다 — 버린 fact 가 결과에 실려야 구분된다.
    assert stored.complete is False
    assert {note.unit for note in stored.diagnostics} == {
        "fact:purpose",
        "fact:program_period",
    }
    assert all(
        note.reason_code == "FACT_REQUIRED_FIELD_MISSING"
        for note in stored.diagnostics
    )
    assert _count(engine, "kb.fact_occurrence") == 28
    assert _rows(
        engine, "SELECT 1 FROM kb.fact_occurrence WHERE fact_id = 'fact:purpose'"
    ) == []
    skipped = {
        note.unit: note.reason_code
        for note in diagnostics
        if note.reason_code == "FACT_REQUIRED_FIELD_MISSING"
    }
    assert "fact:purpose" in skipped
    assert len(skipped) == 2
    assert all(note.stage == "store_existing_profile" for note in diagnostics)


# ------------------------------------------------------------------ 재적재


def test_온전한_적재는_버린_것이_없다고_말한다(
    engine, existing_profile, common_ir, storage
):
    stored = _store(engine, existing_profile, common_ir, storage, diagnostics=[])

    assert stored.complete is True
    assert stored.diagnostics == ()
    assert _count(engine, "kb.fact_occurrence") == 30


def test_같은_프로파일을_다시_넣어도_행이_늘지_않는다(
    engine, existing_profile, common_ir, storage
):
    first = _ingest(engine, existing_profile, common_ir, storage)
    second = _ingest(engine, existing_profile, common_ir, storage)

    assert first == second
    for table in (
        "kb.notice",
        "kb.source_profile",
        "kb.source_version",
        "kb.profile_version",
    ):
        assert _count(engine, table) == 1, table
    assert _count(engine, "kb.artifact") == 2
    assert _count(engine, "kb.fact_occurrence") == 30
    assert _count(engine, "kb.support_component") == 2


def test_같은_원본의_새_프로파일은_이전_판을_현재에서_내린다(
    engine, existing_profile, common_ir, storage
):
    first = _ingest(engine, existing_profile, common_ir, storage)

    revised = copy.deepcopy(existing_profile)
    revised["comparison_profile"]["purpose_goal"][0]["value_raw"] = "개정된 목적"
    second = _ingest(engine, revised, common_ir, storage)

    assert first != second
    # 원본은 그대로다. 바뀐 것은 프로파일이지 파일이 아니다.
    assert _count(engine, "kb.source_version") == 1
    versions = {
        row["profile_version_pk"]: row["is_current"]
        for row in _rows(
            engine, "SELECT profile_version_pk, is_current FROM kb.profile_version"
        )
    }
    # 지우지 않는다. 이전 판은 남고 현재 표시만 내려간다.
    assert versions == {first: False, second: True}
    assert _rows(
        engine,
        "SELECT value_raw FROM kb.fact_occurrence"
        " WHERE profile_version_pk = :pk AND fact_id = 'fact:purpose'",
        pk=second,
    ) == [{"value_raw": "개정된 목적"}]


def test_hwp_와_pdf_는_한_공고_아래_다른_출처로_공존한다(
    engine, existing_profile, common_ir, storage
):
    hwp = _ingest(engine, existing_profile, common_ir, storage)

    as_pdf = copy.deepcopy(existing_profile)
    as_pdf["source_profile_id"] = "pdf:PBLN_000000000125016"
    pdf = _ingest(engine, as_pdf, common_ir, storage)

    assert hwp != pdf
    assert _count(engine, "kb.notice") == 1
    rows = _rows(
        engine,
        "SELECT sp.source_profile_id, sp.source_kind, pv.profile_version_pk"
        "  FROM kb.profile_version pv"
        "  JOIN kb.source_version sv ON sv.source_version_pk = pv.source_version_pk"
        "  JOIN kb.source_profile sp ON sp.source_profile_pk = sv.source_profile_pk"
        " ORDER BY sp.source_profile_id",
    )
    assert [(r["source_profile_id"], r["source_kind"]) for r in rows] == [
        ("hwp:PBLN_000000000125016", "hwp"),
        ("pdf:PBLN_000000000125016", "pdf"),
    ]
    # 둘 다 현재 판이다. 서로의 계보를 밟지 않는다.
    assert {r["profile_version_pk"] for r in rows} == {hwp, pdf}


def test_candidate_pack과_structured_profile은_실제_storage_바이트와_일치한다(
    engine, existing_profile, common_ir, storage
):
    profile_version_pk = _ingest(
        engine, existing_profile, common_ir, storage=storage
    )

    artifacts = _rows(
        engine,
        "SELECT artifact_type, storage_object_key, content_sha256, size_bytes"
        "  FROM kb.artifact ORDER BY artifact_type",
    )
    assert {row["artifact_type"] for row in artifacts} == {
        "candidate_pack",
        "structured_profile",
    }
    for row in artifacts:
        handle = asyncio.run(storage.open(row["storage_object_key"]))
        try:
            body = handle.read()
        finally:
            handle.close()
        assert len(body) == row["size_bytes"]
        assert hashlib.sha256(body).hexdigest() == row["content_sha256"]
        if row["artifact_type"] == "candidate_pack":
            candidate_pack = json.loads(body)
            assert candidate_pack["blocks"]
            assert "value_span_candidates" in candidate_pack
            assert candidate_pack["text_basis"] == "common_ir_v1_candidate_pack"
            assert candidate_pack != common_ir

    assert _rows(
        engine,
        "SELECT profile_version_pk FROM kb.profile_version"
        " WHERE profile_version_pk = :profile_version_pk",
        profile_version_pk=profile_version_pk,
    )


def test_common_ir만_있는_프로파일은_candidate_pack으로_위장하지_않는다(
    engine, existing_profile, common_ir, storage
):
    diagnostics: list = []
    with pytest.raises(ProfileStorageError):
        with engine.begin() as connection:
            store_existing_profile(
                connection,
                profile=existing_profile,
                common_ir=common_ir,
                storage=storage,
                diagnostics=diagnostics,
            )

    assert any(
        note.reason_code == "CANDIDATE_PACK_ARTIFACT_MISSING"
        for note in diagnostics
    )
    assert _count(engine, "kb.profile_version") == 0


def test_storage_없이는_KB_메타데이터도_남기지_않는다(
    engine, existing_profile, common_ir
):
    diagnostics: list = []
    with pytest.raises(ProfileStorageError):
        with engine.begin() as connection:
            store_existing_profile(
                connection,
                profile=existing_profile,
                common_ir=common_ir,
                diagnostics=diagnostics,
            )

    assert any(
        note.reason_code == "ARTIFACT_STORAGE_REQUIRED" for note in diagnostics
    )
    assert _count(engine, "kb.notice") == 0
    assert _count(engine, "kb.artifact") == 0


def test_kb_ingest_성공_흐름이_legacy_profile과_kb를_함께_완성한다(
    engine, existing_profile, common_ir, storage
):
    pblanc_id = f"KB-STORE-FLOW-{os.urandom(6).hex()}"
    with engine.begin() as connection:
        announcement_id = connection.scalar(
            text(
                "INSERT INTO sims.announcement "
                "(source_code, pblanc_id, first_seen_at, last_seen_at) "
                "VALUES ('BIZINFO_OPEN_API', :pblanc_id, now(), now()) "
                "RETURNING id"
            ),
            {"pblanc_id": pblanc_id},
        )
        version_id = connection.scalar(
            text(
                "INSERT INTO sims.announcement_version "
                "(announcement_id, version_no, content_sha256_hex, pblanc_nm, "
                "pblanc_url, bsns_sumry_text, purpose, target, content, "
                "source_created_at, period_raw_text, period_type, period_display_text) "
                "VALUES (:announcement_id, 1, :sha, 'KB flow', 'https://example.test/n', "
                "'요약', '목적', '대상', '내용', now(), '상시', 'ALWAYS', '상시') "
                "RETURNING id"
            ),
            {
                "announcement_id": announcement_id,
                "sha": f"{abs(hash(pblanc_id)):064x}"[:64],
            },
        )

    result = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id=_SOURCE_PROFILE_ID,
        document=common_ir,
        storage=storage,
        structure=lambda _document: _profile_with_candidate_pack(
            existing_profile, common_ir
        ),
    )

    assert result.status == "OK"
    assert result.profile_row_id is not None
    assert result.kb_profile_version_pk is not None
    assert _count(engine, "kb.profile_version") == 1

    profile_id = ensure_fake_embedding_profile(
        engine,
        dimension=8,
        profile_name=f"kb-store-flow-{pblanc_id}",
    )
    embedding_client = _KbEmbeddingClient(dimension=8)
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
        embedding_client=embedding_client,
    )
    assert len(candidates) == 1
    assert candidates[0].kb_profile_version_pk == result.kb_profile_version_pk


def test_kb_storage_실패시_legacy_ok를_남기지_않는다(
    engine, existing_profile, common_ir
):
    pblanc_id = f"KB-STORE-FAIL-{os.urandom(6).hex()}"
    with engine.begin() as connection:
        announcement_id = connection.scalar(
            text(
                "INSERT INTO sims.announcement "
                "(source_code, pblanc_id, first_seen_at, last_seen_at) "
                "VALUES ('BIZINFO_OPEN_API', :pblanc_id, now(), now()) "
                "RETURNING id"
            ),
            {"pblanc_id": pblanc_id},
        )
        version_id = connection.scalar(
            text(
                "INSERT INTO sims.announcement_version "
                "(announcement_id, version_no, content_sha256_hex, pblanc_nm, "
                "pblanc_url, bsns_sumry_text, purpose, target, content, "
                "source_created_at, period_raw_text, period_type, period_display_text) "
                "VALUES (:announcement_id, 1, :sha, 'KB fail', 'https://example.test/n', "
                "'요약', '목적', '대상', '내용', now(), '상시', 'ALWAYS', '상시') "
                "RETURNING id"
            ),
            {
                "announcement_id": announcement_id,
                "sha": f"{abs(hash(pblanc_id)):064x}"[:64],
            },
        )

    result = ingest_announcement(
        engine,
        announcement_version_id=version_id,
        source_profile_id=_SOURCE_PROFILE_ID,
        document=common_ir,
        storage=_FailingStorage(),
        structure=lambda _document: _profile_with_candidate_pack(
            existing_profile, common_ir
        ),
    )

    assert result.status == "FAILED"
    assert result.reason_code == KB_STORE_FAILED
    row = _rows(
        engine,
        "SELECT status, diagnostics FROM sims.announcement_profile "
        "WHERE announcement_version_id = :version_id",
        version_id=version_id,
    )[0]
    assert row["status"] == "FAILED"
    assert any(
        item["reason_code"] == KB_STORE_FAILED for item in row["diagnostics"]
    )
    assert _count(engine, "kb.profile_version") == 0


def test_fact_id_없는_행은_임의_id를_만들지_않고_진단으로_남는다(
    engine, existing_profile, common_ir, storage
):
    broken = copy.deepcopy(existing_profile)
    broken["comparison_profile"]["purpose_goal"][0].pop("fact_id")
    diagnostics: list = []

    _ingest(engine, broken, common_ir, storage, diagnostics=diagnostics)

    assert _rows(
        engine,
        "SELECT fact_id FROM kb.fact_occurrence WHERE fact_id LIKE 'purpose_goal[%]'",
    ) == []
    assert any(note.reason_code == "FACT_ID_MISSING" for note in diagnostics)


def test_source_profile_id가_다른_notice에_이미_연결되면_거부한다(
    engine, existing_profile, common_ir, storage
):
    _ingest(engine, existing_profile, common_ir, storage)
    conflicting = copy.deepcopy(existing_profile)
    conflicting["notice_id"] = "bizinfo:PBLN-CONFLICT"
    diagnostics: list = []

    with pytest.raises(ProfileStorageError) as caught:
        with engine.begin() as connection:
            store_existing_profile(
                connection,
                profile=conflicting,
                common_ir=common_ir,
                storage=storage,
                diagnostics=diagnostics,
            )

    assert any(
        note.reason_code == "SOURCE_PROFILE_NOTICE_CONFLICT"
        for note in [*diagnostics, *caught.value.diagnostics]
    )


def test_sim은_메모리가_아닌_kb_fact를_재구성해_소비한다(
    engine, existing_profile, common_ir, request_profile, storage, monkeypatch
):
    stored = copy.deepcopy(existing_profile)
    stored["comparison_profile"]["support_activities"][0]["value_raw"] = "KB_ONLY"
    profile_version_pk = _ingest(engine, stored, common_ir, storage)
    stored["comparison_profile"]["support_activities"][0]["value_raw"] = "MEMORY_ONLY"

    from worker import kb_ingest

    original_build = kb_ingest.build_common_profile
    seen: list[dict] = []

    def spy(profile, llm_client, *, model_profile):
        seen.append(profile)
        return original_build(profile, llm_client, model_profile=model_profile)

    monkeypatch.setattr(kb_ingest, "build_common_profile", spy)
    request_common = original_build(
        request_profile, _OfflineLLM(), model_profile="test-profile"
    )
    comparisons = compare_kb_candidates(
        engine,
        request_common,
        [
            KbCandidate(
                announcement_version_id=1,
                pblanc_nm="KB",
                source_profile_id=_SOURCE_PROFILE_ID,
                distance=0.0,
                kb_profile_version_pk=profile_version_pk,
            )
        ],
        _OfflineLLM(),
        model_profile="test-profile",
    )

    assert comparisons[0].result is not None
    assert (
        seen[0]["comparison_profile"]["support_activities"][0]["value_raw"]
        == "KB_ONLY"
    )
    assert "MEMORY_ONLY" not in json.dumps(seen[0], ensure_ascii=False)


# ------------------------------------------------------------- SIM 후보까지


class _OfflineLLM:
    """포트 계약대로 실패하는 스텁. Rule 변환 결과만 남게 만든다."""

    async def generate_structured(self, **_kwargs):
        raise LLMUnavailableError("offline")


class _KbEmbeddingClient:
    def __init__(self, *, dimension: int):
        self.dimension = dimension

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        from worker.kb_ingest import fake_embedding

        return EmbeddingBatch(
            model_name="kb-fake-hash",
            vectors=[fake_embedding(text, self.dimension) for text in texts],
        )


@pytest.fixture
def analysis_case_pk(engine: Engine) -> UUID:
    """``persist_results`` 가 쓸 결과 케이스 한 행."""
    with engine.begin() as connection:
        user_id = connection.scalar(
            text(
                "INSERT INTO sims.app_user (login_id, email, password_hash, display_name)"
                " VALUES (:login_id, :email, 'x', 'kb 사용자') RETURNING id"
            ),
            {
                "login_id": f"kb-{os.urandom(6).hex()}",
                "email": f"kb-{os.urandom(6).hex()}@example.test",
            },
        )
        external_uuid = ensure_identity(connection, user_id)
        return connection.scalar(
            text(
                "INSERT INTO result.analysis_case (source_analysis_run_id, user_id, case_status)"
                " VALUES (:run_id, :user_id, 'ready') RETURNING analysis_case_pk"
            ),
            {"run_id": uuid4(), "user_id": external_uuid},
        )


def test_적재한_공고가_sim_candidate_로_이어진다(
    engine,
    existing_profile,
    common_ir,
    request_profile,
    storage,
    analysis_case_pk,
):
    """적재한 ``kb.*`` 행을 다시 읽어 SIM 후보와 결과 저장까지 잇는다."""

    profile_version_pk = _ingest(engine, existing_profile, common_ir, storage)

    llm = _OfflineLLM()
    request_common = build_common_profile(request_profile, llm, model_profile="test-profile")
    # 메모리의 existing_profile을 SIM 입력으로 넘기지 않는다. 실제 검색·비교
    # 경계와 같은 ``kb.*`` 복원 경로를 통해서만 후보를 만든다.
    candidate_profile = _load_kb_profile(engine, profile_version_pk)
    assert candidate_profile is not None
    candidate_common = build_common_profile(
        candidate_profile, llm, model_profile="test-profile"
    )
    comparisons = compare_kb_candidates(
        engine,
        request_common,
        [
            KbCandidate(
                announcement_version_id=1,
                pblanc_nm="KB",
                source_profile_id=_SOURCE_PROFILE_ID,
                distance=0.0,
                kb_profile_version_pk=profile_version_pk,
            )
        ],
        llm,
        model_profile="test-profile",
    )
    candidate = comparisons[0].result
    assert candidate is not None
    assert candidate.candidate_profile_id == _SOURCE_PROFILE_ID

    sim = SimComparisonResult(
        request_profile_id=request_common.source_profile_id,
        candidates=[candidate],
        model_profile="test-profile",
        ruleset_version="sim/test",
        prompt_version="sim/test",
        scoring_version="sim/test",
    )
    with engine.begin() as connection:
        persist_results(
            connection,
            analysis_case_pk=analysis_case_pk,
            sim=sim,
            sim_profiles={
                request_common.source_profile_id: request_common,
                candidate_common.source_profile_id: candidate_common,
            },
        )

    rows = _rows(
        engine,
        "SELECT rank_no, existing_profile_version_pk FROM result.sim_candidate"
        " WHERE analysis_case_pk = :pk",
        pk=analysis_case_pk,
    )
    assert rows == [{"rank_no": 1, "existing_profile_version_pk": profile_version_pk}]

    # 후보 쪽 근거의 Common IR 블록은 KB 가 들고 있던 값이어야 한다. 메모리
    # 객체에서 온 값이면 kb.fact_evidence 집합 밖으로 나간다.
    candidate_blocks = {
        row["common_ir_block_id"]
        for row in _rows(
            engine,
            "SELECT DISTINCT common_ir_block_id FROM result.evidence_snapshot"
            " WHERE analysis_case_pk = :pk AND side = 'EXISTING'"
            "   AND axis_type = 'SIM' AND common_ir_block_id IS NOT NULL",
            pk=analysis_case_pk,
        )
    }
    kb_blocks = {
        row["common_ir_block_id"]
        for row in _rows(
            engine,
            "SELECT DISTINCT e.common_ir_block_id"
            "  FROM kb.fact_evidence e"
            "  JOIN kb.fact_occurrence f ON f.fact_pk = e.fact_pk"
            " WHERE f.profile_version_pk = :pk",
            pk=profile_version_pk,
        )
    }
    assert candidate_blocks
    assert candidate_blocks <= kb_blocks
