"""Cookie-authenticated asynchronous, result-grounded conversation routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ports.conversations import (
    ConversationConflict,
    ConversationMessageRecord,
    ConversationNotFound,
    ConversationRepository,
    ConversationRepositoryUnavailable,
    ConversationRetryCooldown,
    ConversationRetryExhausted,
)

from ..auth import PrincipalDep, access_cookie_scheme, require_trusted_origin
from .openapi_models import error_responses


router = APIRouter(prefix="/analysis-cases", tags=["Conversations"])


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


def conversation_repository(request: Request) -> ConversationRepository:
    repository = getattr(request.app.state, "conversation_repository", None)
    if not isinstance(repository, ConversationRepository):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation service is not configured",
        )
    return repository


ConversationRepositoryDep = Annotated[
    ConversationRepository, Depends(conversation_repository)
]
TrustedOriginDep = Annotated[None, Depends(require_trusted_origin)]


def _not_found(exc: ConversationNotFound) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Conversation was not found",
    )


def _conflict(exc: ConversationConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Conversation message cannot be processed in its current state",
    )


def _unavailable(exc: ConversationRepositoryUnavailable) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Conversation service is temporarily unavailable",
    )


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
    principal: PrincipalDep,
    _: TrustedOriginDep,
    repository: ConversationRepositoryDep,
) -> ConversationTurnResponse:
    try:
        record = await repository.create_message(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            content=payload.content,
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
    response_model=list[ConversationMessageResponse],
    summary="분석 결과 대화 조회",
    dependencies=[Security(access_cookie_scheme)],
    responses=error_responses(401, 403, 404, 422, 500, 503),
)
async def list_conversation_messages(
    analysis_case_id: UUID,
    principal: PrincipalDep,
    repository: ConversationRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    updated_since: Annotated[
        datetime | None,
        Query(
            description=(
                "폴링용 델타 커서. 직전 응답에서 가장 큰 updated_at 을 그대로 넣으면 "
                "그 시각 이후에 생기거나 바뀐 메시지만 돌아온다. 경계는 포함(>=)이라 "
                "직전 마지막 메시지가 한 건 다시 오므로 message_id 로 병합한다. "
                "sequence_no 커서를 쓰면 안 된다 — generating 이던 답변이 completed 로 "
                "바뀔 때 sequence_no 는 그대로여서 완료를 놓친다."
            ),
        ),
    ] = None,
) -> list[ConversationMessageResponse]:
    try:
        records = await repository.list_messages(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            limit=limit,
            updated_since=updated_since,
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except ConversationRepositoryUnavailable as exc:
        raise _unavailable(exc) from exc
    return [_message_response(record) for record in records]


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
    principal: PrincipalDep,
    _: TrustedOriginDep,
    repository: ConversationRepositoryDep,
) -> ConversationTurnResponse:
    try:
        record = await repository.retry_message(
            owner_id=principal.user_id,
            analysis_case_id=str(analysis_case_id),
            assistant_message_id=str(assistant_message_id),
        )
    except ConversationNotFound as exc:
        raise _not_found(exc) from exc
    except (ConversationRetryExhausted, ConversationRetryCooldown, ConversationConflict) as exc:
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
    "ConversationMessageRequest",
    "ConversationMessageResponse",
    "ConversationTurnResponse",
    "router",
]
