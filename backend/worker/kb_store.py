"""검증된 공고 ExistingProfile 한 벌을 ``kb.*`` 계보로 남긴다.

``result.sim_candidate.existing_profile_version_pk`` 는 NOT NULL 이고
``kb.profile_version`` 을 참조한다. 그런데 지금까지 ``kb.*`` 에 쓰는 코드가
없어서 ``worker/persistence.py`` 는 SIM 후보를 **전부** 버렸다. 이 모듈이
그 구멍을 메운다: 공고 프로파일 → ``kb.*`` → 후보 저장이 이어진다.

여기서 판정하지 않는다. 이미 검증된 프로파일이 들고 온 값을 스키마가 가진
자리에 그대로 옮기는 일뿐이다. 그래서 전부 Rule 이다 (CLAUDE.md
"Rule·LLM 선택 원칙" — 입력 문법과 대상 컬럼이 닫혀 있다).

두 가지를 지킨다.

1. **값을 지어내지 않는다.** ``value_raw`` · ``source_block_id`` ·
   ``start_char`` · ``end_char`` · ``text_basis`` 는 프로파일이 실제로 들고
   온 값이다. 정규화하지 않고, 없으면 그 fact 를 **건너뛰고 진단으로 남긴다**.
   NOT NULL 을 채우려고 0 이나 빈 문자열을 넣지 않는다.
2. **``notice_id`` 와 ``source_profile_id`` 를 뭉개지 않는다.** 같은 공고를
   hwp 로 읽은 것과 pdf 로 읽은 것은 ``source_profile_id`` 접두사로만
   갈린다. SIM 후보 조회가 쓰는 키가 그것이다.

트랜잭션은 열지 않는다. 호출자가 준 커넥션에 쓰기만 한다.

이 슬라이스가 담는 것은 계보 · fact · 근거 · 지원구성요소까지다.
``kb.fact_context`` · ``fact_relation`` · ``delivery_role`` ·
``target_constraint`` · 파생 투영은 SIM 후보 저장에 필요하지 않아 쓰지 않는다.

**대량 적재는 아직 막혀 있다.** 임의의 공고에서 ExistingProfile 을 만드는
3단 LLM 체인(section-scope · router · source selection)이 배선되지 않았다.
이 모듈은 **이미 검증된** 프로파일을 적재할 뿐이다.
"""

from __future__ import annotations

import hashlib
import asyncio
import io
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.ports.object_storage import ObjectStorage

from . import vendor  # noqa: F401  vendored semantic_structuring imports
from .contracts.profile_snapshot import StageDiagnostic

__all__ = ["ProfileStorageError", "StoredProfile", "store_existing_profile"]


_STAGE = "store_existing_profile"

# kb.source_profile.source_kind CHECK 의 값 집합 그대로다.
_SOURCE_KINDS = frozenset({"hwp", "hwpx", "pdf", "markdown_fixture"})

# kb.artifact.content_sha256 · kb.source_version.source_sha256 CHECK.
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")

# kb.fact_occurrence 의 (fact_scope, field_name) CHECK 를 그대로 옮긴 것이다.
# 여기 없는 컨테이너(예: delivery_relations)는 fact 모양이 아니므로 걷지 않는다.
_COMPARISON_FIELDS = frozenset(
    {
        "purpose_goal",
        "applicant_eligibility",
        "support_target",
        "eligibility_conditions",
        "beneficiary",
        "exclusions",
        "participation_requirements",
        "program_period",
        "support_period",
        "support_activities",
        "support_methods",
        "support_items",
        "support_content",
        "support_scale",
        "total_budget",
        "cost_sharing",
    }
)
_EXISTING_SPECIFIC_FIELDS = frozenset(
    {
        "payment_terms",
        "duplicate_support_conditions",
        "applicable_entity",
        "delivery_roles",
    }
)
_FACT_SCOPES = {
    **{name: "comparison" for name in _COMPARISON_FIELDS},
    **{name: "existing_specific" for name in _EXISTING_SPECIFIC_FIELDS},
}

