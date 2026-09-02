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


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


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
    response_model=PasswordResetResponse,
    responses={
        400: {"model": PasswordResetResponse},
        429: {"model": PasswordResetResponse},
        503: {"model": PasswordResetResponse},
        422: {"model": PasswordResetResponse},
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
    response_model=PasswordResetResponse,
    responses={
        400: {"model": PasswordResetResponse},
        429: {"model": PasswordResetResponse},
        422: {"model": PasswordResetResponse},
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
    except (InvalidResetTokenError, PasswordUnchangedError):
        return _response(
            status.HTTP_400_BAD_REQUEST,
            "비밀번호 재설정 링크가 유효하지 않거나 새 비밀번호가 기존 비밀번호와 같습니다.",
        )
    return _response(
        status.HTTP_200_OK,
        "비밀번호가 재설정되었습니다.",
    )
