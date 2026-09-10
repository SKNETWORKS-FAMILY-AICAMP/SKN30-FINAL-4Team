"""나가는 JSON Schema 가 Responses API 의 ``strict`` 규칙을 지키는지 본다.

실측 근거: 벤더 스키마를 손대지 않고 보내면 OpenAI 가 400
``invalid_json_schema`` 로 거절한다 ("required ... Missing 'canonical_action'").
LLM 에 닿지 못하므로 보완 루프도 소용이 없다.
"""

import asyncio
import json
from typing import Any

import httpx
from pydantic import BaseModel

from app.infrastructure.openai_llm_client import OpenAILLMClient
from app.ports.llm_client import Message
from worker.profiles import RequestSourceSelectionV012


class _Sample(BaseModel):
    required_field: str
    optional_field: str | None = None
    listy: list[str] = []


def _capture(response_schema: type[BaseModel]) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "required_field": "x",
                                        "optional_field": None,
                                        "listy": [],
                                    }
                                ),
                            }
                        ]
                    }
                ],
            },
        )

    client = OpenAILLMClient(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        model_profiles={"p": "gpt-4o-mini"},
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    try:
        asyncio.run(
            client.generate_structured(
                task_name="t",
                messages=[Message(role="user", content="ping")],
                response_schema=response_schema,
                model_profile="p",
            )
        )
    except Exception:  # 응답 형태는 이 시험의 관심사가 아니다.
        pass
    return sent["text"]["format"]["schema"]


def _strict_violations(node: Any) -> list[str]:
    problems: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            if node.get("additionalProperties") is not False:
                problems.append("additionalProperties is not false")
            missing = set(node["properties"]) - set(node.get("required", []))
            if missing:
                problems.append(f"not required: {sorted(missing)}")
        for value in node.values():
            problems.extend(_strict_violations(value))
    elif isinstance(node, list):
        for value in node:
            problems.extend(_strict_violations(value))
    return problems


def test_optional_fields_are_sent_as_required() -> None:
    schema = _capture(_Sample)

    assert _strict_violations(schema) == []
    assert set(schema["required"]) == {"required_field", "optional_field", "listy"}
    # 타입은 건드리지 않는다. 값을 낼 자리가 남아 있어야 한다.
    assert {"type": "null"} in schema["properties"]["optional_field"]["anyOf"]
    assert schema["properties"]["listy"]["type"] == "array"


def test_vendor_request_selection_schema_is_strict_clean() -> None:
    """`packages/**` 를 고치지 않고 이 경로만으로 통과해야 한다."""

    assert _strict_violations(_capture(RequestSourceSelectionV012)) == []


def test_vendor_schema_without_normalization_would_fail() -> None:
    """역검증: 원본 스키마는 실제로 규칙을 어긴다 (시험이 항상 참이 아님)."""

    assert _strict_violations(RequestSourceSelectionV012.model_json_schema()) != []
