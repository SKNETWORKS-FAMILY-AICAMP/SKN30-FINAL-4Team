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
    "/password/reset/request",
    summary="비밀번호 재설정 요청",
    description=(
        "입력한 이메일이 가입된 활성 계정과 일치하면 10분 동안 유효한 "
        "비밀번호 재설정 링크를 발송합니다. 가입 여부 보호를 위해 이메일 일치 여부와 "
        "관계없이 동일한 응답을 반환합니다."
    ),
    response_model=PasswordResetResponse,
    responses={
        200: {"model": PasswordResetResponse, "description": "요청을 접수합니다."},
        400: {"model": PasswordResetResponse, "description": "이메일 입력을 확인해 주세요."},
        429: {"model": PasswordResetResponse, "description": "요청이 많습니다. 잠시 후 다시 시도해 주세요."},
    },
)
def request_password_reset(
    request: Request,
    body: PasswordResetRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    settings = request.app.state.settings
    mail_sender = request.app.state.mail_sender

    limiter = request.app.state.password_reset_limiter
    if not limiter.allow_request(_client_ip(request), body.email):
        return _response(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            retry_after=limiter.RETRY_AFTER_SECONDS,
        )

    # 가입 여부와 메일 설정 여부를 화면에 드러내지 않는다. 운영자는 서버 로그로 확인한다.
    if mail_sender is not None and settings.password_reset_url:
        background_tasks.add_task(
            issue_password_reset_email,
            request.app.state.database_engine,
            mail_sender,
            settings.password_reset_url,
            settings.jwt_secret.get_secret_value(),
            body.email,
        )
    return _response(
        status.HTTP_200_OK,
        "등록된 이메일이라면 비밀번호 재설정 안내가 발송됩니다.",
    )


@router.post(
    "/password/reset/confirm",
    summary="비밀번호 재설정 확인",
    description=(
        "이메일로 받은 링크의 토큰을 확인하고 새 비밀번호로 변경합니다. "
        "링크는 발급 후 10분 동안 유효합니다. 변경이 완료되면 기존 로그인 세션과 "
        "재설정 링크는 사용할 수 없습니다."
    ),
    response_model=PasswordResetResponse,
    responses={
        200: {"model": PasswordResetResponse, "description": "비밀번호를 변경합니다."},
        400: {"model": PasswordResetResponse, "description": "링크 또는 새 비밀번호를 확인해 주세요."},
    },
)
def confirm_password_reset_route(
    request: Request,
    body: PasswordResetConfirmRequest,
) -> JSONResponse:
    try:
        confirm_password_reset(
            request.app.state.database_engine,
            body.token.get_secret_value(),
            body.new_password.get_secret_value(),
            request.app.state.settings.jwt_secret.get_secret_value(),
        )
    except InvalidResetTokenError:
        return _response(
            status.HTTP_400_BAD_REQUEST,
            "비밀번호 재설정 링크가 유효하지 않습니다.",
        )
    except PasswordUnchangedError:
        return _response(
            status.HTTP_400_BAD_REQUEST,
            "새 비밀번호가 기존 비밀번호와 같습니다.",
        )
    return _response(
        status.HTTP_200_OK,
        "비밀번호가 재설정되었습니다.",
    )
