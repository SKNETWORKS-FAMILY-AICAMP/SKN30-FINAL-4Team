from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.core.security import decode_access_token
from app.services.auth import AppUser, get_user_by_id


_bearer = HTTPBearer(auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer),
    ],
) -> AppUser:
    if credentials is None:
        raise unauthorized()

    try:
        claims = decode_access_token(
            credentials.credentials,
            request.app.state.settings.jwt_secret.get_secret_value(),
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


CurrentUser = Annotated[AppUser, Depends(get_current_user)]
