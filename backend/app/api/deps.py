from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Path, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.core.security import decode_access_token
from app.core.supabase_auth import SupabaseTokenError, looks_like_supabase_token
from app.services.auth import AppUser, get_user_by_id, get_user_by_supabase_id
from app.services.case_identity import CaseNotFoundError, resolve_internal_case_id


_bearer = HTTPBearer(auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_from_supabase_token(request: Request, token: str) -> AppUser:
    settings = request.app.state.settings
    verifier = settings.supabase_token_verifier
    if not verifier.enabled:
        raise unauthorized()

    try:
        claims = verifier.verify(token)
    except SupabaseTokenError:
        raise unauthorized() from None

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise unauthorized()
    try:
        UUID(subject)
    except ValueError:
        raise unauthorized() from None

    email = claims.get("email")
    user = get_user_by_supabase_id(
        request.app.state.database_engine,
        subject,
        email=email if isinstance(email, str) else None,
        link_by_email=settings.supabase_link_existing_user_by_email,
    )
    if user is None or not user.is_active:
        # Supabase 에는 있는데 내부 사용자가 없다. 토큰은 멀쩡하지만 이 서비스의
        # 사용자가 아니라, 인증 실패로 안내한다.
        raise unauthorized()
    return user


def _user_from_internal_token(request: Request, token: str) -> AppUser:
    settings = request.app.state.settings
    if not settings.allow_internal_jwt:
        raise unauthorized()

    try:
        claims = decode_access_token(
            token,
            settings.jwt_secret.get_secret_value(),
        )
        subject = claims["sub"]
        password_version = claims["pwd"]
        if not isinstance(subject, str) or not subject.isdigit():
            raise ValueError("Invalid token subject")
        if isinstance(password_version, bool) or not isinstance(
            password_version, (int, float)
        ):
            raise ValueError("Invalid token password version")
        user = get_user_by_id(
            request.app.state.database_engine,
            int(subject),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        raise unauthorized() from None

    # 발급 당시의 비밀번호 버전과 지금 값을 비교한다. 앞뒤를 재지 않으므로
    # 앱과 DB 의 시계 차이가 끼어들지 않는다. 비밀번호가 바뀌면 값이 달라져
    # 그 전에 발급된 토큰이 전부 무효가 된다.
    if (
        user is None
        or not user.is_active
        or float(password_version) != user.password_changed_at.timestamp()
    ):
        raise unauthorized()

    return user


def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
) -> AppUser:
    """Supabase Access Token 과 자체 발급 JWT 를 모두 받는다.

    어느 쪽으로 검증할지는 토큰의 `iss` 로 고른다. **고르기만** 하고 서명은
    각자 확인하므로, iss 를 위조해도 통과할 수 있는 것은 없다. 자체 발급
    토큰은 Supabase 하나로 확정되면 `ALLOW_INTERNAL_JWT=false` 로 끈다.
    """
    if credentials is None:
        raise unauthorized()

    token = credentials.credentials
    if looks_like_supabase_token(token):
        return _user_from_supabase_token(request, token)
    return _user_from_internal_token(request, token)


CurrentUser = Annotated[AppUser, Depends(get_current_user)]


def get_case_id(
    request: Request,
    user: CurrentUser,
    analysis_case_id: Annotated[
        UUID,
        Path(description="분석 건 식별자입니다."),
    ],
) -> int:
    """외부 UUID → 내부 bigint PK.

    프론트는 내부 PK 를 모르고 UUID 만 쓴다. 여기서 한 번 바꾸면 서비스 계층은
    지금처럼 bigint 로 계속 일한다.

    없는 건과 남의 건을 **같은 404** 로 돌려준다. 403 으로 구분하면 남의 분석
    건이 존재한다는 사실이 새어 나간다.
    """
    try:
        return resolve_internal_case_id(
            request.app.state.database_engine,
            user.id,
            analysis_case_id,
        )
    except CaseNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


CaseId = Annotated[int, Depends(get_case_id)]
