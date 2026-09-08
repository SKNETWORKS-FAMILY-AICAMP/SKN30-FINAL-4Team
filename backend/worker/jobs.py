"""Slice 6: ``workspace.analysis_run`` 임대(lease) 기반 클레임·펜싱.

행을 메시지가 아니라 **임대**로 본다. 워커는 행을 꺼내 가는 것이 아니라
`claim_token` 과 만료 시각을 붙여 잠시 빌린다. 워커가 죽으면 임대가 만료되고
다른 워커가 같은 행을 다시 빌린다. 되살아난 옛 워커의 쓰기는 토큰이 달라
0행을 갱신하고 조용히 버려진다 (fencing).

**크래시 시 작업을 통째로 재실행하며 부분 결과를 이어붙이지 않는다.**
checkpoint 재개, 지수 백오프, 우선순위 큐, 데드레터는 만들지 않는다.
상주 워커 루프도 여기 없다. 이 모듈이 아는 것은 네 가지 전이뿐이다:
claim → heartbeat → complete | fail.

트랜잭션은 호출자 것이다. 모든 함수는 열린 ``Connection`` 을 받아 한 문장을
실행하고, 커밋은 하지 않는다. ``claim_next`` 의 행 잠금은 호출자가 커밋할
때까지 유지된다.


향후 확장: 워커 시도를 ``ops.processing_run`` 행으로 남기면 초안 §9.5 의 실행
진단(실패 단위·호출 ID·보완 이력)을 팀원 구조에 그대로 매핑할 수 있다. 지금은
``attempt_count`` 숫자만 센다. 보류 중인 ``worker/queue.py`` 가 그 방식을 먼저
시도했으니 붙일 때 참고한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "Claim",
    "claim_next",
    "complete",
    "fail",
    "heartbeat",
]

# 권장값이지 상수가 아니다. 모든 함수가 lease_seconds 를 인자로 받는다.
# 하트비트는 임대의 1/10 주기로 돈다 — 한 번 놓쳐도 임대가 살아 있다.
DEFAULT_LEASE_SECONDS = 300
DEFAULT_HEARTBEAT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class Claim:
    """한 번의 클레임으로 빌린 임대. ``claim_token`` 이 펜싱 토큰이다."""

    analysis_run_pk: UUID
    claim_token: UUID
    attempt_count: int
    lease_expires_at: datetime


# queued 이거나, running 인데 임대가 만료된 행 하나를 집는다. SKIP LOCKED 로
# 다른 워커가 이미 잠근 행은 건너뛴다. CTE 의 SELECT ... FOR UPDATE 와 UPDATE 가
# 한 문장이므로 두 워커가 같은 행을 집을 수 없다.
_CLAIM = text(
    """
    WITH claimable AS (
        SELECT analysis_run_pk
          FROM workspace.analysis_run
         WHERE status = 'queued'
            OR (status = 'running'
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at < now())
         ORDER BY created_at, analysis_run_pk
         LIMIT 1
         FOR UPDATE SKIP LOCKED
    )
    UPDATE workspace.analysis_run AS r
       SET status           = 'running',
           claim_token      = gen_random_uuid(),
           lease_expires_at = now() + make_interval(secs => :lease_seconds),
           attempt_count    = r.attempt_count + 1,
           claimed_by       = :worker_id,
           -- 재실행이므로 이전 시도의 시작 시각은 의미가 없다. 매번 새로 찍는다.
           started_at       = now(),
           last_error       = NULL,
           updated_at       = now()
      FROM claimable AS c
     WHERE r.analysis_run_pk = c.analysis_run_pk
    RETURNING r.analysis_run_pk, r.claim_token, r.attempt_count, r.lease_expires_at
    """
)

_HEARTBEAT = text(
    """
    UPDATE workspace.analysis_run
       SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
           updated_at       = now()
     WHERE analysis_run_pk = :run_id
       AND claim_token     = :claim_token
       AND status          = 'running'
    """
)

# 종료 쓰기는 반드시 토큰을 함께 건다. 임대를 뺏긴 워커는 여기서 0행을 갱신한다.
_COMPLETE = text(
    """
    UPDATE workspace.analysis_run
       SET status           = 'succeeded',
           completed_at     = now(),
           claim_token      = NULL,
           lease_expires_at = NULL,
           last_error       = NULL,
           updated_at       = now()
     WHERE analysis_run_pk = :run_id
       AND claim_token     = :claim_token
    """
)

_FAIL = text(
    """
    UPDATE workspace.analysis_run
       SET status           = 'failed',
           completed_at     = now(),
           claim_token      = NULL,
           lease_expires_at = NULL,
           last_error       = :last_error,
           updated_at       = now()
     WHERE analysis_run_pk = :run_id
       AND claim_token     = :claim_token
    """
)


def claim_next(
    connection: Connection,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Claim | None:
    """빌릴 수 있는 run 하나를 원자적으로 잡는다. 없으면 ``None``."""
    row = connection.execute(
        _CLAIM, {"worker_id": worker_id, "lease_seconds": lease_seconds}
    ).one_or_none()
    return None if row is None else Claim(*row)


def heartbeat(
    connection: Connection,
    *,
    run_id: UUID,
    claim_token: UUID,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """토큰이 아직 내 것일 때만 임대를 연장한다. 뺏겼으면 ``False``.

    ``False`` 는 작업을 중단하라는 신호다. 계속 돌아봐야 결과는 펜싱으로 버려진다.
    """
    result = connection.execute(
        _HEARTBEAT,
        {
            "run_id": run_id,
            "claim_token": claim_token,
            "lease_seconds": lease_seconds,
        },
    )
    return result.rowcount == 1


def complete(
    connection: Connection, *, run_id: UUID, claim_token: UUID
) -> bool:
    """성공으로 종료한다. ``False`` 면 펜싱된 것이니 결과를 조용히 버린다.

    예외를 던지지 않는다. 임대를 뺏긴 워커가 뒤늦게 도착하는 것은 정상 경로이지
    오류가 아니며, 이미 다른 워커가 쓴 결과를 덮지 않는 것이 요점이다.
    """
    return connection.execute(
        _COMPLETE, {"run_id": run_id, "claim_token": claim_token}
    ).rowcount == 1


def fail(
    connection: Connection, *, run_id: UUID, claim_token: UUID, last_error: str
) -> bool:
    """실패로 종료한다. ``complete`` 와 같은 펜싱 규율을 따른다."""
    return connection.execute(
        _FAIL,
        {"run_id": run_id, "claim_token": claim_token, "last_error": last_error},
    ).rowcount == 1
