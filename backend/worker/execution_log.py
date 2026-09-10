"""요청서 구조화 실행 한 건을 ``ops.*`` 에 남긴다 (초안 §9.5 실행 진단).

**왜 필요한가.** ``analyse_case`` 는 성공하면 ``ProfileSnapshot.diagnostics`` 를
통째로 버린다. 실패했을 때만 마지막 진단 하나를 예외로 올린다. 그래서 지금은
"이 실행에서 보완이 몇 번 돌았고 무엇이 걸렸나" 가 어디에도 남지 않는다.

그 공백은 관측 취향의 문제가 아니다. 재료화 실패를 묶음 단위로 격리해
정상 결과를 보존하려면 "무엇을 덜어냈는지" 를 적을 곳이 있어야 하는데,
현재 그 자리가 없다. 이 모듈이 그 자리를 만든다.

**새 테이블을 만들지 않는다.** 팀원 스키마의 ``ops.processing_run`` 과
``ops.model_invocation`` 이 이미 이 용도로 설계돼 있다 — run_type, 시도별
행, prompt_version, error_code, 그리고 원문 대신 해시를 두는 칸까지.

노출 등급
---------
``ops.*`` 는 **운영 등급**이다. ``workspace.analysis_run.last_error`` 와 같은
층이고, 사용자 화면에 나가는 ``error_message`` (worker/outcome.py) 와는 다르다.
분석 결과와 같은 수명을 갖는 내부 표이지 범용 로그 싱크가 아니다.

진단 ``message`` 는 여기까지만 온다. 팀원 재료화 오류 문구가 실패를 구분하는
유일한 값이라 담지만(같은 진단이 보완에서 반복되는지 보려면 필요하다),
``last_error`` 와 같은 길이로 자른다. 로그로도, 사용자 응답으로도 가지 않는다.

팀원 스키마가 없는 전환기 DB 에서는 조용히 아무것도 하지 않는다 —
``ensure_identity`` 와 같은 규율이다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.db.identity_bridge import teammate_schema_installed

logger = logging.getLogger(__name__)

__all__ = ["record_profile_run"]

RUN_TYPE = "request_profile"
COMPONENT_NAME = "structure_request_profile"
MODEL_ROLE = "request_source_selection"

# last_error 와 같은 상한. 진단 하나가 표를 채우지 않게 한다.
_MAX_MESSAGE_CHARS = 500


_INSERT_RUN = text(
    """
    INSERT INTO ops.processing_run (
        source_analysis_run_id, run_type, status, pipeline_version,
        component_name, component_version, started_at, finished_at,
        run_metadata, error_code, error_message
    ) VALUES (
        :source_analysis_run_id, :run_type, :status, :pipeline_version,
        :component_name, :component_version, :started_at, :finished_at,
        CAST(:run_metadata AS jsonb), :error_code, :error_message
    )
    RETURNING processing_run_pk
    """
)

# 시도마다 한 행. 토큰·지연은 포트가 돌려주지 않아 NULL 이다 —
# 값을 지어내지 않는다. 포트가 사용량을 돌려주게 되면 같은 칸이 채워진다.
_INSERT_INVOCATION = text(
    """
    INSERT INTO ops.model_invocation (
        processing_run_pk, model_role, model_id, prompt_version, status
    ) VALUES (
        :processing_run_pk, :model_role, :model_id, :prompt_version, :status
    )
    """
)


def record_profile_run(
    connection: Connection,
    snapshot: Any,
    *,
    source_analysis_run_id: UUID | None,
    started_at: datetime,
    finished_at: datetime,
) -> UUID | None:
    """스냅샷 한 건을 ``ops.*`` 에 남기고 run pk 를 돌려준다.

    기록 실패가 분석을 죽이지 않는다. 진단을 남기려다 결과를 잃는 것은
    거꾸로다 — 실패하면 경고만 남기고 ``None`` 을 돌려준다.

    **파이썬 예외를 삼키는 것만으로는 부족하다.** PostgreSQL 은 문장 하나가
    실패하면 트랜잭션 전체를 abort 상태로 만들고, 그 뒤의 모든 명령이
    ``InFailedSqlTransaction`` 으로 거부된다. 즉 예외만 잡으면 호출자의 다음
    쿼리나 커밋이 대신 죽는다 — 실제로 그렇게 동작하는 것을 확인했다.
    그래서 쓰기를 SAVEPOINT 안에 넣는다. 실패하면 거기까지만 되감기고
    바깥 트랜잭션은 멀쩡하게 남는다.

    트랜잭션은 호출자 것이다.
    """
    if not teammate_schema_installed(connection):
        return None

    succeeded = snapshot.status == "OK"
    # 실패 사유는 마지막 진단의 reason code 다. 문구가 아니라 코드로 집계한다.
    last = snapshot.diagnostics[-1] if snapshot.diagnostics else None
    try:
        with connection.begin_nested():
            processing_run_pk = _write(connection, snapshot, succeeded, last, {
                "source_analysis_run_id": source_analysis_run_id,
                "started_at": started_at,
                "finished_at": finished_at,
            })
    except Exception:  # noqa: BLE001 - 진단 기록이 분석을 죽이지 않는다
        logger.warning("Failed to record profile run diagnostics", exc_info=False)
        return None
    return processing_run_pk


def _write(
    connection: Connection,
    snapshot: Any,
    succeeded: bool,
    last: Any,
    context: dict[str, Any],
) -> UUID | None:
    processing_run_pk = connection.scalar(
        _INSERT_RUN,
        {
            "source_analysis_run_id": context["source_analysis_run_id"],
            "run_type": RUN_TYPE,
            "status": "succeeded" if succeeded else "failed",
            "pipeline_version": snapshot.profile_contract_version,
            "component_name": COMPONENT_NAME,
            "component_version": snapshot.prompt_version,
            "started_at": context["started_at"],
            "finished_at": context["finished_at"],
            "run_metadata": json.dumps(_run_metadata(snapshot), ensure_ascii=False),
            # 성공/실패가 아니라 **마지막 진단**에서 가져온다. 부분 재료화로
            # 살아난 실행은 status=succeeded 이면서 PARTIAL_MATERIALIZATION 을
            # 달고 있어야 JSON 을 파싱하지 않고도 집계된다.
            "error_code": _reason_code(last),
            "error_message": _message(last),
        },
    )
    usage_rows = snapshot.usage or []
    for attempt, usage in enumerate(usage_rows, start=1):
        connection.execute(
            _INSERT_INVOCATION,
            {
                "processing_run_pk": processing_run_pk,
                # 보완 호출인지 최초 호출인지가 재현 분석의 핵심 축이다.
                "model_role": (
                    f"{MODEL_ROLE}:repair"
                    if usage.get("repair")
                    else f"{MODEL_ROLE}:initial"
                ),
                "model_id": snapshot.model_id,
                "prompt_version": snapshot.prompt_version,
                "status": (
                    "succeeded"
                    if succeeded or attempt < len(usage_rows)
                    else "failed"
                ),
            },
        )
    return processing_run_pk


def _run_metadata(snapshot: Any) -> dict[str, Any]:
    """집계 가능한 구조만 담는다. 프로필 본문·원문은 담지 않는다."""
    return {
        "profile_id": snapshot.profile_id,
        "candidate_pack_id": snapshot.candidate_pack_id,
        "selection_attempts": snapshot.selection_attempts,
        "diagnostics": [
            {
                "stage": diagnostic.stage,
                "unit": diagnostic.unit,
                "reason_code": diagnostic.reason_code,
                "attempt": diagnostic.attempt,
                "terminated_because": diagnostic.terminated_because,
                # 같은 보완에서 같은 진단이 반복되는지는 이 문구로만 구분된다.
                "message": _message(diagnostic),
            }
            for diagnostic in snapshot.diagnostics or []
        ],
    }


def _reason_code(diagnostic: Any) -> str | None:
    return getattr(diagnostic, "reason_code", None) if diagnostic else None


def _message(diagnostic: Any) -> str | None:
    message = getattr(diagnostic, "message", None) if diagnostic else None
    return message[:_MAX_MESSAGE_CHARS] if message else None
