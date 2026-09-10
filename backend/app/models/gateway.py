"""Inference gateway types. Application code must not bind a vendor SDK."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Sequence

from app.models.outcomes import OutcomeCode


class ModelRole(StrEnum):
    TEXT = "text"
    VISION = "vision"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    model_role: ModelRole
    model_id: str
    model_version: str | None
    prompt_version: str | None
    input_hash: str | None
    output_hash: str | None
    latency_ms: int
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayResult:
    outcome: OutcomeCode
    invocation: ModelInvocation
    text: str | None = None
    vectors: tuple[tuple[float, ...], ...] | None = None
    message: str | None = None


class ModelGateway(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        prompt_version: str | None = None,
        model_id: str | None = None,
    ) -> GatewayResult: ...

    def extract_vision(
        self,
        *,
        image_sha256: str,
        prompt: str = "",
        prompt_version: str | None = None,
        model_id: str | None = None,
    ) -> GatewayResult: ...

    def embed(
        self,
        *,
        texts: Sequence[str],
        model_id: str | None = None,
    ) -> GatewayResult: ...


class NoopModelGateway:
    """Always-offline gateway. Never calls a network or vendor SDK."""

    model_id = "noop"
    model_version = "0"

    def generate(
        self,
        *,
        prompt: str,
        prompt_version: str | None = None,
        model_id: str | None = None,
    ) -> GatewayResult:
        return _unavailable(
            ModelRole.TEXT,
            prompt.encode("utf-8"),
            prompt_version,
            model_id or self.model_id,
            self.model_version,
        )

    def extract_vision(
        self,
        *,
        image_sha256: str,
        prompt: str = "",
        prompt_version: str | None = None,
        model_id: str | None = None,
    ) -> GatewayResult:
        payload = f"{image_sha256}\n{prompt}".encode("utf-8")
        return _unavailable(
            ModelRole.VISION,
            payload,
            prompt_version,
            model_id or self.model_id,
            self.model_version,
        )

    def embed(
        self,
        *,
        texts: Sequence[str],
        model_id: str | None = None,
    ) -> GatewayResult:
        payload = b"\n".join(item.encode("utf-8") for item in texts)
        return _unavailable(
            ModelRole.EMBEDDING,
            payload,
            None,
            model_id or self.model_id,
            self.model_version,
        )


def _unavailable(
    role: ModelRole,
    payload: bytes,
    prompt_version: str | None,
    model_id: str,
    model_version: str,
) -> GatewayResult:
    from hashlib import sha256

    from app.models.outcomes import ErrorCode

    return GatewayResult(
        outcome=OutcomeCode.UNAVAILABLE,
        invocation=ModelInvocation(
            model_role=role,
            model_id=model_id,
            model_version=model_version,
            prompt_version=prompt_version,
            input_hash=sha256(payload).hexdigest(),
            output_hash=None,
            latency_ms=0,
            status="failed",
            error_code=ErrorCode.MODEL_UNAVAILABLE,
        ),
        message="model gateway is noop; no LLM, vision, or embedding call is made",
    )
