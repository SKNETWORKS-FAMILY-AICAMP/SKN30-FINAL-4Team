"""Transactional orchestration for request upload and queue creation."""

from __future__ import annotations

import logging
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.models.artifacts import ArtifactType, MIME_BY_SUFFIX, StorageBucket
from app.pipelines.artifacts import request_object_key
from app.ports.analysis_runs import (
    AnalysisRunRecord,
    AnalysisRunRepository,
    PrivateObjectStorage,
    SourceObject,
)


logger = logging.getLogger(__name__)


class AnalysisRunService:
    """Store an immutable source and expose its durable queue state.

    The content-addressed object is uploaded first with a service credential.
    If the following database transaction fails, best-effort compensation
    removes that object. The original exception is always preserved.
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
        owner_id: str,
        filename: str,
        content: bytes,
        mime_type: str | None,
    ) -> AnalysisRunRecord:
        analysis_run_id = str(uuid4())
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

        await self._storage.put(
            bucket=bucket,
            object_key=object_key,
            content=content,
            content_type=source.mime_type,
        )
        try:
            return await self._repository.create_queued(
                analysis_run_id=analysis_run_id,
                owner_id=owner_id,
                source=source,
            )
        except Exception:
            try:
                await self._storage.delete(bucket=bucket, object_key=object_key)
            except Exception:
                # Never replace the actionable DB failure with cleanup noise and
                # never log credentials, content, or a user-provided filename.
                logger.error(
                    "analysis source compensation failed",
                    extra={"analysis_run_id": analysis_run_id},
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