# 나머지 CHECK 도 값 집합이다. 여기서 걸러야 프로파일 하나가 이상해도
# 적재 전체가 뒤집히지 않고 그 fact 만 진단으로 남는다.
_ENUMS = {
    "status": frozenset({"identified", "partial"}),
    "scope": frozenset({"notice", "component"}),
    "text_basis": frozenset({"common_ir_v1_candidate_pack"}),
}


class ProfileStorageError(ValueError):
    """적재 거부와 진단을 함께 호출자에게 돌려준다."""

    def __init__(self, message: str, diagnostics: list[StageDiagnostic]):
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


@dataclass(frozen=True, slots=True)
class StoredProfile:
    """적재된 판과, 그 적재에서 **버린 것**.

    ``profile_version_pk`` 만 돌려주면 fact 몇 개를 건너뛴 적재와 온전한
    적재가 호출자에게 똑같이 보인다. 둘 다 ``is_current`` 판을 남기기
    때문이다. 그래서 이 호출이 남긴 진단을 결과에 함께 싣는다.
    """

    profile_version_pk: UUID
    diagnostics: tuple[StageDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        """버린 fact·근거·구성요소가 하나도 없으면 참이다."""

        return not self.diagnostics


# ------------------------------------------------------------------ SQL

# 계보는 위에서 아래로 한 번씩만 탄다. 같은 것을 다시 적재해도 행이 늘지
# 않도록 각 단계가 자기 자연키 위에서 ON CONFLICT 를 쓴다. 자연키는 전부
# DDL 이 이미 가진 것이고, 여기서 새로 발명한 키는 없다.

_NOTICE = text(
    """
    INSERT INTO kb.notice (notice_id) VALUES (:notice_id)
    ON CONFLICT (notice_id) DO UPDATE SET updated_at = now()
    RETURNING notice_pk
    """
)

# 이미 있는 출처 프로파일의 notice_pk 는 옮기지 않는다. 같은 id 가 다른
# 공고로 이사하는 것은 적재가 아니라 정정이고, 그것은 이 모듈의 일이 아니다.
_SOURCE_PROFILE = text(
    """
    INSERT INTO kb.source_profile (notice_pk, source_profile_id, source_kind)
    VALUES (:notice_pk, :source_profile_id, :source_kind)
    ON CONFLICT (source_profile_id) DO UPDATE SET source_kind = EXCLUDED.source_kind
    RETURNING source_profile_pk
    """
)

_EXISTING_SOURCE_PROFILE = text(
    """
    SELECT source_profile_pk, notice_pk
      FROM kb.source_profile
     WHERE source_profile_id = :source_profile_id
    """
)

# uq_kb_source_version_one_current 때문에 새 원본이 오면 먼저 이전 것을
# 내려야 한다. 지우지 않는다 — 계보가 남아야 예전 판정을 되짚을 수 있다.
_DEMOTE_SOURCE_VERSIONS = text(
    """
    UPDATE kb.source_version SET is_current = FALSE
     WHERE source_profile_pk = :source_profile_pk
       AND is_current AND source_sha256 <> :source_sha256
    """
)

_SOURCE_VERSION = text(
    """
    INSERT INTO kb.source_version (
        source_profile_pk, source_sha256, source_location, source_url,
        notice_detail_url
    ) VALUES (
        :source_profile_pk, :source_sha256, :source_location, :source_url,
        :notice_detail_url
    )
    ON CONFLICT (source_profile_pk, source_sha256)
    DO UPDATE SET last_seen_at = now(), is_current = TRUE
    RETURNING source_version_pk
    """
)

# storage_object_key 를 내용 주소로 잡아 두면 같은 바이트를 다시 적재해도
# 같은 행으로 돌아온다. 그래서 DO UPDATE 는 사실상 no-op 이고, RETURNING 을
# 받으려고 있는 것이다.
_ARTIFACT = text(
    """
    INSERT INTO kb.artifact (
        source_version_pk, processing_run_pk, artifact_type, artifact_logical_id,
        storage_bucket, storage_object_key, content_sha256, mime_type,
        size_bytes, schema_version
    ) VALUES (
        :source_version_pk, :processing_run_pk, :artifact_type, :artifact_logical_id,
        :storage_bucket, :storage_object_key, :content_sha256, 'application/json',
        :size_bytes, :schema_version
    )
    ON CONFLICT (storage_bucket, storage_object_key)
    DO UPDATE SET content_sha256 = EXCLUDED.content_sha256
    RETURNING artifact_pk
    """
)

_DEMOTE_PROFILE_VERSIONS = text(
    """
    UPDATE kb.profile_version SET is_current = FALSE
     WHERE source_version_pk = :source_version_pk
       AND is_current AND profile_sha256 <> :profile_sha256
    """
)

_PROFILE_VERSION = text(
    """
    INSERT INTO kb.profile_version (
        source_version_pk, schema_version, profile_sha256,
        candidate_pack_artifact_pk, structured_artifact_pk, processing_run_pk
    ) VALUES (
        :source_version_pk, :schema_version, :profile_sha256,
        :candidate_pack_artifact_pk, :structured_artifact_pk, :processing_run_pk
    )
    ON CONFLICT (source_version_pk, profile_sha256) DO NOTHING
    RETURNING profile_version_pk
    """
)

# 같은 프로파일이 이미 적재돼 있었다. 하위 행은 그대로 두고 현재 판으로만
# 되돌린다.
_PROMOTE_PROFILE_VERSION = text(
    """
    UPDATE kb.profile_version SET is_current = TRUE
     WHERE source_version_pk = :source_version_pk
       AND profile_sha256 = :profile_sha256
    RETURNING profile_version_pk
    """
)

_SUPPORT_COMPONENT = text(
    """
    INSERT INTO kb.support_component (
        profile_version_pk, support_component_id, component_kind, name_raw,
        name_status, name_source_block_id, source_block_ids, table_block_ids,
        ordinal
    ) VALUES (
        :profile_version_pk, :support_component_id, :component_kind, :name_raw,
        :name_status, :name_source_block_id,
        CAST(:source_block_ids AS text[]), CAST(:table_block_ids AS text[]),
        :ordinal
    )
    RETURNING component_pk
    """
)

# 대상 없는 DO NOTHING 이라 (profile_version, fact_id) 중복도, comparison
# 정확 스팬 중복도 같이 막힌다. 어느 쪽이든 fact_pk 가 돌아오지 않으므로
# 근거도 함께 건너뛴다.
_FACT = text(
    """
    INSERT INTO kb.fact_occurrence (
        profile_version_pk, fact_id, fact_scope, field_name, value_raw, status,
        scope, support_component_pk, subject_role, semantic_role,
        source_block_id, start_char, end_char, text_basis, ordinal
    ) VALUES (
        :profile_version_pk, :fact_id, :fact_scope, :field_name, :value_raw, :status,
        :scope, :support_component_pk, :subject_role, :semantic_role,
        :source_block_id, :start_char, :end_char, :text_basis, :ordinal
    )
    ON CONFLICT DO NOTHING
    RETURNING fact_pk
    """
)

_FACT_EVIDENCE = text(
    """
    INSERT INTO kb.fact_evidence (
        fact_pk, source_block_id, section_id, common_ir_document_id,
        common_ir_block_id, common_ir_cell_id, common_ir_occurrence_ids, ordinal
    ) VALUES (
        :fact_pk, :source_block_id, :section_id, :common_ir_document_id,
        :common_ir_block_id, :common_ir_cell_id,
        CAST(:common_ir_occurrence_ids AS text[]), :ordinal
    )
    """
)


# ------------------------------------------------------------------ 도구


def _canonical(payload: Any) -> bytes:
    """같은 내용이면 같은 바이트. 해시가 키가 되므로 순서를 고정한다."""

    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


async def _put_bytes(
    storage: ObjectStorage, key: str, body: bytes
) -> bool:
    """Put immutable bytes, returning whether this call created the object."""

    async def read_existing() -> bool:
        handle = await storage.open(key)
        try:
            existing = handle.read()
        finally:
            handle.close()
        if hashlib.sha256(existing).hexdigest() != hashlib.sha256(body).hexdigest():
            raise ValueError(f"storage key already contains different bytes: {key}")
        if len(existing) != len(body):
            raise ValueError(f"storage key has an unexpected size: {key}")
        return False

    try:
        handle = await storage.open(key)
    except FileNotFoundError:
        try:
            stored = await storage.put(key, io.BytesIO(body))
        except FileExistsError:
            return await read_existing()
        if stored.key != key or stored.size_bytes != len(body):
            raise ValueError(f"storage returned inconsistent metadata for {key}")
        return True
    else:
        try:
            existing = handle.read()
        finally:
            handle.close()
        if hashlib.sha256(existing).hexdigest() != hashlib.sha256(body).hexdigest():
            raise ValueError(f"storage key already contains different bytes: {key}")
        if len(existing) != len(body):
            raise ValueError(f"storage key has an unexpected size: {key}")
        return False


def _put_artifact(
    storage: ObjectStorage | None, key: str, body: bytes
) -> bool:
    if storage is None:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_put_bytes(storage, key, body))
    raise RuntimeError("store_existing_profile cannot run inside an active event loop")


