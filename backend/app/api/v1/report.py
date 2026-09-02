from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    CONFLICT,
    describe,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    UNAUTHORIZED,
)
from app.schemas.chat import ChatMessagesResponse
from app.schemas.report import AnalysisReport, ReportCaseDisplay
from app.services.chat import get_chat_history
from app.services.reporting import (
    ReportFileUnavailableError,
    ReportNotFoundError,
    ReportNotReadyError,
    get_report,
    open_report_file,
)


router = APIRouter(prefix="/api/v1/cases", tags=["분석"], responses=UNAUTHORIZED)


class CaseDetailResponse(BaseModel):
    """분석 결과 화면과 과거 이력 상세가 함께 쓰는 응답.

    보고서와 대화를 한 번에 담아 화면을 열 때 요청을 한 번만 보낸다. PDF 는
    경로가 고정이라 링크를 담지 않고, 완료된 건은 PDF 가 항상 존재한다.
    """

    case: ReportCaseDisplay
    report: AnalysisReport
    chat: ChatMessagesResponse


@router.get(
    "/{case_id}",
    response_model=CaseDetailResponse,
    response_model_exclude_none=True,
    summary="분석 상세 조회",
    description=(
        "분석 결과 화면과 과거 이력 상세가 함께 쓴다. "
        "보고서와 **최근 대화 20개**를 한 번에 준다. 더 이전 대화는 대화 조회 API 로 "
        "`chat.next_cursor` 를 넣어 가져온다. "
        "PDF 링크는 담지 않는다. 경로가 고정이고 완료된 건은 PDF 가 항상 존재한다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **NOT_FOUND, **CONFLICT},
        _409="아직 분석이 끝나지 않았다",
    ),
)
def case_detail(
    case_id: int,
    request: Request,
    user: CurrentUser,
) -> CaseDetailResponse:
    engine = request.app.state.database_engine
    try:
        result = get_report(engine, user.id, case_id)
        chat = get_chat_history(engine, user.id, case_id)
    except ReportNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ReportNotReadyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error) or "Report is not ready",
        ) from None
    return CaseDetailResponse(
        case=result.case,
        report=result.report,
        chat=chat,
    )


@router.get(
    "/{case_id}/report",
    # 성공만 PDF 바이너리다. 기본값대로 두면 OpenAPI 가 JSON 이라고 말한다.
    response_class=StreamingResponse,
    summary="보고서 PDF 다운로드",
    description=(
        "분석 보고서를 PDF 로 받는다. `Content-Disposition` 헤더의 파일명을 쓰면 된다. "
        "다른 API 와 같이 `Authorization` 헤더가 필요하므로 링크를 그대로 여는 방식으로는 "
        "받을 수 없다."
    ),
    responses=describe(
        {
            200: {
                "content": {"application/pdf": {}},
                "description": "보고서 PDF 파일",
            },
            **UNAUTHORIZED,
            **NOT_FOUND,
            **CONFLICT,
            **SERVICE_UNAVAILABLE,
        },
        _409="아직 분석이 끝나지 않아 PDF 가 없다",
        _503="파일 저장소에서 PDF 를 읽지 못했다",
    ),
)
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
        # 저장소 경로나 원인 예외를 노출하지 않는 고정 문구를 쓴다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Report PDF is temporarily unavailable",
        ) from None

    disposition = f"attachment; filename*=UTF-8''{quote(result.filename)}"
    return StreamingResponse(
        result.content,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
        background=BackgroundTask(result.content.close),
    )
