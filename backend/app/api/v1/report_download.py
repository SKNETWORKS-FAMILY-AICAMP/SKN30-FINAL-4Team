"""프론트 명세 §11 PDF 다운로드.

    POST /functions/v1/edge-report-create-download-url  -> { signed_url, ... }
    GET  <signed_url>                                   -> PDF 바이트

두 걸음인 이유가 있다. 화면은 받은 URL 로 ``window.location.href`` 이동을
하는데, 그 이동에는 ``Authorization`` 헤더가 붙지 않는다. 그래서 URL 자체가
자격증명이어야 한다 — Supabase 서명 URL 이 하는 일이 그것이고, 여기서는 짧게
사는 토큰이 같은 역할을 한다.

**소유권은 발급 시점에만 본다.** 토큰에 사용자를 담지 않는 이유이자 수명을
1분으로 두는 이유다. 상태·만료는 다운로드 때 다시 본다 — 발급 뒤 보고서가
만료되거나 다시 생성 중이 될 수 있다.

Supabase Storage 가 서면 발급 쪽이 ``createSignedUrl()`` 결과를 그대로
돌려주고 이 파일의 GET 은 사라진다. 응답 모양은 그때도 같다.
"""

from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from jwt import InvalidTokenError
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.api.deps import CurrentUser
from app.api.v1.responses import (
    CONFLICT,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    UNAUTHORIZED,
    describe,
)
from app.core.security import (
    create_report_download_token,
    decode_report_download_token,
)
from app.services.reporting import (
    ReportFileUnavailableError,
    ReportNotFoundError,
    ReportNotReadyError,
    authorize_report_download,
    open_report_artifact,
)


router = APIRouter(tags=["분석"])

_DOWNLOAD_ROUTE = "report_download"


class CreateDownloadUrlRequest(BaseModel):
    analysis_case_id: UUID


class CreateDownloadUrlResponse(BaseModel):
    signed_url: str
    expires_in_seconds: int


@router.post(
    "/functions/v1/edge-report-create-download-url",
    response_model=CreateDownloadUrlResponse,
    summary="보고서 PDF 다운로드 URL 발급",
    description=(
        "짧게 유효한 다운로드 URL을 발급합니다. 결과 조회 응답의 "
        "`report.can_download`가 참일 때만 호출합니다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **NOT_FOUND, **CONFLICT},
        _404="분석 건이 없거나 요청자의 것이 아닙니다.",
        _409="보고서가 아직 준비되지 않았거나 보관 기간이 지났습니다.",
    ),
)
def edge_report_create_download_url(
    request: Request,
    user: CurrentUser,
    body: CreateDownloadUrlRequest,
) -> CreateDownloadUrlResponse:
    settings = request.app.state.settings
    try:
        authorize_report_download(
            request.app.state.database_engine, user.id, body.analysis_case_id
        )
    except ReportNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ReportNotReadyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Report PDF is not ready",
        ) from None

    ttl = settings.report_download_ttl_seconds
    token = create_report_download_token(
        str(body.analysis_case_id),
        settings.jwt_secret.get_secret_value(),
        ttl,
    )
    # 절대 URL 은 요청에서 만든다. 배포 주소를 설정으로 또 들고 있으면 둘이
    # 어긋난다. 리버스 프록시 뒤에서는 forwarded 헤더 처리가 전제다.
    signed_url = str(request.url_for(_DOWNLOAD_ROUTE).include_query_params(token=token))
    return CreateDownloadUrlResponse(signed_url=signed_url, expires_in_seconds=ttl)


@router.get(
    "/api/v1/reports/download",
    name=_DOWNLOAD_ROUTE,
    summary="보고서 PDF 내려받기 (서명 URL)",
    description=(
        "발급받은 다운로드 URL로 PDF를 내려받습니다. URL 자체가 자격증명이므로 "
        "별도의 인증 헤더가 필요하지 않으며, 짧은 시간만 유효합니다."
    ),
    response_class=StreamingResponse,
    responses=describe(
        {**NOT_FOUND, **CONFLICT, **SERVICE_UNAVAILABLE},
        _404="링크가 유효하지 않거나 만료되었습니다.",
        _409="보고서가 아직 준비되지 않았거나 보관 기간이 지났습니다.",
        _503="파일 서비스를 일시적으로 사용할 수 없습니다.",
    ),
)
async def report_download(
    request: Request,
    token: str = Query(description="발급받은 다운로드 토큰입니다."),
) -> StreamingResponse:
    try:
        analysis_case_id = UUID(
            decode_report_download_token(
                token, request.app.state.settings.jwt_secret.get_secret_value()
            )
        )
    except (InvalidTokenError, ValueError):
        # 만료·위조·다른 용도의 토큰을 구분해 알려주지 않는다.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    try:
        result = await open_report_artifact(
            request.app.state.database_engine,
            request.app.state.object_storage,
            analysis_case_id,
        )
    except ReportNotReadyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Report PDF is not ready",
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
