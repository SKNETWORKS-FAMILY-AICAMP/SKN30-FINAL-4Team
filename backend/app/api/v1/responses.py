"""OpenAPI 오류 응답 선언.

FastAPI 는 라우트가 던지는 HTTPException 을 자동으로 문서화하지 않는다.
그대로 두면 명세에 성공 코드와 422 만 남아 프론트가 오류를 알 수 없다.
라우트가 실제로 낼 수 있는 것만 골라 아래 상수를 합쳐 쓴다.

코드는 의미로 정해진다. 같은 의미면 어느 경로에서나 같은 코드다.
"""

from typing import Any

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """HTTPException 이 만드는 본문.

    검증 오류(422)만 detail 이 배열이고 나머지는 문자열이다.
    클라이언트는 detail 의 타입을 확인해야 한다.
    """

    detail: str


def _error(status_code: int, description: str) -> dict[int | str, dict[str, Any]]:
    return {status_code: {"model": ErrorResponse, "description": description}}


# 인증이 필요한 전 경로. 로그인 자체도 자격 증명이 틀리면 이 코드를 쓴다.
UNAUTHORIZED = _error(401, "토큰이 없거나 만료됐거나 잘못됐다")

# 존재하지 않는 건과 남의 건을 구분하지 않는다. 구분하면 해당 번호가
# 존재한다는 사실이 새기 때문이다.
NOT_FOUND = _error(404, "분석 건이 없거나 요청자의 것이 아니다")

# 분석이 끝나기 전에 결과를 요청했거나, 이미 진행 중인 건을 다시 요청했다.
CONFLICT = _error(409, "아직 준비되지 않았거나 이미 진행 중이다")

BAD_REQUEST = _error(400, "요청 본문이 형식에 맞지 않다")
PAYLOAD_TOO_LARGE = _error(413, "업로드 파일이 허용 크기를 넘는다")
UNSUPPORTED_MEDIA_TYPE = _error(415, "HWP 또는 HWPX 가 아니다")
BAD_GATEWAY = _error(502, "LLM 이 사용할 수 없는 응답을 돌려줬다")
SERVICE_UNAVAILABLE = _error(503, "외부 서비스를 사용할 수 없다")
