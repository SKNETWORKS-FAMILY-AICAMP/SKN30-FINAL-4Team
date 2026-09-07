"""포트 호출 한 번을 감싸는 공용 래퍼.

fit.py 와 sim_inputs.py 가 같은 오류 격리 계약을 쓴다. 사본이 두 벌 있으면
한쪽만 고쳐졌을 때 같은 실패가 슬라이스마다 다르게 분류되므로 여기 한 곳에
둔다. ``_TRANSPORT_REASONS`` 의 reason code 매핑은 호출부가 각자 갖는다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel

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
