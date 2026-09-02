from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.password_policy import validate_new_password
from app.services.password_reset import (
    InvalidEmailError,
    InvalidResetTokenError,
    PasswordUnchangedError,
    confirm_password_reset,
    issue_password_reset_email,
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


class PasswordResetConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(min_length=1, max_length=4096)
    new_password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        validate_new_password(value.get_secret_value())
        return value


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
        "가입 이메일로 재설정 링크를 보낸다. "
        "**가입되지 않은 주소여도 성공을 돌려준다.** 이메일을 하나씩 넣어보며 "
        "가입 여부를 알아내는 것을 막기 위해서다. 메일 기능이 설정돼 있지 않을 때도 "
        "성공을 돌려주며 서버 로그에만 경고가 남는다. "
        "화면은 성공이면 안내 후 로그인으로 보내고, 그 외에는 같은 문구로 화면을 유지하면 된다. "
        "링크는 `/password-reset/confirm#token=...` 형식이고 10분 뒤 만료되며 한 번만 쓸 수 있다."
    ),
    response_model=PasswordResetResponse,
    responses={
        200: {"model": PasswordResetResponse, "description": "발송 요청을 접수했다. 가입 여부와 무관하게 같은 응답이다"},
        400: {"model": PasswordResetResponse, "description": "요청 본문이 형식에 맞지 않다"},
        422: {"model": PasswordResetResponse, "description": "이메일 형식이 아니다"},
        429: {"model": PasswordResetResponse, "description": "요청 제한을 넘었다. Retry-After 헤더에 남은 시간이 있다"},
        503: {"model": PasswordResetResponse, "description": "메일 서비스를 쓸 수 없다"},
    },
)
def request_password_reset(
    request: Request,
    body: PasswordResetRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    settings = request.app.state.settings
    mail_sender = request.app.state.mail_sender
    password_reset_url = settings.password_reset_url
    if mail_sender is None or not password_reset_url:
        return _response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "비밀번호 재설정 메일 서비스를 사용할 수 없습니다.",
        )

    limiter = request.app.state.password_reset_limiter
    if not limiter.allow_request(_client_ip(request), body.email):
        return _response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            retry_after=limiter.RETRY_AFTER_SECONDS,
        )

    background_tasks.add_task(
        issue_password_reset_email,
        request.app.state.database_engine,
        mail_sender,
        str(password_reset_url),
        settings.jwt_secret.get_secret_value(),
        body.email,
    )
    return _response(
        status.HTTP_200_OK,
        "등록된 이메일이라면 비밀번호 재설정 안내가 발송됩니다.",
    )


@router.post(
    "/password-reset/confirm",
    summary="비밀번호 재설정 확인",
    description=(
        "메일 링크의 토큰과 새 비밀번호로 변경한다. "
        "**실패는 두 갈래로 나뉘고 화면이 해야 할 일이 정반대다.** "
        "`400` 은 링크 문제라 재설정 요청 화면으로 보내 메일을 다시 받게 해야 하고, "
        "`422` 는 입력 문제라 그 자리에서 다시 입력받으면 된다. "
        "성공하면 그 사용자의 기존 접근 토큰이 모두 무효화되므로 다시 로그인해야 한다."
    ),
    response_model=PasswordResetResponse,
    responses={
        200: {"model": PasswordResetResponse, "description": "변경했다. 기존 접근 토큰은 모두 무효가 된다"},
        400: {"model": PasswordResetResponse, "description": "링크가 만료됐거나 이미 사용됐거나 잘못됐다. 재설정을 다시 요청해야 한다"},
        422: {"model": PasswordResetResponse, "description": "비밀번호 규칙 위반이거나 기존 비밀번호와 같다. 그 자리에서 다시 입력받는다"},
        429: {"model": PasswordResetResponse, "description": "요청 제한을 넘었다. Retry-After 헤더에 남은 시간이 있다"},
    },
)
def confirm_password_reset_route(
    request: Request,
    body: PasswordResetConfirmRequest,
) -> JSONResponse:
    limiter = request.app.state.password_reset_limiter
    if not limiter.allow_confirm(_client_ip(request)):
        return _response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            retry_after=limiter.RETRY_AFTER_SECONDS,
        )

    try:
        confirm_password_reset(
            request.app.state.database_engine,
            body.token.get_secret_value(),
            body.new_password.get_secret_value(),
            request.app.state.settings.jwt_secret.get_secret_value(),
        )
    except InvalidResetTokenError:
        # 링크 문제. 사용자는 재설정을 다시 요청해야 한다.
        return _response(
            status.HTTP_400_BAD_REQUEST,
            "비밀번호 재설정 링크가 유효하지 않습니다.",
        )
    except PasswordUnchangedError:
        # 입력 문제. 사용자는 그 자리에서 다시 입력하면 된다.
        return _response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "새 비밀번호가 기존 비밀번호와 같습니다.",
        )
    return _response(
        status.HTTP_200_OK,
        "비밀번호가 재설정되었습니다.",
    )
