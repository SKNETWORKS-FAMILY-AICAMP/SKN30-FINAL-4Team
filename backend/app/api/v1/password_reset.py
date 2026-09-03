from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.password_reset import (
    InvalidEmailError,
    issue_temporary_password,
    normalize_email,
)


router = APIRouter(prefix="/api/v1/auth", tags=["인증"])


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        try:
            return normalize_email(value)
        except InvalidEmailError as error:
            raise ValueError("Invalid email address") from error


class PasswordResetResponse(BaseModel):
    """화면에 표시할 문구 하나. 상태 코드가 상황을 구분한다."""

    message: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _response(
    http_status: int,
    message: str,
    *,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = PasswordResetResponse(message=message)
    return JSONResponse(
        status_code=http_status,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


@router.post(
    "/password-reset/request",
    summary="비밀번호 재설정 요청",
    description=(
        "가입 이메일로 **임시 비밀번호를 발급해 보낸다.** 화면이 따로 만들 것은 없고, "
        "사용자는 메일에 적힌 임시 비밀번호로 평소처럼 로그인하면 된다. "
        "발송에 성공하면 그 계정의 **기존 비밀번호는 즉시 쓸 수 없게 된다.** "
        "**가입되지 않은 주소여도 성공을 돌려준다.** 이메일을 하나씩 넣어보며 "
        "가입 여부를 알아내는 것을 막기 위해서다. 메일 기능이 설정돼 있지 않을 때도 "
        "성공을 돌려주며 서버 로그에만 경고가 남는다. "
        "그래서 화면은 성공·실패로 분기하지 말고 "
        "`등록된 주소라면 메일이 갑니다` 같은 한 가지 안내만 띄우고 로그인 화면을 유지하면 된다. "
        "임시 비밀번호는 만료되지 않으므로, 로그인한 뒤 비밀번호 변경을 안내하는 편이 좋다."
    ),
    response_model=PasswordResetResponse,
    responses={
        200: {"model": PasswordResetResponse, "description": "발송 요청을 접수했다. 가입 여부와 무관하게 같은 응답이다"},
        400: {"model": PasswordResetResponse, "description": "요청 본문이 형식에 맞지 않다"},
        422: {"model": PasswordResetResponse, "description": "이메일 형식이 아니다"},
        429: {"model": PasswordResetResponse, "description": "요청 제한을 넘었다. Retry-After 헤더에 남은 시간이 있다"},
    },
)
def request_password_reset(
    request: Request,
    body: PasswordResetRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    mail_sender = request.app.state.mail_sender

    limiter = request.app.state.password_reset_limiter
    if not limiter.allow_request(_client_ip(request), body.email):
        return _response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            retry_after=limiter.RETRY_AFTER_SECONDS,
        )

    # 메일 기능이 설정돼 있지 않아도 성공을 돌려준다. 가입 여부를 숨기는
    # 것과 같은 이유로, 메일이 나가지 않았다는 사실도 화면에 드러내지 않는다.
    # 대신 서버 기동 시 로그에 경고를 남긴다.
    if mail_sender is not None:
        background_tasks.add_task(
            issue_temporary_password,
            request.app.state.database_engine,
            mail_sender,
            body.email,
        )
    return _response(
        status.HTTP_200_OK,
        "등록된 이메일이라면 임시 비밀번호가 발송됩니다.",
    )
