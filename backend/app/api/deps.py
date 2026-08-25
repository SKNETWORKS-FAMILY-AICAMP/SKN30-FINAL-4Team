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
        issued_at = claims["iat"]
        if not isinstance(subject, str) or not subject.isdigit():
            raise ValueError("Invalid token subject")
        if isinstance(issued_at, bool) or not isinstance(issued_at, (int, float)):
            raise ValueError("Invalid token issued-at time")
        user = get_user_by_id(
            request.app.state.database_engine,
            int(subject),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError):
        raise unauthorized() from None

    if (
        user is None
        or not user.is_active
        or float(issued_at) < user.password_changed_at.timestamp()
    ):
        raise unauthorized()

    return user


CurrentUser = Annotated[AppUser, Depends(get_current_user)]
