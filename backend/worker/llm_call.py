"""포트 호출 한 번을 감싸는 공용 래퍼.

fit.py 와 sim_inputs.py 가 같은 오류 격리 계약을 쓴다. 사본이 두 벌 있으면
한쪽만 고쳐졌을 때 같은 실패가 슬라이스마다 다르게 분류되므로 여기 한 곳에
둔다. ``_TRANSPORT_REASONS`` 의 reason code 매핑은 호출부가 각자 갖는다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ports.llm_client import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    Message,
)
from app.ports.llm_client import LLMClient


# 포트가 약속한 세 예외. 호출부가 관계·축별로 격리한다.
PORT_ERRORS: tuple[type[Exception], ...] = (
    LLMTimeoutError,
    LLMUnavailableError,
    LLMInvalidResponseError,
)


def generate(
    llm_client: LLMClient,
    *,
    task_name: str,
    instructions: str,
    payload: dict[str, Any],
    response_schema: type[BaseModel],
    model_profile: str,
) -> BaseModel:
    """ponytail: 워커가 동기라 여기서 asyncio.run 으로 끊는다.

    워커를 async 루프 안에서 돌리게 되면 이 한 곳만 스레드로 뺀다.

    포트 계약 밖의 예외는 ``LLMUnavailableError`` 로 옮긴다. 원인을 안다고
    주장하지 않되, 여기서 새면 LLM 을 타지 않은 Rule 결과와 이미 확정된
    관계·축까지 함께 사라진다 (초안 §9.4 정상 결과 보존).
    """

    try:
        response = asyncio.run(
            llm_client.generate_structured(
                task_name=task_name,
                messages=[
                    Message(role="system", content=instructions),
                    Message(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                response_schema=response_schema,
                model_profile=model_profile,
            )
        )
    except PORT_ERRORS:
        raise
    except Exception as error:
        raise LLMUnavailableError(
            f"{task_name}: 포트 계약 밖 예외 {type(error).__name__}: {error}"
        ) from error
    if not isinstance(response, response_schema):
        raise LLMInvalidResponseError(
            f"{task_name} returned {type(response).__name__}, expected {response_schema.__name__}"
        )
    return response


def salvage_rows(
    error: Exception,
    *,
    envelope: str,
    row_model: type[BaseModel],
    id_field: str,
) -> tuple[list[BaseModel], list[str], int] | None:
    """스키마를 어긴 배치 응답에서 살아 있는 행만 건져 낸다 (초안 §9.2.1).

    포트가 배치 전체를 한 번에 검증하기 때문에, 행 하나가 계약을 어기면
    호출부의 항목별 격리가 아예 돌지 못한다. 여기서 그 격리를 되살린다.

    ``None`` 이면 부분 회수를 하지 않는다 = 배치 전체 실패다. 조건은 둘이다.
    본문이 JSON 이 아니었거나(``raw is None``), 엔벨로프 키가 없거나 목록이
    아닌 경우다.

    돌려주는 것은 (행 모델 검증을 통과한 행, id 는 알아냈지만 행이 계약을
    어긴 id 목록, id 조차 알 수 없어 버린 행 수) 다. 호출부는 두 번째를 그
    단위만의 실패로, 세 번째를 경고로 남긴다.

    **``error.raw`` 는 여기서 값으로만 쓴다.** 요청서 원문이 실릴 수 있으므로
    메시지·로그로 흘리지 않는다.
    """

    raw = getattr(error, "raw", None)
    if not isinstance(raw, dict):
        return None
    rows = raw.get(envelope)
    if not isinstance(rows, list):
        return None

    parsed: list[BaseModel] = []
    broken: list[str] = []
    dropped = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(id_field), str):
            dropped += 1
            continue
        try:
            parsed.append(row_model.model_validate(row))
        except ValidationError:
            broken.append(row[id_field])
    return parsed, broken, dropped
