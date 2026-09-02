from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.api.deps import CurrentUser, unauthorized
from app.api.v1.responses import BAD_REQUEST, UNAUTHORIZED, describe
from app.core.password_policy import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    validate_new_password,
)
from app.core.security import create_access_token
from app.services.auth import (
    InvalidCredentialsError,
    PasswordUnchangedError,
    change_password,
    login,
)
from app.services.password_reset import InvalidEmailError, normalize_email


router = APIRouter(prefix="/api/v1/auth", tags=["인증"], responses=UNAUTHORIZED)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        try:
            return normalize_email(value)
        except InvalidEmailError as error:
            raise ValueError("Invalid email address") from error


class TokenResponse(BaseModel):
    """토큰만 반환한다.

    token_type 은 값이 늘 bearer 로 고정이라 정보가 없고, Bearer 사용법은
    OpenAPI 보안 스킴이 이미 문서화한다.
    """

    access_token: str


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
    )

    @field_validator("new_password")
    @classmethod
    def validate_new_password_policy(cls, value: SecretStr) -> SecretStr:
        validate_new_password(value.get_secret_value())
        return value


class MeResponse(BaseModel):
    """화면 사용자 영역에 쓸 이름 하나."""

    name: str


def _issue_token(
    request: Request,
    user_id: int,
    password_changed_at: datetime,
) -> TokenResponse:
    """토큰에 지금의 비밀번호 버전을 함께 담는다.

    인증은 이 값이 DB 의 현재 값과 같은지만 본다. 비밀번호가 바뀌면 값이
    달라져 그 전에 발급된 토큰이 전부 무효가 된다.
    """
    settings = request.app.state.settings
    return TokenResponse(
        access_token=create_access_token(
            user_id,
            settings.jwt_secret.get_secret_value(),
            settings.jwt_access_token_expire_minutes * 60,
            password_changed_at.timestamp(),
        )
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="로그인",
    description=(
        "이메일과 비밀번호로 인증하고 접근 토큰을 발급한다. "
        "사용자 이름은 담지 않으며, 화면에 표시할 이름은 `/auth/me` 에서 받는다. "
        "토큰 수명은 1시간이고 남은 시간은 토큰의 `exp` 클레임에 들어 있다."
    ),
    responses=describe(
        UNAUTHORIZED,
        _401="이메일 또는 비밀번호가 맞지 않다. 계정이 있는지 없는지는 알려주지 않는다",
        _422="이메일 형식이 아니거나 입력이 비었다",
    ),
)
def login_user(request: Request, body: LoginRequest) -> TokenResponse:
    try:
        user = login(
            request.app.state.database_engine,
            body.email,
            body.password.get_secret_value(),
        )
    except InvalidCredentialsError:
        raise unauthorized() from None

    return _issue_token(request, user.id, user.password_changed_at)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="세션 연장",
    description=(
        "아직 만료되지 않은 토큰으로 새 토큰을 받는다. 수명은 발급 시점부터 다시 1시간이다. "
        "요청마다 자동으로 늘어나지 않으며 언제 연장할지는 화면이 정한다. "
        "이미 만료된 뒤에는 연장할 수 없고 재로그인해야 한다."
    ),
    responses=describe(UNAUTHORIZED, _401="토큰이 이미 만료됐거나 잘못됐다. 재로그인이 필요하다"),
)
def refresh_token(request: Request, user: CurrentUser) -> TokenResponse:
    """만료되지 않은 토큰으로만 세션을 연장한다.

    요청마다 자동으로 늘리지 않는다. 연장 시점은 프론트가 정하며, 남은
    시간은 프론트가 토큰의 exp 를 직접 읽어 계산한다.
    """
    return _issue_token(request, user.id, user.password_changed_at)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="로그인 사용자 확인",
    description=(
        "사이드바 사용자 영역에 표시할 이름을 준다. "
        "이름은 계정 이메일의 `@` 앞부분이며 전체 주소는 담지 않는다. "
        "새로고침 후 로그인 상태를 확인할 때도 쓴다."
    ),
)
def me(user: CurrentUser) -> MeResponse:
    """이메일의 @ 앞부분만 이름으로 준다. 전체 주소는 내보내지 않는다."""
    return MeResponse(name=user.email.split("@", 1)[0])


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="비밀번호 변경 (로그인 상태)",
    description=(
        "현재 비밀번호를 확인하고 새 비밀번호로 바꾼다. "
        "**응답의 새 토큰으로 교체하면 이 브라우저는 로그인이 유지되고, "
        "다른 기기에 남아 있던 세션은 끊긴다.** "
        "비밀번호 확인란 일치는 화면에서 검사하며 서버는 새 비밀번호 하나만 받는다."
    ),
    responses=describe(
        {**UNAUTHORIZED, **BAD_REQUEST},
        _400="현재 비밀번호가 맞지 않다. 그 자리에서 다시 입력받는다",
        _422="비밀번호 규칙(영문·숫자·특수문자 8자 이상) 위반이거나 기존 비밀번호와 같다",
    ),
)
def update_password(
    request: Request,
    body: ChangePasswordRequest,
    user: CurrentUser,
) -> TokenResponse:
    """변경 후에도 이 브라우저의 로그인을 유지한다.

    비밀번호가 바뀌면 DB 트리거가 password_changed_at 을 갱신하고 인증이
    그보다 먼저 발급된 토큰을 거부한다. 다른 기기의 세션을 끊는 이 성질은
    그대로 두고, 변경을 요청한 쪽에만 새 토큰을 돌려준다.
    """
    try:
        changed_at = change_password(
            request.app.state.database_engine,
            user.id,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password does not match",
        ) from None
    except PasswordUnchangedError:
        # 입력을 고치면 되는 문제라 현재 비밀번호 불일치와 구분한다.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="New password must differ from the current password",
        ) from None

    # 앱 시계가 DB 보다 뒤져도 방금 만든 토큰이 거부되지 않도록 DB 시각을 쓴다.
    return _issue_token(request, user.id, changed_at)
