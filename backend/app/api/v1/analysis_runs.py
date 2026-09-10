"""Cookie-authenticated request upload and polling endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Security, UploadFile, status
from pydantic import BaseModel

from app.models.pipeline import PipelineKind
from app.pipelines.formats import FormatError, validate_format
from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    AnalysisRunPersistenceUnavailable,
    ObjectStorageUnavailable,
)
from app.services.analysis_runs import AnalysisRunService

from ..auth import PrincipalDep, access_cookie_scheme, require_trusted_origin
from .openapi_models import error_responses


router = APIRouter(prefix="/analysis-runs", tags=["Analysis"])

DEFAULT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
REQUEST_EXTENSIONS = frozenset({".hwp", ".hwpx"})
REQUEST_MIME_TYPES: dict[str, frozenset[str]] = {
    ".hwp": frozenset(
        {
            "application/x-hwp",
            "application/vnd.hancom.hwp",
            "application/haansofthwp",
            "application/octet-stream",
        }
    ),
    ".hwpx": frozenset(
        {
            "application/vnd.hancom.hwpx",
            "application/zip",
            "application/octet-stream",
        }
    ),
}
RunStatus = Literal[
    "uploading",
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "cleanup_pending",
]


class AnalysisRunCreated(BaseModel):
    analysis_run_id: str
    status: RunStatus


class AnalysisRunView(AnalysisRunCreated):
    analysis_case_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def analysis_run_service(request: Request) -> AnalysisRunService:
    service = getattr(request.app.state, "analysis_run_service", None)
    if not isinstance(service, AnalysisRunService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analysis service is not configured",
        )
    return service


AnalysisRunServiceDep = Annotated[AnalysisRunService, Depends(analysis_run_service)]
TrustedOriginDep = Annotated[None, Depends(require_trusted_origin)]


def _safe_filename(upload: UploadFile) -> tuple[str, str]:
    supplied = upload.filename or ""
    filename = Path(supplied.replace("\\", "/")).name.strip()
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded filename is invalid",
        )
    suffix = Path(filename).suffix.lower()
    if suffix not in REQUEST_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Request upload accepts .hwp and .hwpx",
        )
    supplied_mime = (upload.content_type or "").lower()
    if supplied_mime not in REQUEST_MIME_TYPES[suffix]:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Uploaded MIME type does not match the request format",
        )
    return filename, suffix


async def _bounded_content(request: Request, upload: UploadFile, filename: str) -> bytes:
    maximum = int(
        getattr(request.app.state, "upload_max_bytes", DEFAULT_UPLOAD_MAX_BYTES)
    )
    if maximum <= 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload limit is not configured correctly",
        )
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > maximum:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Uploaded file exceeds the {maximum}-byte limit",
            )
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Uploaded file is empty",
        )
    try:
        validate_format(PipelineKind.REQUEST, filename, content=content)
    except FormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc
    return content


@router.post(
    "",
    response_model=AnalysisRunCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary="요청서 업로드 및 분석 작업 생성",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 409, 413, 415, 422, 500, 503),
)
async def create_analysis_run(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    principal: PrincipalDep,
    _: TrustedOriginDep,
    service: AnalysisRunServiceDep,
) -> AnalysisRunCreated:
    filename, _suffix = _safe_filename(file)
    content = await _bounded_content(request, file, filename)
    try:
        record = await service.create(
            owner_id=principal.user_id,
            filename=filename,
            content=content,
            mime_type=file.content_type,
        )
    except ActiveAnalysisRunExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active analysis run already exists",
        ) from exc
    except (AnalysisRunPersistenceUnavailable, ObjectStorageUnavailable) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analysis storage is temporarily unavailable",
        ) from exc
    return AnalysisRunCreated(
        analysis_run_id=record.analysis_run_id,
        status=record.status,
    )


@router.get(
    "/{analysis_run_id}",
    response_model=AnalysisRunView,
    summary="분석 작업 상태 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def get_analysis_run(
    analysis_run_id: UUID,
    principal: PrincipalDep,
    service: AnalysisRunServiceDep,
) -> AnalysisRunView:
    try:
        record = await service.get(
            owner_id=principal.user_id,
            analysis_run_id=str(analysis_run_id),
        )
    except AnalysisRunPersistenceUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Analysis database is temporarily unavailable",
        ) from exc
    if record is None:
        # Deliberately hide whether a UUID belongs to another user.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Analysis run not found",
        )
    return AnalysisRunView(
        analysis_run_id=record.analysis_run_id,
        status=record.status,
        analysis_case_id=record.analysis_case_id,
        error_code=record.error_code,
        error_message=record.error_message,
    )
