from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from app.models.pipeline import PipelineKind
from app.pipelines.formats import FormatError, validate_format

from ..auth import AdminDep, PrincipalDep
from ..envelope import ApiEnvelope, ok
from ..models import AnalysisResult, EvidenceList, ExistingIngestionCreated, ProcessingState, RequestCaseCreated, RequestCaseSummary
from ..services import AnalysisDispatcher, CaseRepository

router = APIRouter(tags=["PreReview"])

REQUEST_EXTENSIONS = {".hwp"}
EXISTING_EXTENSIONS = {".hwp", ".hwpx", ".pdf"}


def repository(request: Request) -> CaseRepository:
    return request.app.state.case_repository


def dispatcher(request: Request) -> AnalysisDispatcher:
    return request.app.state.analysis_dispatcher


RepositoryDep = Annotated[CaseRepository, Depends(repository)]
DispatcherDep = Annotated[AnalysisDispatcher, Depends(dispatcher)]


def _validate_upload(upload: UploadFile, allowed: set[str], label: str) -> str:
    filename = upload.filename or ""
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in allowed:
        formats = ", ".join(sorted(allowed))
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"{label} accepts: {formats}")
    return filename


async def _content(request: Request, upload: UploadFile, kind: PipelineKind) -> bytes:
    """Read multipart uploads in bounded chunks before validating their magic.

    ``UploadFile.read()`` without a size limit can exhaust worker memory.  The
    API still keeps the accepted payload in memory only because the offline
    repository is intentionally a development scaffold; a storage adapter can
    stream these same chunks directly to object storage.
    """

    maximum = int(getattr(request.app.state, "upload_max_bytes", 25 * 1024 * 1024))
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > maximum:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"Uploaded file exceeds the {maximum}-byte limit")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Uploaded file is empty")
    try:
        validate_format(kind, upload.filename or "", content=payload)
    except FormatError as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    return payload


async def _case_or_404(repo: CaseRepository, owner_id: str, case_id: str) -> RequestCaseSummary:
    item = await repo.get_request(owner_id=owner_id, case_id=case_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request case not found")
    return item


@router.post("/requests", response_model=ApiEnvelope[RequestCaseCreated], status_code=status.HTTP_202_ACCEPTED, summary="Upload an HWP request document")
async def create_request_case(request: Request, file: Annotated[UploadFile, File(...)], principal: PrincipalDep, repo: RepositoryDep, jobs: DispatcherDep) -> ApiEnvelope[RequestCaseCreated]:
    filename = _validate_upload(file, REQUEST_EXTENSIONS, "Request upload")
    record = await repo.create_request(owner_id=principal.user_id, filename=filename, content_type=file.content_type, content=await _content(request, file, PipelineKind.REQUEST))
    await jobs.enqueue_request(record.id)
    return ok(record, message="Request accepted for processing")


@router.get("/requests", response_model=ApiEnvelope[list[RequestCaseSummary]], summary="List request cases")
async def list_request_cases(principal: PrincipalDep, repo: RepositoryDep) -> ApiEnvelope[list[RequestCaseSummary]]:
    return ok(await repo.list_requests(owner_id=principal.user_id))


@router.get("/requests/{case_id}", response_model=ApiEnvelope[RequestCaseSummary], summary="Get a request case")
async def get_request_case(case_id: str, principal: PrincipalDep, repo: RepositoryDep) -> ApiEnvelope[RequestCaseSummary]:
    return ok(await _case_or_404(repo, principal.user_id, case_id))


@router.get("/requests/{case_id}/status", response_model=ApiEnvelope[ProcessingState], summary="Get processing status")
async def get_request_status(case_id: str, principal: PrincipalDep, repo: RepositoryDep) -> ApiEnvelope[ProcessingState]:
    await _case_or_404(repo, principal.user_id, case_id)
    item = await repo.get_status(owner_id=principal.user_id, case_id=case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Processing state not found")
    return ok(item)


@router.get("/requests/{case_id}/result", response_model=ApiEnvelope[AnalysisResult], summary="Get structured request and comparison result")
async def get_request_result(case_id: str, principal: PrincipalDep, repo: RepositoryDep) -> ApiEnvelope[AnalysisResult]:
    await _case_or_404(repo, principal.user_id, case_id)
    item = await repo.get_result(owner_id=principal.user_id, case_id=case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Analysis result not found")
    return ok(item)


@router.get("/requests/{case_id}/evidence", response_model=ApiEnvelope[EvidenceList], summary="Get evidence references for a result")
async def get_request_evidence(case_id: str, principal: PrincipalDep, repo: RepositoryDep) -> ApiEnvelope[EvidenceList]:
    await _case_or_404(repo, principal.user_id, case_id)
    item = await repo.get_evidence(owner_id=principal.user_id, case_id=case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return ok(item)


@router.post("/admin/existing/ingestions", response_model=ApiEnvelope[ExistingIngestionCreated], status_code=status.HTTP_202_ACCEPTED, summary="Queue an existing-program source document")
async def create_existing_ingestion(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    notice_id: Annotated[str, Form(...)],
    source_profile_id: Annotated[str, Form(...)],
    principal: AdminDep,
    repo: RepositoryDep,
    jobs: DispatcherDep,
) -> ApiEnvelope[ExistingIngestionCreated]:
    filename = _validate_upload(file, EXISTING_EXTENSIONS, "Existing ingestion")
    if not notice_id.strip() or not source_profile_id.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="notice_id and source_profile_id are required")
    record = await repo.create_existing_ingestion(owner_id=principal.user_id, filename=filename, content_type=file.content_type, content=await _content(request, file, PipelineKind.EXISTING), notice_id=notice_id.strip(), source_profile_id=source_profile_id.strip())
    await jobs.enqueue_existing(record.ingestion_id)
    return ok(record, message="Existing document accepted for ingestion")


@router.get("/admin/existing/ingestions", response_model=ApiEnvelope[list[ExistingIngestionCreated]], summary="List existing-program ingestions")
async def list_existing_ingestions(_: AdminDep, repo: RepositoryDep) -> ApiEnvelope[list[ExistingIngestionCreated]]:
    return ok(await repo.list_existing_ingestions())


@router.get("/admin/existing/ingestions/{ingestion_id}/status", response_model=ApiEnvelope[ProcessingState], summary="Get existing-program ingestion status")
async def get_existing_ingestion_status(ingestion_id: str, _: AdminDep, repo: RepositoryDep) -> ApiEnvelope[ProcessingState]:
    item = await repo.get_existing_status(ingestion_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Existing ingestion not found")
    return ok(item)

# Compatibility aliases for the current frontend contract.  Keep these thin;
# new clients should use /requests, whose name reflects the domain model.
router.add_api_route("/cases", create_request_case, methods=["POST"], response_model=ApiEnvelope[RequestCaseCreated], status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
router.add_api_route("/cases", list_request_cases, methods=["GET"], response_model=ApiEnvelope[list[RequestCaseSummary]], include_in_schema=False)
router.add_api_route("/cases/{case_id}/status", get_request_status, methods=["GET"], response_model=ApiEnvelope[ProcessingState], include_in_schema=False)
router.add_api_route("/cases/{case_id}/report", get_request_result, methods=["GET"], response_model=ApiEnvelope[AnalysisResult], include_in_schema=False)
