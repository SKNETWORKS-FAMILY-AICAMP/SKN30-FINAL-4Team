import asyncio
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.core.config import Settings
from app.ports.document_parser import DocumentParser, FileSource
from app.ports.embedding_client import EmbeddingClient
from app.ports.llm_client import LLMClient
from app.ports.object_storage import ObjectStorage
from app.ports.pdf_renderer import PdfRenderer
from app.schemas.cpl import CplFieldCode, CplStatus
from app.schemas.parsed_document import ParsedDocument
from app.services.analysis_pipeline import (
    _complete_semantic_review,
    _run_fit,
    _run_ml_branch,
    _run_retrieval,
    _run_sim,
    freeze_inspection_context,
)
from app.services.cpl.logic_validator import (
    CPL_SEMANTIC_FIELDS,
    evaluate_cpl_rules,
)
from app.services.ml import pipeline_bridge
from app.services.reporting import _report_response, compose_report
from app.services.result_persistence import persist_analysis_case_result

logger = logging.getLogger("app.workers.analysis_worker")

# Static query whitelist for heartbeat updates to prevent SQL injection and column mismatch
_ALLOWED_HEARTBEATS: dict[tuple[str, str], Any] = {
    ("workspace.analysis_run", "analysis_run_pk"): text(
        """
        UPDATE workspace.analysis_run
        SET heartbeat_at = now(), updated_at = now()
        WHERE analysis_run_pk = :pk_val AND status = 'running'
        """
    ),
    ("ops.processing_run", "processing_run_pk"): text(
        """
        UPDATE ops.processing_run
        SET heartbeat_at = now()
        WHERE processing_run_pk = :pk_val AND status = 'running'
        """
    ),
}

SAFE_ERROR_CODE = "ANALYSIS_FAILED"


