"""Cookie-authenticated request upload and polling endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Security, UploadFile, status
from pydantic import BaseModel, Field, UUID4

from app.models.pipeline import PipelineKind
from app.pipelines.formats import FormatError, validate_format
from app.ports.analysis_runs import (
    ActiveAnalysisRunExists,
    ActiveResultSessionExists,
    AnalysisQueueCapacityExceeded,
    AnalysisRunPersistenceUnavailable,
    IdempotencyKeyConflict,
    ObjectStorageUnavailable,
)
from app.services.analysis_runs import AnalysisRunService

from ..auth import PrincipalDep, access_cookie_scheme, require_trusted_origin
from ..errors import (
    active_result_session,
    analysis_run_active,
    idempotency_key_conflict,
    not_found,
    service_unavailable,
)
from .openapi_models import error_responses


router = APIRouter(prefix="/analysis-runs", tags=["Analysis"])

DEFAULT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
REQUEST_EXTENSIONS = frozenset({".hwp", ".hwpx"})
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
    analysis_run_id: str = Field(description="분석 작업 UUID")
    status: RunStatus = Field(
        description=(
            "분석 작업 상태. uploading은 업로드 확정 전, queued는 worker 대기, "
            "running은 처리 중, succeeded는 완료, failed는 실패, cancelled는 취소, "
            "cleanup_pending은 실패 후 저장 객체 정리 대기 상태다."
        )
    )


class AnalysisRunView(AnalysisRunCreated):
    analysis_case_id: str | None = Field(
        description="status=succeeded일 때 반드시 non-null이며 결과 조회에 사용할 분석 case UUID",
    )
    error_code: str | None = Field(
        description="status=failed일 때 분기 가능한 안전한 오류 코드",
    )
    error_message: str | None = Field(
        description="status=failed일 때 화면에 표시 가능한 오류 메시지",
    )


async def analysis_run_service(request: Request) -> AnalysisRunService:
    # ``async def`` is deliberate: FastAPI runs a plain ``def`` dependency
    # through anyio's threadpool on every call. This dependency only reads
    # ``app.state`` and never blocks, so paying for a thread hop just adds
    # threadpool contention under load with zero benefit -- it competes for
    # the same bounded worker pool as genuinely blocking sync calls
    # elsewhere (e.g. UploadFile's spooled-file reads), which is exactly the
    # "sync dependency threadpool timeout" the v0.2 validation pass
    # reproduced and this removes (see tests/test_asgi_sync_dependency.py).
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
    # Browser-reported MIME is untrusted metadata, not a portable format
    # signal. Windows application registration can make the exact same HWPX
    # bytes arrive as ``application/vnd.hancom.hwp`` when Hancom Office owns
    # the extension, while another association reports ZIP or no MIME at all.
    # The bounded content gate below validates HWP OLE magic and the complete
    # required HWPX ZIP structure, so rejecting on this metadata adds no
    # security boundary and creates environment-dependent false negatives.
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
    description=(
        "HWP/HWPX 요청서를 업로드하고 queued 분석 작업을 만든다. 같은 파일을 네트워크 "
        "재시도할 때는 동일한 Idempotency-Key를 사용한다. 활성 분석 작업이나 결과 "
        "세션이 있으면 새 작업을 만들지 않는다."
    ),
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 409, 413, 415, 422, 429, 500, 502, 503),
)
async def create_analysis_run(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    idempotency_key: Annotated[
        UUID4,
        Header(
            alias="Idempotency-Key",
            description="재시도에서도 동일하게 보내는 클라이언트 생성 UUID",
        ),
    ],
    principal: PrincipalDep,
    _: TrustedOriginDep,
    service: AnalysisRunServiceDep,
) -> AnalysisRunCreated:
    filename, _suffix = _safe_filename(file)
    upload_semaphore = getattr(request.app.state, "upload_semaphore", None)
    if upload_semaphore is None:
        raise service_unavailable("Analysis upload capacity is not configured")
    # Starlette has already parsed/spooled the multipart part. Keep the much
    # larger file-byte materialisation and outbound Storage request within a
    # separate, small process-wide budget so Uvicorn's general request limit
    # cannot multiply the transient b''.join allocation by every connection.
    async with upload_semaphore:
        content = await _bounded_content(request, file, filename)
        try:
            record = await service.create(
                idempotency_key=str(idempotency_key),
                owner_id=principal.user_id,
                filename=filename,
                content=content,
                mime_type=file.content_type,
            )
        except IdempotencyKeyConflict as exc:
            raise idempotency_key_conflict() from exc
        except ActiveResultSessionExists as exc:
            raise active_result_session() from exc
        except ActiveAnalysisRunExists as exc:
            raise analysis_run_active() from exc
        except AnalysisQueueCapacityExceeded as exc:
            raise service_unavailable("Analysis queue is temporarily full") from exc
        except (AnalysisRunPersistenceUnavailable, ObjectStorageUnavailable) as exc:
            raise service_unavailable("Analysis storage is temporarily unavailable") from exc
    return AnalysisRunCreated(
        analysis_run_id=record.analysis_run_id,
        status=record.status,
    )


@router.get(
    "/{analysis_run_id}",
    response_model=AnalysisRunView,
    summary="분석 작업 상태 조회",
    description=(
        "업로드 응답의 analysis_run_id를 polling합니다. uploading/queued/running이면 계속 "
        "조회하고, succeeded이면 반드시 non-null인 analysis_case_id로 결과를 조회합니다. "
        "failed/cancelled/cleanup_pending이면 polling을 중단하고 오류·정리 상태를 표시합니다. "
        "실패한 run을 다시 실행하는 endpoint는 없으며 재시도는 새 Idempotency-Key를 사용한 "
        "새 파일 업로드입니다."
    ),
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 429, 500, 502, 503),
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
        raise service_unavailable("Analysis database is temporarily unavailable") from exc
    if record is None:
        # Deliberately hide whether a UUID belongs to another user.
        raise not_found("Analysis run not found")
    return AnalysisRunView(
        analysis_run_id=record.analysis_run_id,
        status=record.status,
        analysis_case_id=record.analysis_case_id,
        error_code=record.error_code,
        error_message=record.error_message,
    )
