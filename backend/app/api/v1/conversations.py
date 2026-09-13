"""Cookie-authenticated asynchronous, result-grounded conversation routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Security, status
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from app.api.cursor import decode_cursor, encode_cursor
from app.api.errors import (
    ApiError,
    chat_conflict,
    chat_retry_exhausted,
    idempotency_key_conflict,
    not_found,
    service_unavailable,
)
from app.ports.conversations import (
    ConversationConflict,
    ConversationIdempotencyKeyConflict,
    ConversationMessagePage,
    ConversationMessageRecord,
    ConversationNotFound,
    ConversationRepository,
    ConversationRepositoryUnavailable,
    ConversationRetryExhausted,
)

from ..auth import PrincipalDep, access_cookie_scheme, require_trusted_origin
from .openapi_models import error_responses


router = APIRouter(prefix="/analysis-cases", tags=["Conversations"])

_MESSAGES_CURSOR_ENDPOINT = "analysis-case-messages"
_MESSAGES_CURSOR_VERSION = 1
_MESSAGES_CURSOR_KEYS = frozenset({"sequence_no", "message_id"})


class ConversationMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=4_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Chat question must not be blank")
        return value


class ConversationTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_message_id: UUID
    assistant_message_id: UUID
    analysis_session_id: UUID
    status: Literal["generating"]
    retry_count: int = Field(ge=0, le=2)


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    analysis_case_id: UUID
    role: Literal["user", "assistant"]
    sequence_no: int = Field(gt=0)
    content: str | None
    status: Literal["generating", "completed", "failed"]
    reply_to_message_id: UUID | None
    retry_count: int = Field(ge=0)
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ConversationMessageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ConversationMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None


IdempotencyKeyHeader = Annotated[
    UUID4,
    Header(
        alias="Idempotency-Key",
        description=(
            "재시도에서도 동일하게 보내는 클라이언트 생성 UUID. 같은 key와 동일 "
            "본문 재전송은 기존 user/assistant message ID를 반환하고, 같은 key와 "
            "다른 본문은 409 IDEMPOTENCY_KEY_CONFLICT다."
        ),
    ),
]


async def conversation_repository(request: Request) -> ConversationRepository:
    # ``async def``, not a threadpool-hopping ``def``: see the identical note
    # on ``analysis_run_service`` in app/api/v1/analysis_runs.py.
    repository = getattr(request.app.state, "conversation_repository", None)
    if not isinstance(repository, ConversationRepository):
        raise service_unavailable("Conversation service is not configured")
    return repository


ConversationRepositoryDep = Annotated[
    ConversationRepository, Depends(conversation_repository)
]
TrustedOriginDep = Annotated[None, Depends(require_trusted_origin)]


def _not_found(exc: ConversationNotFound) -> ApiError:
    return not_found("Conversation was not found")


def _conflict(exc: ConversationConflict) -> ApiError:
    if isinstance(exc, ConversationIdempotencyKeyConflict):
        return idempotency_key_conflict()
    if isinstance(exc, ConversationRetryExhausted):
        return chat_retry_exhausted()
    # Covers ConversationRetryCooldown and any other state conflict: the
    # spec's single catch-all "채팅 상태 충돌" domain code (v0.2 section 4).
    return chat_conflict()


def _unavailable(exc: ConversationRepositoryUnavailable) -> ApiError:
    return service_unavailable("Conversation service is temporarily unavailable")


def _cursor_secret(request: Request) -> str:
    return str(getattr(request.app.state, "cursor_signing_secret", "") or "")


@router.post(
    "/{analysis_case_id}/messages",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="분석 결과에 질문 등록",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 409, 422, 500, 503),
)
async def create_conversation_message(
    analysis_case_id: UUID,
    payload: ConversationMessageRequest,
    idempotency_key: IdempotencyKeyHeader,
    principal: PrincipalDep,
    _: TrustedOriginDep,
    repository: ConversationRepositoryDep,
) -> ConversationTurnResponse:
    try:
        record = await repository.create_message(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            content=payload.content,
            idempotency_key=str(idempotency_key),
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except ConversationRepositoryUnavailable as exc:
        raise _unavailable(exc) from exc
    except ConversationConflict as exc:
        raise _conflict(exc) from exc
    return ConversationTurnResponse(
        user_message_id=record.user_message_id,
        assistant_message_id=record.assistant_message_id,
        analysis_session_id=record.analysis_session_id,
        status="generating",
        retry_count=record.retry_count,
    )


@router.get(
    "/{analysis_case_id}/messages",
    response_model=ConversationMessageEnvelope,
    summary="과거 대화 목록 조회 (keyset pagination)",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def list_conversation_messages(
    request: Request,
    analysis_case_id: UUID,
    principal: PrincipalDep,
    repository: ConversationRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[
        str | None,
        Query(max_length=2_000, description="이전 응답의 next_cursor를 그대로 전달한다."),
    ] = None,
) -> ConversationMessageEnvelope:
    secret = _cursor_secret(request)
    scope = f"{principal.user_id}:{analysis_case_id}"
    decoded_cursor: tuple[int, str] | None = None
    if cursor is not None:
        fields = decode_cursor(
            cursor,
            secret=secret,
            endpoint=_MESSAGES_CURSOR_ENDPOINT,
            scope=scope,
            version=_MESSAGES_CURSOR_VERSION,
            required_keys=_MESSAGES_CURSOR_KEYS,
        )
        decoded_cursor = (int(fields["sequence_no"]), str(fields["message_id"]))
    try:
        page: ConversationMessagePage = await repository.list_messages(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            limit=limit,
            cursor=decoded_cursor,
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except ConversationRepositoryUnavailable as exc:
        raise _unavailable(exc) from exc
    next_cursor = None
    if page.next_cursor is not None:
        sequence_no, message_id = page.next_cursor
        next_cursor = encode_cursor(
            secret=secret,
            endpoint=_MESSAGES_CURSOR_ENDPOINT,
            scope=scope,
            version=_MESSAGES_CURSOR_VERSION,
            fields={"sequence_no": sequence_no, "message_id": message_id},
        )
    return ConversationMessageEnvelope(
        items=[_message_response(record) for record in page.items],
        next_cursor=next_cursor,
    )


@router.get(
    "/{analysis_case_id}/messages/{message_id}",
    response_model=ConversationMessageResponse,
    summary="단건 메시지 상태 polling",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def get_conversation_message(
    analysis_case_id: UUID,
    message_id: UUID,
    principal: PrincipalDep,
    repository: ConversationRepositoryDep,
) -> ConversationMessageResponse:
    try:
        record = await repository.get_message(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            message_id=str(message_id),
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except ConversationRepositoryUnavailable as exc:
        raise _unavailable(exc) from exc
    return _message_response(record)


@router.post(
    "/{analysis_case_id}/messages/{assistant_message_id}/retry",
    response_model=ConversationTurnResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="실패한 AI 답변 재시도 등록",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 409, 422, 500, 503),
)
async def retry_conversation_message(
    analysis_case_id: UUID,
    assistant_message_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    principal: PrincipalDep,
    _: TrustedOriginDep,
    repository: ConversationRepositoryDep,
) -> ConversationTurnResponse:
    try:
        record = await repository.retry_message(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            assistant_message_id=str(assistant_message_id),
            idempotency_key=str(idempotency_key),
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except (ConversationRetryExhausted, ConversationConflict) as exc:
        raise _conflict(exc) from exc
    except ConversationRepositoryUnavailable as exc:
        raise _unavailable(exc) from exc
    return ConversationTurnResponse(
        user_message_id=record.user_message_id,
        assistant_message_id=record.assistant_message_id,
        analysis_session_id=record.analysis_session_id,
        status="generating",
        retry_count=record.retry_count,
    )


def _message_response(record: ConversationMessageRecord) -> ConversationMessageResponse:
    return ConversationMessageResponse(
        message_id=record.message_id,
        analysis_case_id=record.analysis_case_id,
        role=record.role,
        sequence_no=record.sequence_no,
        content=record.content,
        status=record.status,
        reply_to_message_id=record.reply_to_message_id,
        retry_count=record.retry_count,
        error_code=record.error_code,
        error_message=record.error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


__all__ = [
    "ConversationMessageEnvelope",
    "ConversationMessageRequest",
    "ConversationMessageResponse",
    "ConversationTurnResponse",
    "router",
]