def _delete_created(storage: ObjectStorage | None, keys: list[str]) -> None:
    if storage is None or not keys:
        return

    async def delete_all() -> None:
        for key in keys:
            try:
                await storage.delete(key)
            except FileNotFoundError:
                pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(delete_all())
        return
    # The public storage API is async; a synchronous caller must not silently
    # leave files behind if the DB transaction fails.
    raise RuntimeError("cannot compensate storage inside an active event loop")


def _note(
    diagnostics: list[StageDiagnostic], unit: str, reason: str, message: str
) -> None:
    diagnostics.append(
        StageDiagnostic(stage=_STAGE, unit=unit, reason_code=reason, message=message)
    )


def _text(value: Any) -> str | None:
    """NOT NULL 자리에 쓸 수 있는 문자열이면 그것, 아니면 None."""

    return value if isinstance(value, str) and value else None


def _offset(value: Any) -> int | None:
    # bool 은 int 의 서브클래스라 먼저 걸러야 True 가 1 로 새지 않는다.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_storage_prefix(prefix: str) -> str:
    """Keep content-addressed keys valid for local and object storage."""

    return re.sub(r"[^A-Za-z0-9._/-]+", "_", prefix).strip("/")


def _source_document(
    profile: dict[str, Any], input_common_ir: dict[str, Any] | None = None
) -> dict[str, Any]:
    documents = profile.get("source_documents")
    first = documents[0] if isinstance(documents, list) and documents else {}
    profile_common_ir = first.get("common_ir") if isinstance(first, dict) else None
    common_ir = profile_common_ir if isinstance(profile_common_ir, dict) else None
    if common_ir is None and isinstance(input_common_ir, dict):
        source_document = input_common_ir
        identity = source_document.get("document")
        provenance = identity.get("provenance") if isinstance(identity, dict) else None
        if isinstance(identity, dict) and isinstance(provenance, dict):
            common_ir = {
                "document_id": identity.get("document_id"),
                "schema_version": source_document.get("schema_version"),
                "source_kind": identity.get("source_kind"),
                "source_sha256": provenance.get("source_sha256"),
                "source_location": provenance.get("source_location"),
            }
    return {
        "document": first if isinstance(first, dict) else {},
        "common_ir": common_ir if isinstance(common_ir, dict) else {},
    }


