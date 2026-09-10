"""Slice 6: 분석 작업을 asyncio 태스크가 아니라 DB 큐에 넣는다.

``app.ports.job_dispatcher.JobDispatcher`` 의 DB 구현이다. 넣는 쪽은
``workspace.analysis_run`` 에 ``queued`` 행 하나를 만들고, 꺼내는 쪽은
``worker.jobs`` 의 임대·펜싱을 그대로 쓴다. 큐 전이는 여기서 다시 만들지 않는다.

**초안 §10: 두 큐를 동시에 운영하지 않는다.** 이것이 ``InProcessJobDispatcher``
를 큐로서 대체한다. 프로세스 안 태스크 집합은 더 이상 큐가 아니라 실행기일
뿐이다 — 큐는 DB 행 하나이고, 그 행을 누가 집어 실행하느냐만 남는다.

멱등 제출은 ``100_analysis_run_queue.sql`` 헤더가 규정한
``INSERT ... ON CONFLICT ... DO NOTHING`` 형태를 그대로 쓴다. 다만
``submission_sha256`` 에는 제출물의 내용 해시가 아니라 **케이스 신원 해시**를
넣는다. 같은 파일을 올린 서로 다른 검사 건이 서로의 분석을 막으면 안 되고,
여기서 막아야 하는 것은 "같은 케이스의 더블 서브밋" 하나뿐이기 때문이다.

상주 데몬도, 백오프도, 체크포인트 재개도 없다. ``run_once`` 는 한 번 집어
한 번 돌리고 한 번 끝낸다.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID
from uuid import uuid4

from sqlalchemy import Engine, text

from app.db.identity_bridge import ensure_identity
from app.infrastructure.in_process_job_dispatcher import InProcessJobDispatcher
from app.ports.object_storage import ObjectStorage, StoredObject, storage_key
from app.ports.pdf_renderer import PdfRenderer
from worker.jobs import DEFAULT_LEASE_SECONDS, claim_next, complete, fail
from worker.outcome import DEFAULT_ERROR_CODE, user_outcome
from worker.persistence import AnalysisResults, persist_results
from worker.report_pdf import compose_queued_case_report

logger = logging.getLogger(__name__)

__all__ = ["QueueJobDispatcher", "enqueue", "run_once", "submission_key"]

# last_error 컬럼에 스택 전체를 붓지 않는다. 무엇이 터졌는지 한 줄이면 된다.
_MAX_ERROR_CHARS = 500
_REPORT_BUCKET = "report"


def submission_key(case_id: int) -> str:
    """케이스 신원을 ``submission_sha256`` 의 64자리 hex 형식으로 옮긴다."""
    return hashlib.sha256(f"sims.inspection_case:{case_id}".encode()).hexdigest()


# 부분 유니크 인덱스이므로 WHERE 절을 함께 적어야 아비터 인덱스가 추론된다.
_INSERT_RUN = text(
    """
    INSERT INTO workspace.analysis_run (user_id, status, submission_sha256)
    VALUES (:user_id, 'queued', :submission_sha256)
    ON CONFLICT (user_id, submission_sha256)
        WHERE submission_sha256 IS NOT NULL
          AND status IN ('queued', 'running')
    DO NOTHING
    RETURNING analysis_run_pk
    """
)

_LIVE_RUN = text(
    """
    SELECT analysis_run_pk
      FROM workspace.analysis_run
     WHERE user_id = :user_id
       AND submission_sha256 = :submission_sha256
       AND status IN ('queued', 'running')
    """
)

_CASE_OWNER = text(
    """
    SELECT owner_user_id
      FROM sims.inspection_case
     WHERE id = :case_id
    """
)

_LINK_CASE = text(
    """
    UPDATE sims.inspection_case
       SET analysis_run_id = :run_id
     WHERE id = :case_id
    """
)

_LINKED_CASE = text(
    """
    SELECT c.id AS case_id, r.user_id, c.created_at,
           (
               SELECT f.original_filename
                 FROM sims.uploaded_document d
                 JOIN sims.file_asset f ON f.id = d.file_asset_id
                WHERE d.inspection_case_id = c.id
                LIMIT 1
           ) AS original_filename
      FROM workspace.analysis_run r
      JOIN sims.inspection_case c ON c.analysis_run_id = r.analysis_run_pk
     WHERE r.analysis_run_pk = :run_id
    """
)

# 케이스의 최종 상태가 결과 행의 상태를 정한다. 파이프라인은 분석 실패를
# 예외가 아니라 케이스 상태로 알리므로, 예외가 없었다는 것만으로 'ready' 를
# 쓰면 실패한 분석이 성공 결과로 남는다.
_WRITE_RESULT = text(
    """
    INSERT INTO result.analysis_case (
        source_analysis_run_id, user_id, case_status, analysis_completed_at,
        program_name, original_filename, report_status
    )
    SELECT :run_id, :user_id,
           CASE WHEN c.status = 'COMPLETED' THEN 'ready' ELSE 'failed' END,
           CASE WHEN c.status = 'COMPLETED' THEN now() END,
           NULL,
           (
               SELECT f.original_filename
                 FROM sims.uploaded_document d
                 JOIN sims.file_asset f ON f.id = d.file_asset_id
                WHERE d.inspection_case_id = c.id
                LIMIT 1
           ),
           CASE WHEN c.status = 'COMPLETED' THEN 'ready' ELSE 'failed' END
      FROM sims.inspection_case c
     WHERE c.id = :case_id
    ON CONFLICT (source_analysis_run_id) DO UPDATE
       SET case_status           = EXCLUDED.case_status,
           analysis_completed_at = EXCLUDED.analysis_completed_at,
           program_name          = EXCLUDED.program_name,
           original_filename     = EXCLUDED.original_filename,
           report_status         = EXCLUDED.report_status,
           updated_at            = now()
    RETURNING analysis_case_pk
    """
)

_WRITE_READY_RESULT = text(
    """
    INSERT INTO result.analysis_case (
        analysis_case_pk, source_analysis_run_id, user_id,
        case_status, analysis_completed_at,
        program_name, original_filename, report_status
    ) VALUES (
        :analysis_case_pk, :run_id, :user_id, 'ready', :completed_at,
        :program_name, :original_filename, :report_status
    )
    ON CONFLICT (source_analysis_run_id) DO UPDATE
       SET case_status           = EXCLUDED.case_status,
           analysis_completed_at = EXCLUDED.analysis_completed_at,
           program_name          = EXCLUDED.program_name,
           original_filename     = EXCLUDED.original_filename,
           report_status         = EXCLUDED.report_status,
           updated_at            = now()
    RETURNING analysis_case_pk
    """
)

_INSERT_REPORT_ARTIFACT = text(
    """
    INSERT INTO result.report_artifact (
        analysis_case_pk, report_type, storage_bucket, storage_object_key,
        content_sha256, mime_type, size_bytes, expires_at
    ) VALUES (
        :analysis_case_pk, 'pdf', :storage_bucket, :storage_object_key,
        :content_sha256, 'application/pdf', :size_bytes,
        COALESCE(
            (
                SELECT retention_expires_at
                  FROM result.analysis_case
                 WHERE analysis_case_pk = :analysis_case_pk
            ),
            'infinity'::timestamptz
        )
    )
    """
)

_WRITE_ANALYSIS_SESSION = text(
    """
    INSERT INTO result.analysis_session (
        analysis_case_pk, status, started_at, last_activity_at,
        expires_at, closed_at, updated_at
    ) VALUES (
        :analysis_case_pk, 'active', now(), now(),
        now() + interval '30 minutes', NULL, now()
    )
    ON CONFLICT (analysis_case_pk) DO UPDATE
       SET status           = 'active',
           last_activity_at = now(),
           expires_at       = now() + interval '30 minutes',
           closed_at        = NULL,
           updated_at       = now()
    """
)


class _Fenced(Exception):
    """임대를 뺏긴 워커가 결과 쓰기를 되돌리게 하는 내부 신호."""


@dataclass(frozen=True, slots=True)
class _QueuedReportAttempt:
    analysis_case_pk: UUID
    completed_at: datetime
    report_status: str
    storage_bucket: str | None = None
    storage_object_key: str | None = None
    content_sha256: str | None = None
    size_bytes: int | None = None
    cleanup_keys: tuple[str, ...] = ()
    error: BaseException | None = None


async def _delete_report_files(
    storage: ObjectStorage | None, keys: set[str]
) -> None:
    if storage is None:
        return
    for key in keys:
        try:
            await storage.delete(key)
        except Exception:  # noqa: BLE001 - 보상 실패는 운영 로그에만 남긴다
            logger.exception("Failed to compensate report PDF: %s", key)


async def _prepare_queued_report(
    *,
    object_storage: ObjectStorage,
    pdf_renderer: PdfRenderer,
    linked: dict,
    outcome: AnalysisResults,
    analysis_case_pk: UUID,
) -> _QueuedReportAttempt:
    completed_at = datetime.now(timezone.utc)
    object_key = f"{analysis_case_pk}/report.pdf"
    storage_path = storage_key(_REPORT_BUCKET, object_key)
    stored: StoredObject | None = None
    cleanup_keys = {storage_path}
    put_started = False
    try:
        title = (
            outcome.original_filename
            or linked.get("original_filename")
            or outcome.program_name
            or "분석 요청서"
        )
        report = compose_queued_case_report(
            case_id=linked["case_id"],
            title=title,
            completed_at=completed_at,
            results=outcome,
        )
        pdf_bytes = await pdf_renderer.render(report)
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF renderer returned invalid content")
        put_started = True
        stored = await object_storage.put(storage_path, io.BytesIO(pdf_bytes))
        cleanup_keys.add(stored.key)
        if stored.key != storage_path or stored.size_bytes != len(pdf_bytes):
            raise RuntimeError("Stored PDF metadata does not match rendered content")
    except Exception as error:  # noqa: BLE001 - 분석 결과는 별도 저장한다
        await _delete_report_files(
            object_storage, cleanup_keys if put_started else set()
        )
        return _QueuedReportAttempt(
            analysis_case_pk=analysis_case_pk,
            completed_at=completed_at,
            report_status="failed",
            storage_bucket=_REPORT_BUCKET,
            storage_object_key=object_key,
            cleanup_keys=tuple(cleanup_keys if put_started else ()),
            error=error,
        )
    return _QueuedReportAttempt(
        analysis_case_pk=analysis_case_pk,
        completed_at=completed_at,
        report_status="ready",
        storage_bucket=_REPORT_BUCKET,
        storage_object_key=object_key,
        content_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        size_bytes=len(pdf_bytes),
        cleanup_keys=tuple(cleanup_keys),
    )


def enqueue(engine: Engine, case_id: int) -> UUID:
    """검사 건 하나를 큐에 넣고 run id 를 돌려준다. 여러 번 불러도 하나다."""
    with engine.begin() as connection:
        owner_user_id = connection.scalar(_CASE_OWNER, {"case_id": case_id})
        if owner_user_id is None:
            raise LookupError(f"sims.inspection_case {case_id} does not exist")
        # 팀원 테이블에 행을 넣기 직전이 신원 다리가 필요한 시점이다.
        params = {
            "user_id": ensure_identity(connection, owner_user_id),
            "submission_sha256": submission_key(case_id),
        }
        run_id = connection.scalar(_INSERT_RUN, params)
        if run_id is None:
            # 0행이 돌아왔다는 것은 이미 살아 있는 run 이 있다는 뜻이다.
            run_id = connection.execute(_LIVE_RUN, params).scalar_one()
        connection.execute(_LINK_CASE, {"run_id": run_id, "case_id": case_id})
        return run_id


async def run_once(
    engine: Engine,
    run_analysis: Callable[[int], Awaitable[object]],
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    require_analysis_results: bool = False,
    object_storage: ObjectStorage | None = None,
    pdf_renderer: PdfRenderer | None = None,
) -> UUID | None:
    """한 건 집어 돌리고 끝낸다. 집을 것이 없으면 ``None``."""
    with engine.begin() as connection:
        claim = claim_next(
            connection, worker_id=worker_id, lease_seconds=lease_seconds
        )
        if claim is None:
            return None
        linked = (
            connection.execute(_LINKED_CASE, {"run_id": claim.analysis_run_pk})
            .mappings()
            .one_or_none()
        )
    # 클레임을 먼저 커밋한다. 임대가 보이지 않으면 분석이 도는 동안 다른
    # 워커가 같은 행을 집는다.

    if linked is None:
        _finish_failed(engine, claim, "linked inspection_case was not found")
        return claim.analysis_run_pk

    try:
        outcome = await run_analysis(linked["case_id"])
        if isinstance(outcome, AnalysisResults) and outcome.cpl is None:
            raise TypeError("queued worker must return AnalysisResults with CPL")
        if require_analysis_results and (
            not isinstance(outcome, AnalysisResults) or outcome.cpl is None
        ):
            raise TypeError("queued worker must return AnalysisResults with CPL")
    except Exception as error:  # noqa: BLE001 - 실패 사유를 행에 남기고 삼킨다
        logger.warning(
            "Queued analysis failed for run %s: %s",
            claim.analysis_run_pk,
            type(error).__name__,
        )
        _finish_failed(engine, claim, f"{type(error).__name__}: {error}", error)
        return claim.analysis_run_pk

    # PDF 는 외부 저장소의 부수효과라 SQL 트랜잭션 안에서 기다리지 않는다.
    # 대신 최종 결과 트랜잭션 전에 만들고, 그 트랜잭션이 펜싱되면 즉시
    # 보상 삭제한다. PDF 가 실패해도 결과 행을 남기기 위해 report_status 만
    # failed 로 낮춘다.
    report_attempt: _QueuedReportAttempt | None = None
    if isinstance(outcome, AnalysisResults):
        report_case_pk = uuid4()
        if object_storage is None and pdf_renderer is None:
            # 의존성을 주지 않은 전환기 호출은 PDF를 아직 생성하지 않는다.
            report_attempt = _QueuedReportAttempt(
                analysis_case_pk=report_case_pk,
                completed_at=datetime.now(timezone.utc),
                report_status="generating",
            )
        elif object_storage is None or pdf_renderer is None:
            report_attempt = _QueuedReportAttempt(
                analysis_case_pk=report_case_pk,
                completed_at=datetime.now(timezone.utc),
                report_status="failed",
                error=RuntimeError("PDF renderer and object storage are required"),
            )
        else:
            report_attempt = await _prepare_queued_report(
                object_storage=object_storage,
                pdf_renderer=pdf_renderer,
                linked=linked,
                outcome=outcome,
                analysis_case_pk=report_case_pk,
            )
            if report_attempt.error is not None:
                logger.warning(
                    "Queued report generation failed for run %s: %s",
                    claim.analysis_run_pk,
                    type(report_attempt.error).__name__,
                )

    # 펜싱 검사와 결과 쓰기는 반드시 한 트랜잭션이다. complete() 가 0행이면
    # 임대를 뺏긴 것이므로 예외를 던져 같은 트랜잭션의 결과 INSERT 까지
    # 함께 되돌린다. 임대를 잃은 워커는 결과를 단 한 행도 남기지 못한다.
    # 축·후보·근거·보고서 메타데이터도 이 블록 안에서 쓴다.
    try:
        with engine.begin() as connection:
            result_statement = (
                _WRITE_READY_RESULT
                if isinstance(outcome, AnalysisResults)
                else _WRITE_RESULT
            )
            analysis_case_pk = connection.scalar(
                result_statement,
                {
                    "analysis_case_pk": (
                        report_attempt.analysis_case_pk
                        if report_attempt is not None
                        else None
                    ),
                    "run_id": claim.analysis_run_pk,
                    "user_id": linked["user_id"],
                    "case_id": linked["case_id"],
                    "completed_at": (
                        report_attempt.completed_at
                        if report_attempt is not None
                        else None
                    ),
                    "report_status": (
                        report_attempt.report_status
                        if report_attempt is not None
                        else None
                    ),
                    "program_name": (
                        outcome.program_name
                        if isinstance(outcome, AnalysisResults)
                        else None
                    ),
                    "original_filename": (
                        outcome.original_filename
                        if isinstance(outcome, AnalysisResults)
                        else None
                    ),
                },
            )
            if isinstance(outcome, AnalysisResults) and analysis_case_pk is not None:
                if (
                    report_attempt is None
                    or analysis_case_pk != report_attempt.analysis_case_pk
                ):
                    raise RuntimeError("Queued result identity changed")
                persist_results(
                    connection,
                    analysis_case_pk=analysis_case_pk,
                    cpl=outcome.cpl,
                    fit=outcome.fit,
                    sim=outcome.sim,
                    sim_profiles=outcome.sim_profiles,
                )
                connection.execute(
                    _WRITE_ANALYSIS_SESSION,
                    {"analysis_case_pk": analysis_case_pk},
                )
                if report_attempt.report_status == "ready":
                    connection.execute(
                        _INSERT_REPORT_ARTIFACT,
                        {
                            "analysis_case_pk": analysis_case_pk,
                            "storage_bucket": report_attempt.storage_bucket,
                            "storage_object_key": report_attempt.storage_object_key,
                            "content_sha256": report_attempt.content_sha256,
                            "size_bytes": report_attempt.size_bytes,
                        },
                    )
            if not complete(
                connection,
                run_id=claim.analysis_run_pk,
                claim_token=claim.claim_token,
                # RUN-04 Realtime 이 succeeded 이벤트에서 이 값으로 결과를
                # 연다. 결과 행과 같은 트랜잭션에서 써야 화면이 아직 없는
                # 케이스를 열려고 하지 않는다.
                analysis_case_pk=analysis_case_pk,
            ):
                raise _Fenced
    except _Fenced:
        # 정상 경로다. 다른 워커가 이미 이 run 을 맡았다.
        if report_attempt is not None:
            await _delete_report_files(
                object_storage, set(report_attempt.cleanup_keys)
            )
        logger.warning(
            "Discarded fenced result for run %s (worker %s)",
            claim.analysis_run_pk,
            worker_id,
        )
    except Exception as error:  # noqa: BLE001 - 결과 저장 실패도 큐 실패로 남긴다
        # 결과 행·축·근거는 위 트랜잭션에서 이미 롤백됐다. 별도 트랜잭션으로
        # 큐 행만 실패 처리해, 영속화 오류가 running 작업으로 고립되지 않게 한다.
        if report_attempt is not None:
            await _delete_report_files(
                object_storage, set(report_attempt.cleanup_keys)
            )
        logger.warning(
            "Queued result persistence failed for run %s: %s",
            claim.analysis_run_pk,
            type(error).__name__,
        )
        _finish_failed(engine, claim, f"{type(error).__name__}: {error}", error)
    return claim.analysis_run_pk


def _finish_failed(
    engine: Engine, claim, last_error: str, error: BaseException | None = None
) -> None:
    """운영용 원문과 사용자용 문구를 각자의 칸에 넣는다.

    ``last_error`` 에는 예외 원문이 들어가고 화면에 나가지 않는다.
    ``error_code``·``error_message`` 는 ``worker.outcome`` 의 고정표에서만
    온다 — 예외 문자열이 사용자 화면으로 새는 경로를 만들지 않는다.
    """
    error_code, error_message = (
        user_outcome(error) if error is not None else (DEFAULT_ERROR_CODE, None)
    )
    with engine.begin() as connection:
        fail(
            connection,
            run_id=claim.analysis_run_pk,
            claim_token=claim.claim_token,
            last_error=last_error[:_MAX_ERROR_CHARS],
            error_code=error_code,
            error_message=error_message,
        )


class QueueJobDispatcher:
    """``JobDispatcher`` 의 DB 큐 구현.

    ``run_analysis`` 를 주면 넣은 직후 프로세스 안에서 한 건을 꺼내 돌린다.
    ponytail: 전환기 실행기다. 큐는 DB 하나뿐이고 이 태스크는 그것을 꺼내는
    수단일 뿐이다. 별도 워커 프로세스를 띄우면 ``run_analysis`` 를 빼고
    삽입만 하는 디스패처가 된다.

    팀원 스키마가 없는 DB 도 전환기에는 남아 있다. 그 DB 에는 큐가 아예 없으므로
    예전 ``InProcessJobDispatcher`` 에 그대로 위임한다. 두 큐를 동시에 운영하는
    것이 아니라, 큐가 있으면 큐 하나만 쓴다 (초안 §10). 팀원 스키마가 표준이
    되면 이 갈래와 위임 대상 클래스를 함께 지운다.

    설치 여부는 기동이 아니라 **첫 제출 때** 본다. 기동에서 DB 를 건드리면
    DB 없이 뜨는 경로(CORS·검증 테스트, 부팅 순서상 DB 가 늦게 오는 배포)가
    연결 타임아웃만큼 멈춘다.
    """

    def __init__(
        self,
        engine: Engine,
        run_analysis: Callable[[int], Awaitable[object]] | None = None,
        *,
        legacy_run_analysis: Callable[[int], Awaitable[object]] | None = None,
        worker_id: str | None = None,
        object_storage: ObjectStorage | None = None,
        pdf_renderer: PdfRenderer | None = None,
    ) -> None:
        self._engine = engine
        self._run_analysis = run_analysis
        self._object_storage = object_storage
        self._pdf_renderer = pdf_renderer
        self._worker_id = worker_id or f"inproc-{os.getpid()}"
        self._tasks: set[asyncio.Task[UUID | None]] = set()
        self._queue_installed: bool | None = None
        # 큐 갈래는 결과 계약(``AnalysisResults``)까지 돌려주는 콜러블을 쓰고,
        # 큐가 없는 전환기 DB 는 예전 콜러블을 그대로 쓴다. 하나만 주면 두
        # 갈래가 같은 것을 쓴다 — 기존 호출자의 모양이다.
        legacy = legacy_run_analysis or run_analysis
        self._legacy = InProcessJobDispatcher(legacy) if legacy is not None else None
        # An explicitly supplied legacy callback marks the first callback as the
        # new result-producing worker. The one-argument form remains the legacy
        # compatibility shape used by existing callers.
        self._require_analysis_results = (
            run_analysis is not None and legacy_run_analysis is not None
        )

    def _queue_available(self) -> bool:
        if self._queue_installed is None:
            with self._engine.connect() as connection:
                self._queue_installed = (
                    connection.scalar(text("SELECT to_regclass('workspace.analysis_run')"))
                    is not None
                )
        return self._queue_installed

    async def enqueue_analysis(self, case_id: int) -> str:
        if not self._queue_available():
            if self._legacy is None:
                raise RuntimeError(
                    "workspace.analysis_run is not installed on this database"
                )
            return await self._legacy.enqueue_analysis(case_id)

        run_id = enqueue(self._engine, case_id)
        self.dispatch(run_id)
        return str(run_id)

    def dispatch(self, run_id: UUID) -> None:
        """이미 ``queued`` 인 행 하나를 프로세스 안 실행기에 넘긴다.

        Edge 업로드 경로(RUN-03)는 케이스 생성과 큐 등록을 자기 트랜잭션
        안에서 이미 끝냈다. 그 경로가 부를 것은 삽입이 아니라 실행뿐이다.
        별도 워커 프로세스를 띄우면 이 메서드는 아무것도 하지 않게 된다.
        """
        if self._run_analysis is None:
            return
        task = asyncio.create_task(
            run_once(
                self._engine,
                self._run_analysis,
                worker_id=self._worker_id,
                require_analysis_results=self._require_analysis_results,
                object_storage=self._object_storage,
                pdf_renderer=self._pdf_renderer,
            ),
            name=f"analysis-run-{run_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self) -> None:
        if self._legacy is not None:
            await self._legacy.shutdown()
        if not self._tasks:
            return
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
