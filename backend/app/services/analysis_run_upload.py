"""프론트 명세 SCR-004 의 세 걸음 업로드를 기존 분석 큐에 붙인다.

    RUN-01  edge-analysis-run-create           작업 생성 + object key 발급
    RUN-02  storage.from(bucket).upload()      브라우저 → Storage (우리 코드 아님)
    RUN-03  edge-analysis-run-complete-upload  실제 바이트 검증 + 큐 등록

기존 멀티파트 진입점(``POST /api/v1/cases``)과 **같은 큐, 같은 원본 기록**을
쓴다. 다른 것은 바이트가 저장소에 들어가는 방법 하나뿐이다. 원본 두 행은
``case_upload.record_uploaded_document`` 하나가 쓰고, 큐는
``workspace.analysis_run`` 행 하나뿐이다.

**RUN-02 는 우리 코드가 아니다.** 브라우저가 Supabase Storage 로 직접 올린다.
그래서 RUN-03 은 클라이언트 말을 믿지 않고 저장소에서 다시 읽어 검사한다.
확장자·크기·매직바이트를 전부 여기서 본다.

**bucket·object_key 를 클라이언트에게서 받지 않는다.** 경로는
(사용자 UUID, run PK, 확장자)의 순수 함수라 RUN-03 이 서버 값으로 다시
계산한다. 위조할 표면 자체가 없다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import Engine, text

from app.db.identity_bridge import ensure_identity
from app.ports.object_storage import ObjectStorage, storage_key
from app.services.case_upload import (
    InvalidUploadError,
    ValidatedUpload,
    record_uploaded_document,
    validate_declared_upload,
    validate_upload,
)
from worker.dispatcher import submission_key

logger = logging.getLogger(__name__)

__all__ = [
    "AnalysisRunNotFoundError",
    "AnalysisRunStateError",
    "CompletedUpload",
    "CreatedAnalysisRun",
    "REQUEST_TEMP_BUCKET",
    "SourceObjectMissingError",
    "complete_analysis_upload",
    "create_analysis_run",
    "source_object_key",
]

# 명세 RUN-01 응답의 고정값. 분석이 끝나면 정리되는 임시 버킷이다.
REQUEST_TEMP_BUCKET = "request-temp"

# 완료 요청을 그대로 재생해 줄 수 있는 상태. uploading 만이 실제로 일할 상태다.
_REPLAYABLE = frozenset({"queued", "running", "succeeded"})


class AnalysisRunNotFoundError(LookupError):
    """없는 run 이거나 남의 run 이다. 둘을 구분해 알려주지 않는다."""


class AnalysisRunStateError(RuntimeError):
    """이 상태에서는 업로드를 완료할 수 없다."""


class SourceObjectMissingError(AnalysisRunStateError):
    """예약한 경로에 파일이 없다. RUN-02 가 실패했거나 건너뛰었다."""


class _AlreadyQueued(Exception):
    """다른 요청이 먼저 큐에 넣었다. 이 요청의 쓰기를 전부 되돌린다."""


@dataclass(frozen=True)
class CreatedAnalysisRun:
    analysis_run_id: UUID
    bucket: str
    object_key: str


@dataclass(frozen=True)
class CompletedUpload:
    analysis_run_id: UUID
    status: str


def source_object_key(user_uuid: UUID, run_id: UUID, extension: str) -> str:
    """명세가 정한 예약 경로. 저장하지 않고 필요할 때마다 다시 만든다."""
    return f"request-source/{user_uuid}/{run_id}/source.{extension}"


def _storage_key(object_key: str) -> str:
    return storage_key(REQUEST_TEMP_BUCKET, object_key)


_CREATE_RUN = text(
    """
    INSERT INTO workspace.analysis_run (
        user_id, status, original_filename, declared_mime_type,
        declared_size_bytes
    )
    VALUES (
        :user_id, 'uploading', :original_filename, :declared_mime_type,
        :declared_size_bytes
    )
    RETURNING analysis_run_pk
    """
)

# 소유자 조건을 WHERE 에 넣는다. 남의 run 은 '없는 run' 과 같은 응답이 된다.
_OWNED_RUN = text(
    """
    SELECT status, original_filename, declared_size_bytes
      FROM workspace.analysis_run
     WHERE analysis_run_pk = :run_id
       AND user_id = :user_id
    """
)

_RUN_STATUS = text(
    """
    SELECT status
      FROM workspace.analysis_run
     WHERE analysis_run_pk = :run_id
       AND user_id = :user_id
    """
)

_CREATE_CASE = text(
    """
    INSERT INTO sims.inspection_case (owner_user_id)
    VALUES (:owner_user_id)
    RETURNING id
    """
)

_LINK_CASE = text(
    """
    UPDATE sims.inspection_case
       SET analysis_run_id = :run_id
     WHERE id = :case_id
    """
)

# status = 'uploading' 조건이 멱등성의 전부다. 두 요청이 동시에 들어와도
# UPDATE 는 하나만 1행을 돌려주고, 진 쪽은 자기 트랜잭션을 통째로 되돌린다.
_QUEUE_RUN = text(
    """
    UPDATE workspace.analysis_run
       SET status            = 'queued',
           submission_sha256 = :submission_sha256,
           updated_at        = now()
     WHERE analysis_run_pk = :run_id
       AND user_id         = :user_id
       AND status          = 'uploading'
    """
)


def create_analysis_run(
    engine: Engine,
    owner_user_id: int,
    *,
    original_filename: str | None,
    declared_mime_type: str | None,
    declared_size_bytes: int,
) -> CreatedAnalysisRun:
    """RUN-01. 작업 행 하나를 만들고 그 사용자·그 run 전용 경로를 돌려준다.

    신고값만 본다. 파일은 아직 없다. 여기서 통과했다고 정상 파일이 되는 것은
    아니고, RUN-03 이 실제 바이트로 다시 검사한다.
    """
    extension = validate_declared_upload(original_filename, declared_size_bytes)

    with engine.begin() as connection:
        user_uuid = ensure_identity(connection, owner_user_id)
        run_id = connection.scalar(
            _CREATE_RUN,
            {
                "user_id": user_uuid,
                "original_filename": original_filename,
                # 브라우저 신고값이다. 기록만 하고 판단에는 쓰지 않는다.
                "declared_mime_type": declared_mime_type,
                "declared_size_bytes": declared_size_bytes,
            },
        )
        if run_id is None:
            raise RuntimeError("Failed to create analysis run")

    return CreatedAnalysisRun(
        analysis_run_id=run_id,
        bucket=REQUEST_TEMP_BUCKET,
        object_key=source_object_key(user_uuid, run_id, extension),
    )


async def complete_analysis_upload(
    engine: Engine,
    storage: ObjectStorage,
    dispatcher,
    owner_user_id: int,
    analysis_run_id: UUID,
) -> CompletedUpload:
    """RUN-03. 올라온 바이트를 검사하고 기존 큐에 정확히 한 건 등록한다.

    검사는 트랜잭션 밖에서 하고(저장소 I/O 동안 행을 잡고 있지 않는다),
    쓰기는 전부 한 트랜잭션이다. 케이스·원본 기록·큐 등록이 함께 커밋되거나
    함께 사라진다 — 중간 상태가 남을 자리가 없다.
    """
    with engine.begin() as connection:
        user_uuid = ensure_identity(connection, owner_user_id)
        run = (
            connection.execute(
                _OWNED_RUN, {"run_id": analysis_run_id, "user_id": user_uuid}
            )
            .mappings()
            .one_or_none()
        )
    if run is None:
        raise AnalysisRunNotFoundError("Analysis run was not found")
    if run["status"] in _REPLAYABLE:
        # 같은 완료 요청의 재시도다. 아무것도 더 만들지 않는다.
        return CompletedUpload(analysis_run_id=analysis_run_id, status=run["status"])
    if run["status"] != "uploading":
        raise AnalysisRunStateError("Upload cannot be completed in this state")

    upload = await _read_and_validate(
        storage, user_uuid, analysis_run_id, run["original_filename"]
    )
    if upload.size_bytes != run["declared_size_bytes"]:
        # 신고한 크기와 실제 저장된 크기가 다르다. 올린 파일이 바뀌었다.
        raise InvalidUploadError("Stored file size does not match the declared size")

    object_key = source_object_key(user_uuid, analysis_run_id, upload.extension)
    try:
        with engine.begin() as connection:
            case_id = connection.scalar(_CREATE_CASE, {"owner_user_id": owner_user_id})
            if case_id is None:
                raise RuntimeError("Failed to create inspection case")
            record_uploaded_document(
                connection,
                owner_user_id=owner_user_id,
                case_id=case_id,
                storage_key=_storage_key(object_key),
                upload=upload,
            )
            queued = connection.execute(
                _QUEUE_RUN,
                {
                    "run_id": analysis_run_id,
                    "user_id": user_uuid,
                    "submission_sha256": submission_key(case_id),
                },
            ).rowcount
            if queued != 1:
                raise _AlreadyQueued
            connection.execute(
                _LINK_CASE, {"run_id": analysis_run_id, "case_id": case_id}
            )
    except _AlreadyQueued:
        # 케이스·원본 행은 위에서 함께 롤백됐다. 이긴 쪽의 상태를 그대로 준다.
        with engine.begin() as connection:
            current = connection.scalar(
                _RUN_STATUS, {"run_id": analysis_run_id, "user_id": user_uuid}
            )
        return CompletedUpload(
            analysis_run_id=analysis_run_id, status=current or "failed"
        )

    # 커밋된 뒤에만 실행기를 깨운다. 큐 행은 이미 DB 에 있으므로 여기서
    # 실패해도 작업이 사라지지 않는다 — 다른 워커가 집는다.
    dispatcher.dispatch(analysis_run_id)
    return CompletedUpload(analysis_run_id=analysis_run_id, status="queued")


async def _read_and_validate(
    storage: ObjectStorage,
    user_uuid: UUID,
    run_id: UUID,
    original_filename: str | None,
) -> ValidatedUpload:
    """예약 경로에서 읽어 실제 바이트를 검사한다.

    경로를 클라이언트에게 받지 않으므로 확장자는 RUN-01 이 기록한
    ``original_filename`` 에서만 나온다. 열린 파일의 내용은
    ``validate_upload`` 가 매직바이트로 다시 판정한다 — 확장자를 믿지 않는다.
    """
    extension = validate_declared_upload(original_filename, 1)
    key = _storage_key(source_object_key(user_uuid, run_id, extension))
    try:
        content: BinaryIO = await storage.open(key)
    except (OSError, ValueError) as error:
        raise SourceObjectMissingError(
            "The uploaded source file was not found"
        ) from error
    try:
        return await asyncio.to_thread(validate_upload, original_filename, content)
    finally:
        content.close()