def _source_kind(source_profile_id: str, fallback: Any) -> str:
    """``hwp:...`` 의 접두사가 곧 원본 종류다. 접두사를 못 믿으면 Common IR 것."""

    prefix = source_profile_id.split(":", 1)[0]
    if prefix in _SOURCE_KINDS:
        return prefix
    if isinstance(fallback, str) and fallback in _SOURCE_KINDS:
        return fallback
    raise ValueError(f"unknown source_kind for source_profile_id={source_profile_id!r}")


def _artifact(
    connection: Connection,
    *,
    source_version_pk: UUID,
    processing_run_pk: UUID | None,
    artifact_type: str,
    logical_id: str | None,
    payload: Any,
    bucket: str,
    prefix: str,
    schema_version: str | None,
    storage: ObjectStorage | None,
    created_keys: list[str],
) -> UUID:
    body = _canonical(payload)
    content_sha256 = hashlib.sha256(body).hexdigest()
    key = f"{_safe_storage_prefix(prefix)}/{artifact_type}/{content_sha256}.json"
    if _put_artifact(storage, key, body):
        created_keys.append(key)
    return connection.scalar(
        _ARTIFACT,
        {
            "source_version_pk": source_version_pk,
            "processing_run_pk": processing_run_pk,
            "artifact_type": artifact_type,
            "artifact_logical_id": logical_id,
            "storage_bucket": bucket,
            "storage_object_key": key,
            "content_sha256": content_sha256,
            "size_bytes": len(body),
            "schema_version": schema_version,
        },
    )