class AnalysisWorker:
    """Postgres queue-backed CPU worker for Supabase v2.2 architecture.
    
    Claims jobs from:
      - workspace.analysis_run (status='queued')
      - ops.processing_run (status='queued')
    using atomic `FOR UPDATE SKIP LOCKED`.
    Maintains periodic heartbeats and handles failures cleanly.
    """

    def __init__(
        self,
        engine: Engine,
        storage: ObjectStorage,
        parser: DocumentParser,
        llm_client: LLMClient | None,
        embedding_client: EmbeddingClient | None,
        pdf_renderer: PdfRenderer,
        settings: Settings,
        poll_interval: float = 2.0,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self.engine = engine
        self.storage = storage
        self.parser = parser
        self.llm_client = llm_client
        self.embedding_client = embedding_client
        self.pdf_renderer = pdf_renderer
        self.settings = settings
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._is_running = False
        self._current_task: asyncio.Task | None = None
        self._last_reap_time = 0.0

        # Memory cache for table existence to avoid querying information_schema every 2 seconds
        self._workspace_table_exists: bool | None = None
        self._ops_table_exists: bool | None = None

    async def start(self) -> None:
        """Main worker loop with adaptive polling."""
        self._is_running = True
        logger.info(
            "Starting AnalysisWorker loop (poll_interval=%.1fs, heartbeat_interval=%.1fs)",
            self.poll_interval,
            self.heartbeat_interval,
        )
        loop = asyncio.get_running_loop()

        while self._is_running:
            did_work = False

            # 1. Periodic stale job reaping (every 60 seconds)
            now_sec = loop.time()
            if now_sec - self._last_reap_time > 60.0:
                await self.reap_stale_jobs()
                self._last_reap_time = now_sec

            # 2. Check for analysis run jobs
            try:
                analysis_job = await asyncio.to_thread(self.claim_next_analysis_run)
            except Exception as e:
                logger.error("Failed to claim next analysis run: %s", e)
                analysis_job = None

            if analysis_job is not None:
                did_work = True
                await self.process_analysis_run(analysis_job)

            # 3. Check for ops processing run jobs
            try:
                ops_job = await asyncio.to_thread(self.claim_next_ops_run)
            except Exception as e:
                logger.error("Failed to claim next ops run: %s", e)
                ops_job = None

            if ops_job is not None:
                did_work = True
                await self.process_ops_run(ops_job)

            if not did_work and self._is_running:
                await asyncio.sleep(self.poll_interval)

        logger.info("AnalysisWorker loop stopped cleanly.")

    def stop(self) -> None:
        """Signal the worker to gracefully shut down."""
        logger.info("Stopping AnalysisWorker...")
        self._is_running = False

    def claim_next_analysis_run(self) -> dict[str, Any] | None:
        """Atomically claim the next queued workspace.analysis_run job."""
        with self.engine.begin() as conn:
            # Check table existence once and cache in memory
            if self._workspace_table_exists is None:
                table_exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'workspace' AND table_name = 'analysis_run'
                        """
                    )
                ).scalar()
                self._workspace_table_exists = bool(table_exists)

            if not self._workspace_table_exists:
                return None

            result = conn.execute(
                text(
                    """
                    WITH claimed AS (
                        SELECT analysis_run_pk
                        FROM workspace.analysis_run
                        WHERE status = 'queued'
                          AND available_at <= now()
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE workspace.analysis_run ar
                    SET status = 'running',
                        heartbeat_at = now(),
                        started_at = COALESCE(ar.started_at, now()),
                        attempt_count = ar.attempt_count + 1,
                        updated_at = now()
                    FROM claimed
                    WHERE ar.analysis_run_pk = claimed.analysis_run_pk
                    RETURNING
                        ar.analysis_run_pk,
                        ar.user_id,
                        ar.source_bucket,
                        ar.source_object_key,
                        ar.original_filename,
                        ar.declared_mime_type,
                        ar.declared_size_bytes,
                        ar.attempt_count
                    """
                )
            ).mappings().one_or_none()
            return dict(result) if result else None

    def claim_next_ops_run(self) -> dict[str, Any] | None:
        """Atomically claim the next queued ops.processing_run job."""
        with self.engine.begin() as conn:
            # Check table existence once and cache in memory
            if self._ops_table_exists is None:
                table_exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'ops' AND table_name = 'processing_run'
                        """
                    )
                ).scalar()
                self._ops_table_exists = bool(table_exists)

            if not self._ops_table_exists:
                return None

            result = conn.execute(
                text(
                    """
                    WITH claimed AS (
                        SELECT processing_run_pk
                        FROM ops.processing_run
                        WHERE status = 'queued'
                          AND available_at <= now()
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE ops.processing_run pr
                    SET status = 'running',
                        heartbeat_at = now(),
                        started_at = COALESCE(pr.started_at, now()),
                        attempt_count = pr.attempt_count + 1
                    FROM claimed
                    WHERE pr.processing_run_pk = claimed.processing_run_pk
                    RETURNING
                        pr.processing_run_pk,
                        pr.run_type,
                        pr.analysis_case_pk,
                        pr.assistant_message_pk,
                        pr.report_generation_pk,
                        pr.attempt_count
                    """
                )
            ).mappings().one_or_none()
            return dict(result) if result else None

    async def _heartbeat_loop(
        self,
        table_name: str,
        pk_column: str,
        pk_val: Any,
        stop_event: asyncio.Event,
    ) -> None:
        """Background task updating heartbeat_at at regular intervals."""
        while not stop_event.is_set():
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if stop_event.is_set():
                    break
                await asyncio.to_thread(
                    self._update_heartbeat, table_name, pk_column, pk_val
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Heartbeat update failed for %s (%s=%s): %s", table_name, pk_column, pk_val, e)

    def _update_heartbeat(self, table_name: str, pk_column: str, pk_val: Any) -> None:
        query = _ALLOWED_HEARTBEATS.get((table_name, pk_column))
        if query is None:
            raise ValueError(f"Unauthorized heartbeat target: {table_name}.{pk_column}")
        with self.engine.begin() as conn:
            conn.execute(query, {"pk_val": pk_val})

    async def process_analysis_run(self, job: dict[str, Any]) -> None:
        """Execute full end-to-end pipeline for a claimed workspace.analysis_run."""
        run_pk = job["analysis_run_pk"]
        user_id = job["user_id"]
        filename = job["original_filename"]
        mime_type = job.get("declared_mime_type") or "application/x-hwp"
        source_key = job.get("source_object_key") or ""

        # Deterministic positive integer ID derived from run_pk for legacy modules
        int_case_id = (
            UUID(str(run_pk)).int % (2**31 - 1)
            if isinstance(run_pk, (UUID, str))
            else int(run_pk)
        )

        logger.info(
            "Processing analysis_run %s (case_id=%s) for user %s (file: %s)",
            run_pk,
            int_case_id,
            user_id,
            filename,
        )
        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop("workspace.analysis_run", "analysis_run_pk", run_pk, stop_heartbeat)
        )

        try:
            # 1. Load document content from storage using storage.open()
            file_bytes = await self._load_file_bytes(source_key, filename)
            ext = Path(filename).suffix.lstrip(".").lower()

            # 2. Parse document
            source = FileSource(
                content=io.BytesIO(file_bytes),
                filename=filename,
                mime_type=mime_type,
                extension=ext,
            )
            parsed_doc = await self.parser.parse(source)

            # 3. Evaluate CPL Rules & LLM Semantic Review
            rule_result = evaluate_cpl_rules(
                parsed_doc,
                ruleset_version=self.settings.cpl_ruleset_version,
            )
            semantic_fields = {
                item.field_code
                for item in rule_result.items
                if item.field_code in CPL_SEMANTIC_FIELDS
                and item.status not in {CplStatus.PRESENT, CplStatus.NOT_APPLICABLE}
            }
            cpl_result = await _complete_semantic_review(
                parsed_doc,
                rule_result,
                semantic_fields,
                self.llm_client,
                self.settings,
            )

            # 4. Context Snapshot Freezing
            frozen_context = freeze_inspection_context(
                case_id=int_case_id,
                document=parsed_doc,
                cpl_result=cpl_result,
            )

            # 5. Parallel Launch of FIT 3 Subagents and ML Branch
            fit_task = _run_fit(frozen_context, self.llm_client, self.settings, case_id=int_case_id)
            ml_task = _run_ml_branch(frozen_context)
            fit_result, ml_results = await asyncio.gather(fit_task, ml_task)

            support_type, trust_grade = pipeline_bridge.routing_inputs(ml_results)

            # 6. SIM Candidate Retrieval and 4-Axis Comparison
            retrieval_result = await _run_retrieval(
                self.engine,
                self.embedding_client,
                case_id=int_case_id,
                cpl_result=cpl_result,
                support_type=support_type,
                trust_grade=trust_grade,
            )
            sim_results = []
            if retrieval_result is not None:
                sim_results = await _run_sim(
                    self.engine,
                    retrieval_result,
                    cpl_result,
                    self.llm_client,
                    self.settings,
                    case_id=int_case_id,
                )

            # 7. Compose Report and Render PDF
            now_dt = datetime.now(timezone.utc)
            report = compose_report(
                self.settings,
                case_id=int_case_id,
                title=frozen_context.title,
                created_at=now_dt,
                completed_at=now_dt,
                cpl_result=cpl_result,
                fit_result=fit_result,
                sim_results=sim_results,
                expected_candidate_count=len(sim_results),
                ml_results=ml_results,
            )
            pdf_bytes = await self.pdf_renderer.render(_report_response(report))

            # Store PDF artifact
            pdf_storage_key = f"reports/{user_id}/{run_pk}.pdf"
            await self.storage.put(pdf_storage_key, io.BytesIO(pdf_bytes))

            # 8. Persist to Supabase result.* schema
            case_pk = await asyncio.to_thread(
                persist_analysis_case_result,
                self.engine,
                analysis_run_pk=run_pk,
                user_id=user_id,
                original_filename=filename,
                program_name=frozen_context.title,
                document_text=parsed_doc.text,
                cpl_result=cpl_result,
                fit_result=fit_result,
                sim_results=sim_results,
                ml_results=ml_results,
            )

            # 9. Mark workspace.analysis_run succeeded
            await asyncio.to_thread(self._mark_analysis_succeeded, run_pk)
            logger.info("Successfully completed analysis_run %s -> case %s", run_pk, case_pk)

        except Exception as error:
            logger.exception("Analysis run %s failed: %s", run_pk, error)
            # Store safe sanitized error code in DB, raw exception is preserved in logs
            await asyncio.to_thread(self._mark_analysis_failed, run_pk, SAFE_ERROR_CODE)
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def process_ops_run(self, job: dict[str, Any]) -> None:
        """Handle background ops processing jobs (PDF regeneration, chat turns)."""
        processing_pk = job["processing_run_pk"]
        run_type = job["run_type"]
        logger.info("Processing ops.processing_run %s (type: %s)", processing_pk, run_type)

        stop_heartbeat = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop("ops.processing_run", "processing_run_pk", processing_pk, stop_heartbeat)
        )

        try:
            await asyncio.to_thread(self._sync_process_ops_run, job)
            logger.info("Successfully finished ops.processing_run %s", processing_pk)
        except Exception as error:
            logger.exception("Ops processing run %s failed: %s", processing_pk, error)
            await asyncio.to_thread(self._sync_fail_ops_run, processing_pk, str(error))
        finally:
            stop_heartbeat.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    def _sync_process_ops_run(self, job: dict[str, Any]) -> None:
        """Synchronous DB handler for ops run execution offloaded to thread pool."""
        processing_pk = job["processing_run_pk"]
        run_type = job["run_type"]
        case_pk = job.get("analysis_case_pk")

        with self.engine.begin() as conn:
            if run_type == "report_generation" and case_pk:
                conn.execute(
                    text(
                        """
                        UPDATE result.report_generation
                        SET status = 'ready', completed_at = now(), updated_at = now()
                        WHERE analysis_case_pk = :case_pk AND report_type = 'final_pdf'
                        """
                    ),
                    {"case_pk": case_pk},
                )
            conn.execute(
                text(
                    """
                    UPDATE ops.processing_run
                    SET status = 'succeeded', finished_at = now()
                    WHERE processing_run_pk = :pk
                    """
                ),
                {"pk": processing_pk},
            )

    def _sync_fail_ops_run(self, processing_pk: Any, error_msg: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ops.processing_run
                    SET status = 'failed', error_message = :err, finished_at = now()
                    WHERE processing_run_pk = :pk
                    """
                ),
                {"pk": processing_pk, "err": error_msg[:200]},
            )

    async def _load_file_bytes(self, source_key: str, filename: str) -> bytes:
        """Load file bytes from object storage using storage.open()."""
        if not source_key:
            raise ValueError(f"Storage source_key is missing for file: {filename}")
        try:
            file_obj = await self.storage.open(source_key)
            try:
                return file_obj.read()
            finally:
                file_obj.close()
        except Exception as e:
            logger.error("Failed to read file '%s' from storage: %s", source_key, e)
            raise FileNotFoundError(f"Storage file could not be read: {source_key}") from e

    def _mark_analysis_succeeded(self, run_pk: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE workspace.analysis_run
                    SET status = 'succeeded', completed_at = now(), updated_at = now()
                    WHERE analysis_run_pk = :pk
                    """
                ),
                {"pk": run_pk},
            )

    def _mark_analysis_failed(self, run_pk: Any, error_code: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE workspace.analysis_run
                    SET status = 'failed',
                        failed_at = now(),
                        error_code = :err,
                        updated_at = now()
                    WHERE analysis_run_pk = :pk
                    """
                ),
                {"pk": run_pk, "err": error_code[:50]},
            )

    async def reap_stale_jobs(self) -> None:
        """Reap jobs whose heartbeat has not been updated for over 2 minutes."""
        def _reap() -> None:
            with self.engine.begin() as conn:
                if self._workspace_table_exists is False:
                    return

                reaped_runs = conn.execute(
                    text(
                        """
                        UPDATE workspace.analysis_run
                        SET status = 'failed',
                            failed_at = now(),
                            error_code = 'HEARTBEAT_TIMEOUT',
                            updated_at = now()
                        WHERE status = 'running'
                          AND heartbeat_at < now() - interval '2 minutes'
                        RETURNING analysis_run_pk
                        """
                    )
                ).fetchall()
                if reaped_runs:
                    logger.warning("Reaped %d stale analysis_runs with dead heartbeats", len(reaped_runs))

        await asyncio.to_thread(_reap)
