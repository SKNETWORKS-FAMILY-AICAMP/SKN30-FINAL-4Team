"""OpenAPI 오류 응답 선언.

FastAPI 는 라우트가 던지는 HTTPException 을 자동으로 문서화하지 않습니다.
라우트가 실제로 낼 수 있는 것만 골라 아래 상수를 합쳐 씁니다.

코드는 의미로 정합니다. 같은 의미면 어느 경로에서나 같은 코드입니다.
"""

from typing import Any

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """실패 응답 본문.

    화면에 표시할 문구 하나뿐이다. 내부 오류 코드·스택·필드별 검증 세부는
    담지 않는다. 문구의 주인은 프론트이며 여기 message 는 폴백이다.
    프론트는 상태 코드로 화면 동작을 정한다.
    """

    message: str


def _error(status_code: int, description: str) -> dict[int | str, dict[str, Any]]:
    return {status_code: {"model": ErrorResponse, "description": description}}


def describe(
    responses: dict[int | str, dict[str, Any]],
    **overrides: str,
) -> dict[int | str, dict[str, Any]]:
    """공통 오류 선언에 라우트별 설명을 덧씌운다.

    같은 상태 코드라도 화면이 해야 할 일이 다르면 그것을 적는다.
    예를 들어 재설정 확인의 400 은 링크 문제라 재요청으로 보내야 하고,
    비밀번호 변경의 400 은 현재 비밀번호 오류라 그 자리에서 다시 받는다.
    """
    merged = {code: dict(value) for code, value in responses.items()}
    for code, description in overrides.items():
        key = int(code.removeprefix("_"))
        # 성공 코드에 오류 모델을 붙이면 스웨거가 본문을 {message} 로 보여준다.
        # 그 코드의 본문은 라우트의 response_model 이 정한다.
        merged.setdefault(key, {} if key < 400 else {"model": ErrorResponse})
        merged[key] = {**merged[key], "description": description}
    return merged


# 인증이 필요한 전 경로. 로그인 자체도 자격 증명이 틀리면 이 코드를 씁니다.
UNAUTHORIZED = _error(401, "인증 정보를 확인해 주세요.")

# 존재하지 않는 건과 남의 건을 구분하지 않는다. 구분하면 해당 번호가
# 존재한다는 사실이 새기 때문이다.
NOT_FOUND = _error(404, "분석 건이 없거나 요청자의 것이 아닙니다.")

# 분석이 끝나기 전에 결과를 요청했거나, 이미 진행 중인 건을 다시 요청했다.
CONFLICT = _error(409, "아직 준비되지 않았거나 이미 진행 중입니다.")

BAD_REQUEST = _error(400, "입력값을 확인해 주세요.")
PAYLOAD_TOO_LARGE = _error(413, "업로드 파일이 허용 크기를 초과합니다.")
UNSUPPORTED_MEDIA_TYPE = _error(415, "HWP 또는 HWPX 파일을 사용해 주세요.")
SERVICE_UNAVAILABLE = _error(503, "외부 서비스를 사용할 수 없습니다.")
