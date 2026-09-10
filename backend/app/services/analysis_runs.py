"""Transactional orchestration for request upload and queue creation."""

from __future__ import annotations

import logging
from hashlib import sha256
from pathlib import Path

from app.models.artifacts import ArtifactType, MIME_BY_SUFFIX, StorageBucket
from app.pipelines.artifacts import request_object_key
from app.ports.analysis_runs import (
    AnalysisRunFinalizationRejected,
    AnalysisRunRecord,
    AnalysisRunRepository,
    ObjectStorageWriteUncertain,
    PrivateObjectStorage,
    SourceObject,
    UploadCleanupObject,
)


logger = logging.getLogger(__name__)


class AnalysisRunService:
    """Store an immutable source and expose its durable queue state.

    A durable ``uploading`` reservation is committed before private Storage is
    touched.  Only a second, atomic database transaction may register the
    source artifact and expose the run to a polling worker as ``queued``.

    A finalization outcome that cannot be proven is deliberately left intact:
    deleting its source could corrupt a transaction that actually committed.
    Expired reservations and explicit upload failures instead pass through the
    retryable ``cleanup_pending`` state.
    """

    def __init__(
        self,
        repository: AnalysisRunRepository,
        storage: PrivateObjectStorage,
    ) -> None:
        self._repository = repository
        self._storage = storage

    async def create(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None,
    ) -> AnalysisRunRecord:
        suffix = Path(filename).suffix.lower()
        digest = sha256(content).hexdigest()
        bucket = StorageBucket.REQUEST_TEMP.value
        object_key = request_object_key(
            analysis_run_pk=analysis_run_id,
            artifact_type=ArtifactType.SOURCE,
            content_sha256=digest,
            ext=suffix,
        )
        source = SourceObject(
            bucket=bucket,
            object_key=object_key,
            content_sha256=digest,
            filename=filename,
            mime_type=MIME_BY_SUFFIX[suffix],
            declared_mime_type=mime_type,
            size_bytes=len(content),
        )

        reservation = await self._repository.reserve_uploading(
            analysis_run_id=analysis_run_id,
            owner_id=owner_id,
            source=source,
        )
        for stale in reservation.cleanup_objects:
            await self._cleanup_reserved_object(stale)
        if reservation.replayed:
            return reservation.record

        try:
            await self._storage.put(
                bucket=bucket,
                object_key=object_key,
                content=content,
                content_type=source.mime_type,
            )
        except ObjectStorageWriteUncertain:
            # A timed-out POST may have committed and may still be completing.
            # Preserve both the exact reservation and its deterministic key so
            # a retry can verify/resume without racing a compensating delete.
            raise
        except Exception:
            await self._cleanup_failed_upload(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
                error_code="SOURCE_UPLOAD_FAILED",
                error_message="파일 업로드를 완료하지 못했습니다. 다시 시도해 주세요.",
            )
            raise

        try:
            return await self._repository.finalize_queued(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
            )
        except AnalysisRunFinalizationRejected:
            # The repository performed a read-back and proved that queued
            # finalization did not commit.  A conditional state transition
            # fences cleanup from any later successful finalizer.
            await self._cleanup_rejected_finalization(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
                error_code="UPLOAD_FINALIZE_FAILED",
                error_message="분석 작업 등록을 완료하지 못했습니다. 다시 시도해 주세요.",
            )
            raise

    async def get(
        self,
        *,
        owner_id: str,
        analysis_run_id: str,
    ) -> AnalysisRunRecord | None:
        return await self._repository.get_for_owner(
            analysis_run_id=analysis_run_id,
            owner_id=owner_id,
        )

    async def _cleanup_failed_upload(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
        error_code: str,
        error_message: str,
    ) -> None:
        try:
            cleanup = await self._repository.mark_upload_cleanup_pending(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            logger.error(
                "analysis upload cleanup state could not be recorded",
                extra={"analysis_run_id": analysis_run_id},
            )
            return
        if cleanup is None:
            # The exact uploading -> cleanup_pending fence was not acquired.
            # Another finalizer may already own or have queued this object.
            return
        await self._cleanup_reserved_object(cleanup)

    async def _cleanup_rejected_finalization(
        self,
        *,
        analysis_run_id: str,
        owner_id: str,
        source: SourceObject,
        error_code: str,
        error_message: str,
    ) -> None:
        try:
            cleanup = await self._repository.mark_upload_cleanup_pending(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
                error_code=error_code,
                error_message=error_message,
            )
        except Exception:
            logger.error(
                "analysis finalization cleanup state could not be recorded",
                extra={"analysis_run_id": analysis_run_id},
            )
            return
        if cleanup is not None:
            await self._cleanup_reserved_object(cleanup)

    async def _cleanup_reserved_object(self, cleanup: UploadCleanupObject) -> None:
        try:
            await self._storage.delete(
                bucket=cleanup.bucket,
                object_key=cleanup.object_key,
            )
        except Exception:
            # The cleanup_pending row and dispatch key remain durable so a
            # later upload request can retry this idempotent delete.
            logger.error(
                "analysis reserved source cleanup failed",
                extra={"analysis_run_id": cleanup.analysis_run_id},
            )
            return
        await self._complete_cleanup_best_effort(cleanup.analysis_run_id)

    async def _complete_cleanup_best_effort(self, analysis_run_id: str) -> None:
        try:
            await self._repository.complete_upload_cleanup(
                analysis_run_id=analysis_run_id,
            )
        except Exception:
            logger.error(
                "analysis upload cleanup completion could not be recorded",
                extra={"analysis_run_id": analysis_run_id},
            )