def _candidate_pack_payload(
    profile: dict[str, Any], diagnostics: list[StageDiagnostic]
) -> dict[str, Any]:
    """Return only the exact pack used by the profile producer.

    A Common IR document is an input to routing, not a CandidatePack. Rebuilding
    one here would silently change the selected block set and break lineage, so
    old profiles without the producer's artifact are rejected instead.
    """

    processing = profile.get("processing_metadata")
    if isinstance(processing, dict):
        artifact = processing.get("candidate_pack_artifact")
        if (
            isinstance(artifact, dict)
            and isinstance(artifact.get("blocks"), list)
            and isinstance(artifact.get("value_span_candidates"), list)
            and artifact.get("text_basis") == "common_ir_v1_candidate_pack"
        ):
            return artifact
    _note(
        diagnostics,
        str(profile.get("source_profile_id") or "profile"),
        "CANDIDATE_PACK_ARTIFACT_MISSING",
        "프로파일 생산자가 사용한 exact CandidatePack artifact가 없어 Common IR을 재생성하지 않았다",
    )
    raise ProfileStorageError(
        "profile must carry the exact candidate_pack_artifact", diagnostics
    )


def _facts(profile: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """적재 대상 fact 를 프로파일 순서 그대로 편다.

    ``comparison_profile`` 의 필드 순서와 각 목록의 순서가 곧 ``ordinal``
    이다. 지원구성요소가 자기 fact 를 들고 있으면 그 뒤에 붙는다.
    """

    rows: list[tuple[str, dict[str, Any]]] = []
    container = profile.get("comparison_profile")
    for field_name, facts in (container or {}).items():
        if field_name not in _FACT_SCOPES or not isinstance(facts, list):
            continue
        rows.extend((field_name, fact) for fact in facts if isinstance(fact, dict))
    for component in profile.get("support_components") or []:
        for fact in (component or {}).get("facts") or []:
            if not isinstance(fact, dict):
                continue
            field_name = fact.get("field_name")
            if field_name in _FACT_SCOPES:
                rows.append((field_name, fact))
    return rows


def _components(
    connection: Connection,
    profile: dict[str, Any],
    profile_version_pk: UUID,
    diagnostics: list[StageDiagnostic],
) -> dict[str, UUID]:
    pks: dict[str, UUID] = {}
    for ordinal, component in enumerate(profile.get("support_components") or []):
        if not isinstance(component, dict):
            continue
        component_id = _text(component.get("support_component_id"))
        kind = _text(component.get("component_kind"))
        name_status = _text(component.get("name_status"))
        if component_id is None or kind is None or name_status is None:
            _note(
                diagnostics,
                component_id or f"support_component[{ordinal}]",
                "COMPONENT_REQUIRED_FIELD_MISSING",
                "support_component_id·component_kind·name_status 중 하나가 없다",
            )
            continue
        pks[component_id] = connection.scalar(
            _SUPPORT_COMPONENT,
            {
                "profile_version_pk": profile_version_pk,
                "support_component_id": component_id,
                "component_kind": kind,
                "name_raw": component.get("name_raw"),
                "name_status": name_status,
                "name_source_block_id": component.get("name_source_block_id"),
                "source_block_ids": list(component.get("source_block_ids") or []),
                "table_block_ids": list(component.get("table_block_ids") or []),
                "ordinal": ordinal,
            },
        )
    return pks


def _evidence(
    connection: Connection,
    fact: dict[str, Any],
    fact_pk: UUID,
    fact_id: str,
    diagnostics: list[StageDiagnostic],
) -> None:
    for ordinal, entry in enumerate(fact.get("evidence") or []):
        if not isinstance(entry, dict):
            continue
        source_block_id = _text(entry.get("source_block_id"))
        document_id = _text(entry.get("common_ir_document_id"))
        block_id = _text(entry.get("common_ir_block_id"))
        if source_block_id is None or document_id is None or block_id is None:
            _note(
                diagnostics,
                f"{fact_id}#evidence[{ordinal}]",
                "EVIDENCE_REQUIRED_FIELD_MISSING",
                "source_block_id·common_ir_document_id·common_ir_block_id 중 하나가 없다",
            )
            continue
        occurrence_ids = entry.get("common_ir_occurrence_ids")
        connection.execute(
            _FACT_EVIDENCE,
            {
                "fact_pk": fact_pk,
                "source_block_id": source_block_id,
                "section_id": entry.get("section_id"),
                "common_ir_document_id": document_id,
                "common_ir_block_id": block_id,
                "common_ir_cell_id": entry.get("common_ir_cell_id"),
                "common_ir_occurrence_ids": list(occurrence_ids)
                if isinstance(occurrence_ids, list) and occurrence_ids
                else None,
                "ordinal": ordinal,
            },
        )


# ------------------------------------------------------------------ 진입점


def store_existing_profile(
    connection: Connection,
    *,
    profile: dict[str, Any],
    common_ir: dict[str, Any],
    source_sha256: str | None = None,
    storage_bucket: str = "kb",
    storage_object_key: str | None = None,
    processing_run_pk: UUID | None = None,
    storage: ObjectStorage | None = None,
    diagnostics: list[StageDiagnostic] | None = None,
) -> StoredProfile:
    """공고 프로파일 한 벌을 ``kb.*`` 에 적재하고 ``StoredProfile`` 을 준다.

    ``storage`` 는 필수다. KB 아티팩트는 DB 메타데이터만으로는 읽을 수
    없으므로, 실제 바이트를 저장할 객체 저장소 없이 계보 행만 남기지 않는다.

    ``storage_object_key`` 는 접두사다. 실제 키는 그 아래에
    ``<artifact_type>/<content_sha256>.json`` 으로 붙는다 — 내용 주소라서
    같은 바이트를 다시 적재해도 같은 아티팩트 행으로 돌아온다.

    같은 ``(source_profile_id, profile_sha256)`` 을 다시 넣으면 아무 행도
    늘지 않고 같은 pk 가 돌아온다. 같은 원본에 새 프로파일이 오면 이전
    판의 ``is_current`` 만 내리고 행은 남긴다.

    건너뛴 fact 는 결과의 ``diagnostics`` 에 실려 돌아온다. ``diagnostics``
    를 주지 않은 호출은 부분 적재를 받아들이겠다고 말한 적이 없으므로 그때는
    ``ProfileStorageError`` 로 거절한다 — 호출자의 트랜잭션이 되돌린다.
    """

    notes = diagnostics if diagnostics is not None else []
    # 호출자가 넘긴 목록에는 앞 단계 진단이 이미 들어 있을 수 있다. 이
    # 호출이 실제로 버린 것만 결과에 싣는다.
    note_start = len(notes)

    def stored(profile_version_pk: UUID) -> StoredProfile:
        return StoredProfile(profile_version_pk, tuple(notes[note_start:]))
    if storage is None:
        _note(
            notes,
            str(profile.get("source_profile_id") or "profile"),
            "ARTIFACT_STORAGE_REQUIRED",
            "KB 아티팩트의 실제 바이트를 저장할 ObjectStorage가 필요하다",
        )
        raise ProfileStorageError(
            "store_existing_profile requires ObjectStorage", notes
        )
    created_keys: list[str] = []

    notice_id = _text(profile.get("notice_id"))
    source_profile_id = _text(profile.get("source_profile_id"))
    if notice_id is None or source_profile_id is None:
        raise ValueError("profile must carry both notice_id and source_profile_id")

    document = _source_document(profile, common_ir)
    sha = source_sha256 or document["common_ir"].get("source_sha256")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise ValueError("source_sha256 is missing or is not a SHA-256 hex digest")

    schema_version = profile.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("profile.schema_version is required")

    try:
        notice_pk = connection.scalar(_NOTICE, {"notice_id": notice_id})
        existing = connection.execute(
            _EXISTING_SOURCE_PROFILE, {"source_profile_id": source_profile_id}
        ).mappings().first()
        if existing is not None and existing["notice_pk"] != notice_pk:
            _note(
                notes,
                source_profile_id,
                "SOURCE_PROFILE_NOTICE_CONFLICT",
                "source_profile_id가 이미 다른 notice_id에 연결되어 있다",
            )
            raise ProfileStorageError(
                f"source_profile_id belongs to another notice: {source_profile_id}",
                notes,
            )

        source_profile_pk = connection.scalar(
            _SOURCE_PROFILE,
            {
                "notice_pk": notice_pk,
                "source_profile_id": source_profile_id,
                "source_kind": _source_kind(
                    source_profile_id, document["common_ir"].get("source_kind")
                ),
            },
        )

        connection.execute(
            _DEMOTE_SOURCE_VERSIONS,
            {"source_profile_pk": source_profile_pk, "source_sha256": sha},
        )
        source_version_pk = connection.scalar(
            _SOURCE_VERSION,
            {
                "source_profile_pk": source_profile_pk,
                "source_sha256": sha,
                "source_location": document["common_ir"].get("source_location"),
                "source_url": document["document"].get("source_url")
                or (profile.get("identity") or {}).get("source_url"),
                "notice_detail_url": document["document"].get("notice_detail_url"),
            },
        )

        prefix = storage_object_key or f"{source_profile_id}/{sha}"
        candidate_pack = _candidate_pack_payload(profile, notes)
        artifacts = {
            artifact_type: _artifact(
                connection,
                source_version_pk=source_version_pk,
                processing_run_pk=processing_run_pk,
                artifact_type=artifact_type,
                logical_id=logical_id,
                payload=payload,
                bucket=storage_bucket,
                prefix=prefix,
                schema_version=version,
                storage=storage,
                created_keys=created_keys,
            )
            for artifact_type, logical_id, payload, version in (
                (
                    "candidate_pack",
                    candidate_pack.get("candidate_pack_id"),
                    candidate_pack,
                    candidate_pack.get("candidate_pack_generator_version"),
                ),
                ("structured_profile", source_profile_id, profile, schema_version),
            )
        }

        profile_sha256 = hashlib.sha256(_canonical(profile)).hexdigest()
        connection.execute(
            _DEMOTE_PROFILE_VERSIONS,
            {"source_version_pk": source_version_pk, "profile_sha256": profile_sha256},
        )
        params = {
            "source_version_pk": source_version_pk,
            "schema_version": schema_version,
            "profile_sha256": profile_sha256,
            "candidate_pack_artifact_pk": artifacts["candidate_pack"],
            "structured_artifact_pk": artifacts["structured_profile"],
            "processing_run_pk": processing_run_pk,
        }
        profile_version_pk = connection.scalar(_PROFILE_VERSION, params)
        if profile_version_pk is None:
            # 같은 프로파일이 이미 있다. 하위 행을 다시 쓰지 않는다.
            return stored(connection.scalar(_PROMOTE_PROFILE_VERSION, params))

        components = _components(connection, profile, profile_version_pk, notes)

        for ordinal, (field_name, fact) in enumerate(_facts(profile)):
            fact_id = _text(fact.get("fact_id"))
            if fact_id is None:
                _note(
                    notes,
                    f"{field_name}[{ordinal}]",
                    "FACT_ID_MISSING",
                    "검증된 프로파일 fact_id가 없어 임의 식별자를 만들지 않고 건너뛴다",
                )
                continue
            value_source = fact.get("value_source")
            value_source = value_source if isinstance(value_source, dict) else {}
            source_block_id = _text(value_source.get("source_block_id"))
            start_char = _offset(value_source.get("start_char"))
            end_char = _offset(value_source.get("end_char"))
            required = {
                "value_raw": _text(fact.get("value_raw")),
                "status": _text(fact.get("status")),
                "scope": _text(fact.get("scope")),
                "source_block_id": source_block_id,
                "text_basis": _text(value_source.get("text_basis")),
            }
            missing = [name for name, value in required.items() if value is None]
            if start_char is None or end_char is None or end_char <= start_char:
                missing.append("start_char/end_char")
            if missing:
                _note(
                    notes,
                    fact_id,
                    "FACT_REQUIRED_FIELD_MISSING",
                    f"NOT NULL 자리가 비었다: {', '.join(missing)}",
                )
                continue

            unknown = [
                f"{name}={required[name]!r}"
                for name, allowed in _ENUMS.items()
                if required[name] not in allowed
            ]
            if unknown:
                _note(
                    notes,
                    fact_id,
                    "FACT_VALUE_NOT_ALLOWED",
                    f"CHECK 값 집합 밖이다: {', '.join(unknown)}",
                )
                continue

            component_id = fact.get("support_component_id")
            component_pk = components.get(component_id) if component_id else None
            if required["scope"] == "component" and component_pk is None:
                _note(
                    notes,
                    fact_id,
                    "FACT_COMPONENT_MISSING",
                    f"scope=component 인데 지원구성요소 {component_id!r} 가 없다",
                )
                continue

            fact_pk = connection.scalar(
                _FACT,
                {
                    "profile_version_pk": profile_version_pk,
                    "fact_id": fact_id,
                    "fact_scope": _FACT_SCOPES[field_name],
                    "field_name": field_name,
                    "value_raw": required["value_raw"],
                    "status": required["status"],
                    "scope": required["scope"],
                    "support_component_pk": component_pk,
                    "subject_role": fact.get("subject_role"),
                    "semantic_role": fact.get("semantic_role"),
                    "source_block_id": source_block_id,
                    "start_char": start_char,
                    "end_char": end_char,
                    "text_basis": required["text_basis"],
                    "ordinal": ordinal,
                },
            )
            if fact_pk is None:
                # (profile_version, fact_id) 나 comparison 정확 스팬이 겹쳤다.
                # 먼저 온 행이 이긴다. 덮어쓰면 근거가 둘로 갈린다.
                _note(
                    notes,
                    fact_id,
                    "FACT_DUPLICATE",
                    "같은 fact_id 또는 같은 정확 스팬이 이미 있다",
                )
                continue
            _evidence(connection, fact, fact_pk, fact_id, notes)

        if diagnostics is None and notes:
            raise ProfileStorageError(
                "profile facts were rejected; pass diagnostics to accept a partial load",
                notes,
            )
        return stored(profile_version_pk)
    except Exception:
        _delete_created(storage, created_keys)
        raise
