from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, field_validator

from app.api.envelope import EnvelopeRoute
from app.api.deps import CurrentUser, unauthorized
from app.api.v1.responses import BAD_REQUEST, UNAUTHORIZED
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


router = APIRouter(prefix="/api/v1/auth", tags=["auth"], responses=UNAUTHORIZED, route_class=EnvelopeRoute)


class LoginRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


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
    id: int
    login_id: str


@router.post("/login", response_model=TokenResponse)
def login_user(request: Request, body: LoginRequest) -> TokenResponse:
    try:
        user = login(
            request.app.state.database_engine,
            body.login_id,
            body.password.get_secret_value(),
        )
    except InvalidCredentialsError:
        raise unauthorized() from None

    settings = request.app.state.settings
    return TokenResponse(
        access_token=create_access_token(
            user.id,
            settings.jwt_secret.get_secret_value(),
            settings.jwt_access_token_expire_minutes * 60,
        )
    )


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser) -> MeResponse:
    return MeResponse(id=user.id, login_id=user.login_id)


@router.post("/change-password", responses=BAD_REQUEST)
def update_password(
    request: Request,
    body: ChangePasswordRequest,
    user: CurrentUser,
) -> None:
    try:
        change_password(
            request.app.state.database_engine,
            user.id,
            body.current_password.get_secret_value(),
            body.new_password.get_secret_value(),
        )
    except (InvalidCredentialsError, PasswordUnchangedError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change failed",
        ) from None

    return None


@router.post("/logout")
def logout(_: CurrentUser) -> None:
    """토큰 폐기 목록은 두지 않는다. 프론트가 저장한 토큰을 지운다."""
    return None
