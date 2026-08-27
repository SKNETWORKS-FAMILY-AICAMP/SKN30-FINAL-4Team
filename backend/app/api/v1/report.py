from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.deps import CurrentUser
from app.schemas.report import ReportResponse
from app.services.reporting import (
    ReportFileUnavailableError,
    ReportNotFoundError,
    ReportNotReadyError,
    get_report,
    open_report_file,
)


router = APIRouter(prefix="/api/v1/cases", tags=["reports"])


@router.get("/{case_id}/report", response_model=ReportResponse)
def report_result(case_id: int, request: Request, user: CurrentUser) -> ReportResponse:
    try:
        return get_report(request.app.state.database_engine, user.id, case_id)
    except ReportNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ReportNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error) or "Report is not ready",
        ) from None


@router.get("/{case_id}/report.pdf")
async def report_pdf(
    case_id: int,
    request: Request,
    user: CurrentUser,
) -> StreamingResponse:
    try:
        result = await open_report_file(
            request.app.state.database_engine,
            request.app.state.object_storage,
            user.id,
            case_id,
        )
    except ReportNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ReportNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error) or "Report PDF is not ready",
        ) from None
    except ReportFileUnavailableError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from None

    disposition = f"attachment; filename*=UTF-8''{quote(result.filename)}"
    return StreamingResponse(
        result.content,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
        background=BackgroundTask(result.content.close),
    )
