"""외부 Case 식별자(UUID) ↔ 내부 PK(bigint).

프론트는 `analysis_case_id(UUID)` 하나만 안다. 내부 테이블은 그대로 bigint PK
로 참조를 건다 — 이미 모든 테이블이 그것을 쓰고 있고, 외부 식별자를 위해 그
전부를 바꿀 이유가 없다. 경계에서 한 번만 변환한다.
"""
from uuid import UUID

from sqlalchemy import Engine, text


class CaseNotFoundError(LookupError):
    """그런 분석 건이 없거나 이 사용자의 것이 아니다. 둘을 구분하지 않는다."""


def resolve_internal_case_id(
    engine: Engine,
    owner_user_id: int,
    analysis_case_id: UUID,
) -> int:
    with engine.connect() as connection:
        case_id = connection.scalar(
            text(
                """
                SELECT id
                FROM sims.inspection_case
                WHERE analysis_case_id = CAST(:analysis_case_id AS uuid)
                  AND owner_user_id = :owner_user_id
                """
            ),
            {
                "analysis_case_id": str(analysis_case_id),
                "owner_user_id": owner_user_id,
            },
        )
    if case_id is None:
        raise CaseNotFoundError
    return int(case_id)


def public_case_id(engine: Engine, case_id: int) -> UUID:
    """내부 PK → 외부 UUID. 응답을 만들 때만 쓴다."""
    with engine.connect() as connection:
        value = connection.scalar(
            text("SELECT analysis_case_id FROM sims.inspection_case WHERE id = :case_id"),
            {"case_id": case_id},
        )
    if value is None:
        raise CaseNotFoundError
    return value if isinstance(value, UUID) else UUID(str(value))
