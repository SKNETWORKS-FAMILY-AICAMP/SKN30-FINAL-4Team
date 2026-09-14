#!/usr/bin/env python3
"""Run one local self-hosted Auth -> API -> Storage/DB -> worker E2E.

This operator-only smoke test reads secrets from ``backend/.env`` and the
local Supabase Compose ``.env``.  It never prints those
values, the generated password, uploaded bytes, or full model output.  The
created test user and analysis result are intentionally retained for Studio
inspection.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import psycopg
from dotenv import dotenv_values
from psycopg.rows import dict_row


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_SOURCE = (
    REPOSITORY_ROOT
    / "docs"
    / "pre_review_request_e2e_5_20260909_v1"
    / "generated"
    / "01_유니콘브릿지_기술금융_사전협의요청서.hwpx"
)
DEFAULT_BACKEND_ENV = BACKEND_ROOT / ".env"
DEFAULT_SUPABASE_ENV = REPOSITORY_ROOT / ".runtime" / "supabase-dev" / ".env"
TEST_ORIGIN = "http://e2e.local"
# Deployment default, and the retrieval breadth the stored E2E traces were
# produced with. Keep live runs comparable to those artifacts.
DEFAULT_TOP_K = 5
SOURCE_MIME_TYPES = {
    ".hwp": "application/x-hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
}
MAX_WORKER_ATTEMPTS = 2
ANALYSIS_POLL_INTERVAL_SECONDS = 0.5
ANALYSIS_POLL_TIMEOUT_SECONDS = 1800
CHAT_POLL_INTERVAL_SECONDS = 0.25
CHAT_POLL_TIMEOUT_SECONDS = 600
E2E_CHAT_QUESTION = "이번 분석 결과를 요약하고 근거를 알려주세요."
_BUILD_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
ML_ENVIRONMENT_KEYS = (
    "PREREVIEW_ML_ROOT",
    "PREREVIEW_MODEL1_SERVING_DIR",
    "PREREVIEW_ML_PYTHON_EXECUTABLE",
    "PREREVIEW_ML_TIMEOUT_SECONDS",
)
OPTIONAL_STAGE_MODEL_ENVIRONMENT_KEYS = (
    "OPENAI_REQUEST_PROFILE_MODEL",
    "OPENAI_FIT_MODEL",
    "OPENAI_SIM_MODEL",
    "OPENAI_CHAT_MODEL",
)
CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY = "PREREVIEW_CURSOR_SIGNING_SECRET"
MODEL_CONFIGURATION_ENVIRONMENT_KEYS = (
    "OPENAI_LLM_MODEL",
    "OPENAI_EMBEDDING_MODEL",
    *OPTIONAL_STAGE_MODEL_ENVIRONMENT_KEYS,
    "OPENAI_MAX_REPAIRS",
)

_CLAIM_NEXT_RUN_SQL = """
SELECT
    analysis_run_pk,
    source_bucket,
    source_object_key,
    source_content_sha256,
    processing_run_pk,
    attempt_count,
    lease_expires_at,
    heartbeat_interval_seconds
FROM workspace.claim_next_analysis_run(%s, %s)
"""
_E2E_QUEUE_ADVISORY_LOCK = 7_612_330_025

_CLAIM_NEXT_CHAT_MESSAGE_SQL = """
SELECT
    assistant_message_id,
    analysis_case_id,
    analysis_session_id,
    user_message_id,
    owner_id,
    question,
    result_payload,
    conversation,
    processing_run_pk,
    attempt_count,
    lease_expires_at,
    heartbeat_interval_seconds
FROM workspace.claim_next_conversation_message_v2(%s, %s)
"""
_CHAT_REFERENCE_COUNTS_SQL = """
SELECT
    count(*)::integer AS reference_count,
    count(*) FILTER (
        WHERE e.analysis_case_pk = %s::uuid
          AND e.usage_scope IN ('RESULT', 'CONVERSATION')
    )::integer AS valid_reference_count
FROM result.conversation_reference AS r
LEFT JOIN result.evidence_snapshot AS e
  ON e.evidence_snapshot_pk = r.evidence_snapshot_pk
WHERE r.message_pk = %s::uuid
"""
_QUEUE_QUIESCENCE_SQL = """
SELECT
    (
        SELECT count(*)::integer
        FROM workspace.analysis_run
        WHERE status IN ('uploading', 'queued', 'running', 'cleanup_pending')
    ) AS active_analysis_runs,
    (
        SELECT count(*)::integer
        FROM result.conversation_message
        WHERE role = 'assistant' AND status = 'generating'
    ) AS active_chat_messages
"""
_ML_RESULT_SQL = """
SELECT ml_result
FROM result.analysis_case
WHERE analysis_case_pk = %s::uuid
"""
_ANALYSIS_EMBEDDING_CONFIGURATION_SQL = """
SELECT processing.run_metadata -> 'embedding_configuration' AS provenance
FROM ops.processing_run AS processing
WHERE processing.source_analysis_run_id = %s::uuid
  AND processing.run_type = 'analysis'
  AND processing.status = 'succeeded'
ORDER BY processing.finished_at DESC NULLS LAST,
         processing.created_at DESC, processing.processing_run_pk DESC
LIMIT 2
"""
_ANALYSIS_WORKER_AUDIT_SQL = """
SELECT
    dispatch.attempt_count,
    processing.status,
    processing.run_metadata
FROM workspace.analysis_run_dispatch AS dispatch
JOIN ops.processing_run AS processing
  ON processing.source_analysis_run_id = dispatch.analysis_run_pk
 AND processing.run_type = 'analysis'
WHERE dispatch.analysis_run_pk = %s::uuid
ORDER BY processing.started_at, processing.created_at, processing.processing_run_pk
"""
_CHAT_WORKER_AUDIT_SQL = """
SELECT
    dispatch.attempt_count AS current_cycle_attempt_count,
    message.auto_retry_count,
    message.manual_retry_count,
    processing.status,
    processing.run_metadata
FROM workspace.conversation_message_dispatch AS dispatch
JOIN result.conversation_message AS message
  ON message.message_pk = dispatch.assistant_message_pk
JOIN ops.processing_run AS processing
  ON processing.run_type = 'chat'
 AND processing.run_metadata ->> 'assistant_message_id'
     = dispatch.assistant_message_pk::text
 AND processing.run_metadata ->> 'analysis_case_id'
     = dispatch.analysis_case_pk::text
WHERE dispatch.assistant_message_pk = %s::uuid
  AND dispatch.analysis_case_pk = %s::uuid
ORDER BY processing.started_at, processing.created_at, processing.processing_run_pk
"""
_E2E_CHAT_QUEUE_ADVISORY_LOCK = 7_612_330_026


class E2EFailure(RuntimeError):
    """A safe stage-level failure that never contains credentials or content."""


class _UnexpectedClaim(E2EFailure):
    """The guarded queue function selected a run other than this E2E's run."""


class _UnexpectedChatClaim(E2EFailure):
    """The guarded chat queue function selected a different assistant message."""


@dataclass(frozen=True, slots=True)
class _WorkerAttemptAudit:
    """Small, non-secret summary of durable worker-attempt audit rows."""

    final_worker_id: str
    attempt_count: int
    attempt_worker_ids: tuple[str, ...]


def _claim_value(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise E2EFailure("Local E2E queue returned an incomplete claim")
    return value


def _acquire_target_claim_guard(cursor: Any) -> None:
    """Serialize the short-lived claim transaction between E2E operators.

    Production workers do not honor this advisory lock.  Queue safety instead
    comes from the explicit empty-queue precondition plus verifying the
    migration-owned claim before commit.  If another producer races the E2E,
    an unexpected claim is rolled back rather than consuming its attempt.
    """

    try:
        # Connection timeout does not bound waits after connecting.  Fail
        # clearly instead of leaving this operator command hung behind another
        # worker or an abandoned transaction.
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        # Serialise concurrent copies of this operator-only script.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_E2E_QUEUE_ADVISORY_LOCK,),
        )
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E queue isolation is unavailable") from None


def _claim_target_analysis_run(
    database_url: str,
    target_run_id: str,
    *,
    worker_id: str,
    lease_seconds: int,
) -> Any:
    """Claim and verify the E2E run before the claim transaction commits."""

    # Import after this script has added BACKEND_ROOT to sys.path.  Keeping the
    # concrete queue value local avoids broadening the production repository
    # protocol with an operator-only targeted-claim method.
    from worker.runtime import ClaimedJob

    try:
        connection = psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        )
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E queue isolation is unavailable") from None

    try:
        with connection:
            with connection.cursor() as cursor:
                _acquire_target_claim_guard(cursor)
                # This is intentionally outside the guard-acquisition
                # exception boundary.  A failed claim/commit is not evidence
                # that queue isolation was unavailable, so it receives its
                # own safe stage classification below.
                cursor.execute(_CLAIM_NEXT_RUN_SQL, (worker_id, lease_seconds))
                row = cursor.fetchone()
                if row is None:
                    return None
                if not isinstance(row, Mapping):
                    raise E2EFailure("Local E2E queue returned an invalid claim")
                job_pk = _claim_value(row, "analysis_run_pk")
                if str(job_pk) != target_run_id:
                    # Raising before the connection context exits rolls back
                    # claim_next_analysis_run's attempt/status mutations.
                    raise _UnexpectedClaim(
                        "Local E2E worker claimed an unexpected analysis run"
                    )
                attempt_count = _claim_value(row, "attempt_count")
                if not isinstance(attempt_count, int) or isinstance(
                    attempt_count, bool
                ):
                    raise E2EFailure("Local E2E queue returned an invalid claim")
                return ClaimedJob(
                    job_pk=job_pk,
                    processing_run_pk=_claim_value(row, "processing_run_pk"),
                    payload={
                        "source_bucket": _claim_value(row, "source_bucket"),
                        "source_object_key": _claim_value(
                            row, "source_object_key"
                        ),
                        "source_content_sha256": _claim_value(
                            row, "source_content_sha256"
                        ),
                        "attempt_count": attempt_count,
                        "lease_expires_at": _claim_value(
                            row, "lease_expires_at"
                        ),
                        "heartbeat_interval_seconds": _claim_value(
                            row, "heartbeat_interval_seconds"
                        ),
                    },
                )
    except E2EFailure:
        raise
    except (psycopg.Error, OSError):
        # WorkerRuntime logs repository exceptions.  Replace driver details
        # before that boundary so a DSN or SQL fragment can never reach the
        # operator log, while keeping this distinct from guard acquisition.
        raise E2EFailure("Local E2E queue claim failed") from None


class _TargetRunRepository:
    """Restrict a normal worker runtime to one E2E-created analysis run."""

    def __init__(
        self,
        delegate: Any,
        *,
        target_run_id: str,
        database_url: str,
        claim_target: Callable[..., Any] | None = None,
    ) -> None:
        self._delegate = delegate
        self._target_run_id = target_run_id
        self._claim_target = claim_target or (
            lambda **values: _claim_target_analysis_run(
                database_url,
                target_run_id,
                **values,
            )
        )
        self.target_claims = 0
        self.unexpected_claim = False
        self.claim_error: E2EFailure | None = None

    def claim(self, *, worker_id: str, lease_seconds: int) -> Any:
        try:
            job = self._claim_target(
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
        except E2EFailure as error:
            # WorkerRuntime deliberately converts repository exceptions into
            # an unavailable outcome.  Retain only this safe stage-level
            # message so the operator sees the actual isolation failure.
            self.claim_error = error
            if isinstance(error, _UnexpectedClaim):
                self.unexpected_claim = True
            raise
        if job is None:
            return None
        if str(job.job_pk) != self._target_run_id:
            # A custom/in-memory claim implementation still gets the same
            # fail-closed boundary.  The default PostgreSQL implementation has
            # already performed this check before committing its transaction.
            self.unexpected_claim = True
            raise E2EFailure("Local E2E worker claimed an unexpected analysis run")
        self.target_claims += 1
        return job

    def heartbeat(self, **kwargs: Any) -> bool:
        return self._delegate.heartbeat(**kwargs)

    def complete(self, **kwargs: Any) -> bool:
        return self._delegate.complete(**kwargs)

    def fail(self, **kwargs: Any) -> bool:
        return self._delegate.fail(**kwargs)


def _chat_uuid(row: Mapping[str, Any], key: str) -> UUID:
    value = _claim_value(row, key)
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise E2EFailure("Local E2E chat queue returned an invalid claim") from None


def _claim_target_chat_message(
    database_url: str,
    target_assistant_message_id: str,
    *,
    worker_id: str,
    lease_seconds: int,
) -> Any:
    """Claim exactly one API-created assistant message, or fail closed.

    The normal chat repository intentionally has no targeted production claim
    API.  The operator command requires both queues to be empty before it
    starts.  This transaction serialises concurrent E2E operators, calls the
    migration-owned claim function, and verifies the returned row before
    commit.  A racing unrelated claim is therefore rolled back fail-closed.
    """

    from worker.runtime import ClaimedJob

    try:
        connection = psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        )
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E chat queue isolation is unavailable") from None

    try:
        with connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                    cursor.execute("SET LOCAL statement_timeout = '15s'")
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_E2E_CHAT_QUEUE_ADVISORY_LOCK,),
                    )
                except (psycopg.Error, OSError):
                    raise E2EFailure(
                        "Local E2E chat queue isolation is unavailable"
                    ) from None

                cursor.execute(
                    _CLAIM_NEXT_CHAT_MESSAGE_SQL, (worker_id, lease_seconds)
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                if not isinstance(row, Mapping):
                    raise E2EFailure("Local E2E chat queue returned an invalid claim")
                assistant_message_id = _chat_uuid(row, "assistant_message_id")
                if str(assistant_message_id) != target_assistant_message_id:
                    # Roll back the migration claim before it can consume a
                    # real user's attempt.
                    raise _UnexpectedChatClaim(
                        "Local E2E worker claimed an unexpected chat message"
                    )
                attempt_count = _claim_value(row, "attempt_count")
                heartbeat_interval = _claim_value(
                    row, "heartbeat_interval_seconds"
                )
                result_payload = _claim_value(row, "result_payload")
                conversation = row.get("conversation")
                if conversation is None:
                    conversation = []
                if (
                    not isinstance(attempt_count, int)
                    or isinstance(attempt_count, bool)
                    or not isinstance(heartbeat_interval, int)
                    or isinstance(heartbeat_interval, bool)
                    or not isinstance(result_payload, Mapping)
                    or not isinstance(conversation, list)
                ):
                    raise E2EFailure("Local E2E chat queue returned an invalid claim")
                return ClaimedJob(
                    job_pk=assistant_message_id,
                    processing_run_pk=_chat_uuid(row, "processing_run_pk"),
                    payload={
                        "question": _claim_value(row, "question"),
                        "result_payload": dict(result_payload),
                        "conversation": conversation,
                        "analysis_case_id": _chat_uuid(row, "analysis_case_id"),
                        "analysis_session_id": _chat_uuid(
                            row, "analysis_session_id"
                        ),
                        "user_message_id": _chat_uuid(row, "user_message_id"),
                        "owner_id": _chat_uuid(row, "owner_id"),
                        "attempt_count": attempt_count,
                        "lease_expires_at": _claim_value(row, "lease_expires_at"),
                        "heartbeat_interval_seconds": heartbeat_interval,
                    },
                )
    except E2EFailure:
        raise
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E chat queue claim failed") from None


class _TargetChatRepository:
    """Limit one normal chat runtime invocation to the E2E assistant row."""

    def __init__(
        self,
        delegate: Any,
        *,
        target_assistant_message_id: str,
        database_url: str,
        claim_target: Callable[..., Any] | None = None,
    ) -> None:
        self._delegate = delegate
        self._target_assistant_message_id = target_assistant_message_id
        self._claim_target = claim_target or (
            lambda **values: _claim_target_chat_message(
                database_url,
                target_assistant_message_id,
                **values,
            )
        )
        self.target_claims = 0
        self.unexpected_claim = False
        self.claim_error: E2EFailure | None = None

    def claim(self, *, worker_id: str, lease_seconds: int) -> Any:
        try:
            job = self._claim_target(worker_id=worker_id, lease_seconds=lease_seconds)
        except E2EFailure as error:
            self.claim_error = error
            if isinstance(error, _UnexpectedChatClaim):
                self.unexpected_claim = True
            raise
        if job is None:
            return None
        if str(job.job_pk) != self._target_assistant_message_id:
            self.unexpected_claim = True
            raise E2EFailure("Local E2E worker claimed an unexpected chat message")
        self.target_claims += 1
        return job

    def heartbeat(self, **kwargs: Any) -> bool:
        return self._delegate.heartbeat(**kwargs)

    def complete(self, **kwargs: Any) -> bool:
        return self._delegate.complete(**kwargs)

    def fail(self, **kwargs: Any) -> bool:
        return self._delegate.fail(**kwargs)


def _source_mime_type(source: Path) -> str:
    try:
        return SOURCE_MIME_TYPES[source.suffix.lower()]
    except KeyError:
        raise E2EFailure("--file must be an HWP or HWPX file") from None


def _validated_source(source: Path) -> tuple[bytes, str]:
    """Read and validate the exact HWP/HWPX bytes that will be uploaded."""

    content = source.read_bytes()
    from app.models.pipeline import PipelineKind
    from app.pipelines.formats import validate_format

    try:
        decision = validate_format(PipelineKind.REQUEST, source, content=content)
    except ValueError as error:
        raise E2EFailure(f"--file format is invalid: {error}") from None
    return content, decision.mime_type


def _git_commit() -> str:
    """Return the exact checked-out commit without exposing worktree paths."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise E2EFailure("Git revision identity is unavailable") from None
    revision = completed.stdout.strip().lower()
    if completed.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", revision) is None:
        raise E2EFailure("Git revision identity is unavailable")
    return revision


def _git_source_state(revision: str) -> tuple[bool, str]:
    """Return dirty state and a content-safe fingerprint of the source tree.

    The fingerprint includes the complete tracked diff plus untracked file
    metadata and content digests.  It is deliberately a digest only: neither
    source paths nor source contents are persisted in the execution manifest.
    """

    def git_bytes(*arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise E2EFailure("Git source state identity is unavailable") from None
        if completed.returncode != 0:
            raise E2EFailure("Git source state identity is unavailable")
        return completed.stdout

    status = git_bytes("status", "--porcelain=v1", "-z")
    tracked_diff = git_bytes(
        "diff", "--no-ext-diff", "--binary", "--full-index", "HEAD"
    )
    untracked = git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    source_state = sha256()
    source_state.update(b"git-source-state-v1\0")
    source_state.update(revision.encode("ascii"))
    source_state.update(b"\0tracked-diff\0")
    source_state.update(sha256(tracked_diff).digest())

    for raw_path in sorted(path for path in untracked.split(b"\0") if path):
        # Git paths are repository-relative.  Reject a surprising value before
        # reading it, even though git normally guarantees this invariant.
        if raw_path.startswith(b"/") or b".." in raw_path.split(b"/"):
            raise E2EFailure("Git source state identity is unavailable")
        path = REPOSITORY_ROOT / os.fsdecode(raw_path)
        try:
            path_stat = path.lstat()
            if stat.S_ISREG(path_stat.st_mode):
                content_digest = sha256(path.read_bytes()).digest()
                kind = b"regular"
            elif stat.S_ISLNK(path_stat.st_mode):
                content_digest = sha256(os.fsencode(os.readlink(path))).digest()
                kind = b"symlink"
            else:
                raise OSError("unsupported untracked file type")
        except OSError:
            raise E2EFailure("Git source state identity is unavailable") from None
        source_state.update(b"\0untracked\0")
        source_state.update(raw_path)
        source_state.update(b"\0")
        source_state.update(kind)
        source_state.update(f"{stat.S_IMODE(path_stat.st_mode):o}".encode("ascii"))
        source_state.update(content_digest)
    return bool(status), source_state.hexdigest()


def _require_clean_checkout_build_context_digest() -> str:
    """Return this clean checkout's independently computed Docker identity."""

    revision = _git_commit()
    dirty, _ = _git_source_state(revision)
    if dirty:
        raise E2EFailure(
            "external release E2E requires a clean Git checkout; commit or discard local changes first"
        )
    try:
        from scripts.build_context_digest import backend_build_context_digest

        return backend_build_context_digest(BACKEND_ROOT)
    except (OSError, ValueError):
        raise E2EFailure("Local Docker build-context identity is unavailable") from None


def _analysis_embedding_configuration(
    database_url: str,
    analysis_run_id: str,
) -> dict[str, object]:
    """Read the exact retrieval configuration snapshotted by this attempt."""

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_ANALYSIS_EMBEDDING_CONFIGURATION_SQL, (analysis_run_id,))
                rows = cursor.fetchall()
    except (psycopg.Error, OSError):
        raise E2EFailure("Analysis embedding provenance is unavailable") from None
    if len(rows) != 1:
        raise E2EFailure("Analysis embedding provenance is unavailable")
    provenance = rows[0].get("provenance")
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "selected",
        "configuration_id",
        "provider",
        "model_id",
        "dimensions",
        "max_input_tokens",
        "assembly_version",
    } or not isinstance(provenance.get("selected"), bool):
        raise E2EFailure("Analysis embedding provenance is unavailable")
    if provenance["selected"] is False:
        if any(value is not None for key, value in provenance.items() if key != "selected"):
            raise E2EFailure("Analysis embedding provenance is unavailable")
        return dict(provenance)
    try:
        UUID(str(provenance["configuration_id"]))
        dimensions = int(provenance["dimensions"])
        max_input_tokens = int(provenance["max_input_tokens"])
    except (TypeError, ValueError):
        raise E2EFailure("Analysis embedding provenance is unavailable") from None
    if (
        not isinstance(provenance["provider"], str)
        or not provenance["provider"].strip()
        or not isinstance(provenance["model_id"], str)
        or not provenance["model_id"].strip()
        or not isinstance(provenance["assembly_version"], str)
        or not provenance["assembly_version"].strip()
        or dimensions <= 0
        or max_input_tokens <= 0
    ):
        raise E2EFailure("Analysis embedding provenance is unavailable")
    return {
        "selected": True,
        "configuration_id": str(provenance["configuration_id"]),
        "provider": provenance["provider"],
        "model_id": provenance["model_id"],
        "dimensions": dimensions,
        "max_input_tokens": max_input_tokens,
        "assembly_version": provenance["assembly_version"],
    }


def _docker_container_id(worker_id: str) -> str:
    """Extract the audited Docker container id from an external worker id."""

    container_id = worker_id.split(":", 1)[0].strip()
    if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
        raise E2EFailure("External worker id does not contain a Docker container id")
    return container_id


def _docker_image_identity(worker_id: str) -> str:
    """Resolve an external worker's container hostname to its immutable image."""

    container_id = _docker_container_id(worker_id)
    try:
        completed = subprocess.run(
            ["docker", "inspect", "--format={{.Image}}", container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise E2EFailure("External worker image identity is unavailable") from None
    image_id = completed.stdout.strip().lower()
    if completed.returncode != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise E2EFailure("External worker image identity is unavailable")
    return image_id


def _external_worker_model_environment(worker_id: str) -> dict[str, str]:
    """Read just the non-secret model settings from an audited container.

    Do not replace this with ``docker inspect``: its Config.Env field exposes
    every container variable, including provider credentials.  The fixed shell
    program below expands only the allowlisted model setting names.
    """

    container_id = _docker_container_id(worker_id)
    command = "; ".join(
        f"printf '{name}=%s\\n' \"${{{name}-}}\""
        for name in MODEL_CONFIGURATION_ENVIRONMENT_KEYS
    )
    try:
        completed = subprocess.run(
            ["docker", "exec", container_id, "/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise E2EFailure("External worker model configuration is unavailable") from None
    if completed.returncode != 0:
        raise E2EFailure("External worker model configuration is unavailable")

    lines = completed.stdout.splitlines()
    if len(lines) != len(MODEL_CONFIGURATION_ENVIRONMENT_KEYS):
        raise E2EFailure("External worker model configuration is unavailable")
    environment: dict[str, str] = {}
    for name, line in zip(MODEL_CONFIGURATION_ENVIRONMENT_KEYS, lines, strict=True):
        prefix = f"{name}="
        if not line.startswith(prefix):
            raise E2EFailure("External worker model configuration is unavailable")
        value = line.removeprefix(prefix)
        if len(value) > 512 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise E2EFailure("External worker model configuration is unavailable")
        environment[name] = value
    return environment


def _model_configuration_manifest(
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return only public model identifiers, never provider credentials."""

    if environment is None:
        from worker.config import OpenAIConfig

        config = OpenAIConfig.from_env()
        return {
            "provider": "openai",
            "request_profile_model": config.llm_model_for("request_profile"),
            "cpl_model": config.llm_model,
            "fit_model": config.llm_model_for("fit"),
            "sim_model": config.llm_model_for("sim"),
            "chat_model": config.llm_model_for("chat"),
            "embedding_model": config.embedding_model,
            "max_repairs": config.max_repairs,
        }

    def required_model(name: str) -> str:
        value = str(environment.get(name) or "").strip()
        if not value:
            raise E2EFailure("External worker model configuration is unavailable")
        return value

    def stage_model(name: str, fallback: str) -> str:
        value = str(environment.get(name) or "").strip()
        return value or fallback

    cpl_model = required_model("OPENAI_LLM_MODEL")
    try:
        # This accepts the same integer/default semantics as OpenAIConfig,
        # while passing the allowlisted container mapping explicitly so no
        # process environment (and therefore no provider secret) is read.
        from worker.config import MissingConfigError, _int_from_env

        max_repairs = _int_from_env("OPENAI_MAX_REPAIRS", 2, environment)
    except MissingConfigError:
        raise E2EFailure("External worker model configuration is unavailable") from None
    return {
        "provider": "openai",
        "request_profile_model": stage_model(
            "OPENAI_REQUEST_PROFILE_MODEL", cpl_model
        ),
        "cpl_model": cpl_model,
        "fit_model": stage_model("OPENAI_FIT_MODEL", cpl_model),
        "sim_model": stage_model("OPENAI_SIM_MODEL", cpl_model),
        "chat_model": stage_model("OPENAI_CHAT_MODEL", cpl_model),
        "embedding_model": required_model("OPENAI_EMBEDDING_MODEL"),
        "max_repairs": max_repairs,
    }


def _execution_manifest(
    *,
    source_content: bytes,
    worker_mode: str,
    analysis_worker_id: str,
    chat_worker_id: str,
    database_url: str,
    analysis_run_id: str,
    deployed_api_build_id: str | None = None,
) -> dict[str, object]:
    """Build the reproducibility record required by the release E2E gate."""

    revision = _git_commit()
    git_dirty, git_source_state_sha256 = _git_source_state(revision)
    if worker_mode == "external":
        if git_dirty:
            raise E2EFailure(
                "external release E2E requires a clean Git checkout; commit or discard local changes first"
            )
        if (
            not isinstance(deployed_api_build_id, str)
            or _BUILD_ID_PATTERN.fullmatch(deployed_api_build_id) is None
        ):
            raise E2EFailure("External FastAPI build identity is unavailable")
        expected_build_id = _require_clean_checkout_build_context_digest()
        if deployed_api_build_id != expected_build_id:
            raise E2EFailure(
                "External FastAPI build identity does not match the clean checkout Docker context"
            )
        analysis_runtime: dict[str, object] = {
            "kind": "docker",
            "worker_id": analysis_worker_id,
            "image_id": _docker_image_identity(analysis_worker_id),
        }
        chat_runtime: dict[str, object] = {
            "kind": "docker",
            "worker_id": chat_worker_id,
            "image_id": _docker_image_identity(chat_worker_id),
        }
        models: dict[str, object] = {
            "analysis_worker": _model_configuration_manifest(
                _external_worker_model_environment(analysis_worker_id)
            ),
            "chat_worker": _model_configuration_manifest(
                _external_worker_model_environment(chat_worker_id)
            ),
        }
        api_runtime: dict[str, object] = {
            "kind": "deployed-http",
            "build_id": deployed_api_build_id,
        }
    else:
        common = {
            "kind": "host-python",
            "python_version": sys.version.split()[0],
            "platform": sys.platform,
            "git_commit": revision,
        }
        analysis_runtime = {**common, "worker_id": analysis_worker_id}
        chat_runtime = {**common, "worker_id": chat_worker_id}
        models = _model_configuration_manifest()
        api_runtime = {"kind": "in-process", "build_id": revision}
    return {
        "input_sha256": sha256(source_content).hexdigest(),
        "git_commit": revision,
        "git_dirty": git_dirty,
        "git_source_state_sha256": git_source_state_sha256,
        "api": api_runtime,
        "analysis_worker": analysis_runtime,
        "chat_worker": chat_runtime,
        "models": models,
        "embedding_configuration": _analysis_embedding_configuration(
            database_url,
            analysis_run_id,
        ),
    }


def _partial_execution_manifest(
    *,
    source_content: bytes,
    worker_mode: str,
    analysis_worker_id: str | None,
    database_url: str,
    analysis_run_id: str,
    deployed_api_build_id: str | None,
) -> dict[str, object]:
    """Return truthful known provenance after analysis but before chat exists.

    This path must be total: tracing a rejected analysis can never replace the
    original failure.  Unknown chat identity is represented as JSON null, not
    an empty object that could be mistaken for captured provenance.
    """

    manifest: dict[str, object] = {
        "partial": True,
        "input_sha256": sha256(source_content).hexdigest(),
        "git_commit": None,
        "git_dirty": None,
        "git_source_state_sha256": None,
        "api": {
            "kind": "deployed-http" if worker_mode == "external" else "in-process",
            "build_id": deployed_api_build_id if worker_mode == "external" else None,
        },
        "analysis_worker": {
            "kind": "docker" if worker_mode == "external" else "host-python",
            "worker_id": analysis_worker_id,
        },
        "chat_worker": None,
        "models": None,
        "embedding_configuration": None,
    }
    try:
        revision = _git_commit()
        dirty, source_state = _git_source_state(revision)
        manifest.update(
            {
                "git_commit": revision,
                "git_dirty": dirty,
                "git_source_state_sha256": source_state,
            }
        )
        if worker_mode != "external":
            manifest["api"] = {"kind": "in-process", "build_id": revision}
            manifest["models"] = _model_configuration_manifest()
    except Exception:  # noqa: BLE001 - original E2E failure takes precedence
        pass
    try:
        manifest["embedding_configuration"] = _analysis_embedding_configuration(
            database_url,
            analysis_run_id,
        )
    except Exception:  # noqa: BLE001 - result may not have persisted metadata
        pass
    return manifest


def _write_trace(
    trace_dir: Path,
    *,
    upload: object,
    common_ir: object,
    structured_profile: object,
    result: dict[str, object],
    cpl_diagnostics: object = None,
    run_state: object = None,
    execution_manifest: object = None,
) -> None:
    """Record persisted E2E artifacts; never rerun a stage just to trace it."""

    if trace_dir.exists():
        raise E2EFailure("--trace-dir must not already exist")
    trace_dir.mkdir(parents=True)
    stages = (
        ("00_upload.json", upload),
        ("01_common_ir.json", common_ir),
        ("02_structured_profile.json", structured_profile),
        ("03_cpl.json", result.get("cpl")),
        ("04_fit.json", result.get("fit")),
        ("05_sim.json", result.get("sim")),
        ("06_ml.json", result.get("ml")),
        ("07_result.json", result),
        ("08_run_state.json", run_state),
        ("09_execution_manifest.json", execution_manifest or {}),
        ("cpl_diagnostics.json", cpl_diagnostics if cpl_diagnostics is not None else []),
    )
    for name, payload in stages:
        (trace_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _cached_artifacts(handler: object, run_id: str) -> tuple[object, object]:
    """Load worker-persisted intermediate artifacts without recomputation."""

    try:
        cached = handler._store.cached_request_profile(analysis_run_id=run_id)
        if cached is None:
            return None, None
        return (
            handler._load_json_artifact(cached.common_ir),
            handler._load_json_artifact(cached.structured_profile),
        )
    except Exception:  # noqa: BLE001 - tracing must not hide the original failure
        return None, None


def _write_analysis_trace_safely(
    trace_dir: Path | None,
    *,
    upload: object,
    handler: object | None,
    run_id: str,
    run_state: object,
    cpl_diagnostics: object,
    result: dict[str, object] | None = None,
    execution_manifest: object = None,
) -> bool:
    """Best-effort trace of persisted analysis state without changing its result.

    This is deliberately usable after a terminal or retry failure.  In
    particular, it never asks the worker to rerun a stage merely to obtain an
    artifact, and any unavailable storage or filesystem must leave the E2E's
    original failure intact.
    """

    if trace_dir is None:
        return False
    common_ir: object = None
    structured_profile: object = None
    if handler is not None:
        common_ir, structured_profile = _cached_artifacts(handler, run_id)
    try:
        _write_trace(
            trace_dir,
            upload=upload,
            common_ir=common_ir,
            structured_profile=structured_profile,
            result=result if result is not None else {},
            run_state=run_state,
            cpl_diagnostics={
                "analysis_run_id": run_id,
                "diagnostics": cpl_diagnostics,
            },
            execution_manifest=execution_manifest,
        )
    except Exception:  # noqa: BLE001 - tracing must not hide the original failure
        return False
    return True


def _collect_diagnostics(
    destination: list[dict[str, object]],
    _stage: str,
    rows: object,
) -> None:
    """Copy optional worker diagnostics without letting reporting fail a job."""

    try:
        destination.extend(
            {
                "stage": row.stage,
                "unit": row.unit,
                "reason_code": row.reason_code,
                "message": row.message,
                "attempt": row.attempt,
            }
            for row in rows
        )
    except Exception:  # noqa: BLE001 - diagnostics must not change worker result
        return


def _report_analysis_stage(
    stage: str,
    status: str,
    detail: str | None,
) -> None:
    """Keep inline progress off stdout, which is reserved for summary JSON."""

    message = f"[{stage}] {status}"
    if detail:
        message += f" — {detail}"
    print(message, file=sys.stderr, flush=True)


async def _capture_analysis_failure_trace(
    *,
    client: httpx.AsyncClient,
    trace_dir: Path | None,
    upload: object,
    handler: object | None,
    run_id: str,
    cpl_diagnostics: object,
    result: dict[str, object] | None = None,
    execution_manifest: object = None,
) -> bool:
    """Read only already-persisted failure evidence, including external mode."""

    run_state: object = None
    try:
        run_state = await _analysis_state(client, run_id)
    except Exception:  # noqa: BLE001 - tracing must not hide the original failure
        pass
    return _write_analysis_trace_safely(
        trace_dir,
        upload=upload,
        handler=handler,
        run_id=run_id,
        run_state=run_state,
        cpl_diagnostics=cpl_diagnostics,
        result=result,
        execution_manifest=execution_manifest,
    )


async def _require_live_ml_results_with_failure_trace(
    *,
    client: httpx.AsyncClient,
    trace_dir: Path | None,
    upload: object,
    handler: object | None,
    run_id: str,
    cpl_diagnostics: object,
    result: dict[str, object],
    database_url: str,
    case_id: str,
    execution_manifest: object,
) -> dict[str, str]:
    """Validate persisted ML acceptance and retain evidence if it is rejected."""

    try:
        return await asyncio.to_thread(
            _require_live_ml_results,
            database_url,
            case_id=case_id,
        )
    except E2EFailure:
        # This is deliberately scoped to the post-worker ML acceptance gate.
        # The trace operation is best-effort and must not mask that rejection.
        await _capture_analysis_failure_trace(
            client=client,
            trace_dir=trace_dir,
            upload=upload,
            handler=handler,
            run_id=run_id,
            cpl_diagnostics=cpl_diagnostics,
            result=result,
            execution_manifest=execution_manifest,
        )
        raise


def _database_endpoint(tenant: str, platform: str = sys.platform) -> tuple[str, int]:
    """Select the reachable local PostgreSQL endpoint for this host."""

    if platform == "win32":
        # Docker Desktop exposes the direct PostgreSQL listener on Windows;
        # the Supabase pooler is not consistently reachable there.
        return "postgres", 55432
    return quote(f"postgres.{tenant}", safe=""), 5432


def _assert_queues_quiescent(database_url: str) -> None:
    """Refuse an operator E2E while either shared production queue is active."""

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_QUEUE_QUIESCENCE_SQL)
                row = cursor.fetchone()
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E queue preflight is unavailable") from None
    if not isinstance(row, Mapping):
        raise E2EFailure("Local E2E queue preflight returned an invalid result")
    analysis_count = row.get("active_analysis_runs")
    chat_count = row.get("active_chat_messages")
    if (
        not isinstance(analysis_count, int)
        or isinstance(analysis_count, bool)
        or not isinstance(chat_count, int)
        or isinstance(chat_count, bool)
        or analysis_count < 0
        or chat_count < 0
    ):
        raise E2EFailure("Local E2E queue preflight returned an invalid result")
    if analysis_count or chat_count:
        raise E2EFailure(
            "Local E2E requires empty analysis and chat queues; "
            f"active analysis={analysis_count}, chat={chat_count}"
        )


def _require_live_ml_results(
    database_url: str,
    *,
    case_id: str,
) -> dict[str, str]:
    """Prove all three persisted ML adapters completed, without exposing output."""

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_ML_RESULT_SQL, (case_id,))
                row = cursor.fetchone()
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E ML result validation is unavailable") from None
    ml_result = row.get("ml_result") if isinstance(row, Mapping) else None
    if not isinstance(ml_result, Mapping):
        raise E2EFailure("Local E2E ML result validation is invalid")

    statuses: dict[str, str] = {}
    for model_name in ("model_1", "model_2", "model_3"):
        model = ml_result.get(model_name)
        status = model.get("status") if isinstance(model, Mapping) else None
        if status not in {"OK", "UNAVAILABLE", "FAILED"}:
            raise E2EFailure("Local E2E ML result validation is invalid")
        statuses[model_name] = status
    failed = [name for name, status in statuses.items() if status != "OK"]
    if failed:
        summary = ", ".join(f"{name}={statuses[name]}" for name in failed)
        raise E2EFailure(f"live ML execution did not complete: {summary}")
    return statuses


def _worker_audit_rows(
    database_url: str,
    *,
    query: str,
    params: tuple[str, ...],
    subject: str,
) -> list[Mapping[str, Any]]:
    """Read immutable processing attempts without touching either queue."""

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall()
    except (psycopg.Error, OSError):
        raise E2EFailure(
            f"Local E2E {subject} worker audit is unavailable"
        ) from None
    if not isinstance(rows, list) or not rows or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise E2EFailure(f"Local E2E {subject} worker audit is invalid")
    return rows


def _audit_int(value: object, *, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise E2EFailure(f"Local E2E {subject} worker audit is invalid")
    return value


def _audit_metadata(
    row: Mapping[str, Any],
    *,
    subject: str,
) -> tuple[int, str]:
    metadata = row.get("run_metadata")
    if not isinstance(metadata, Mapping):
        raise E2EFailure(f"Local E2E {subject} worker audit is invalid")
    attempt_no = _audit_int(metadata.get("attempt_no"), subject=subject)
    worker_id = metadata.get("worker_id")
    if (
        not isinstance(worker_id, str)
        or not worker_id.strip()
        or worker_id != worker_id.strip()
        or len(worker_id) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in worker_id)
    ):
        raise E2EFailure(f"Local E2E {subject} worker audit is invalid")
    return attempt_no, worker_id


def _require_external_analysis_worker_audit(
    database_url: str,
    *,
    run_id: str,
) -> _WorkerAttemptAudit:
    """Prove which external worker attempts produced a terminal analysis."""

    rows = _worker_audit_rows(
        database_url,
        query=_ANALYSIS_WORKER_AUDIT_SQL,
        params=(run_id,),
        subject="analysis",
    )
    attempt_count = _audit_int(rows[0].get("attempt_count"), subject="analysis")
    if not 1 <= attempt_count <= MAX_WORKER_ATTEMPTS or len(rows) != attempt_count:
        raise E2EFailure("Local E2E analysis worker audit is invalid")

    worker_ids: list[str] = []
    for expected_attempt, row in enumerate(rows, start=1):
        if _audit_int(row.get("attempt_count"), subject="analysis") != attempt_count:
            raise E2EFailure("Local E2E analysis worker audit is invalid")
        attempt_no, worker_id = _audit_metadata(row, subject="analysis")
        expected_status = "succeeded" if expected_attempt == attempt_count else "failed"
        if attempt_no != expected_attempt or row.get("status") != expected_status:
            raise E2EFailure("Local E2E analysis worker audit is invalid")
        worker_ids.append(worker_id)

    return _WorkerAttemptAudit(
        final_worker_id=worker_ids[-1],
        attempt_count=attempt_count,
        attempt_worker_ids=tuple(worker_ids),
    )


def _require_external_chat_worker_audit(
    database_url: str,
    *,
    assistant_message_id: str,
    case_id: str,
) -> _WorkerAttemptAudit:
    """Prove bounded external chat attempts using its durable ops history.

    Chat resets the private dispatch counter after its one automatic retry, so
    the exact total is the number of immutable ``ops.processing_run`` rows.
    The current-cycle counter and public retry counters still provide an
    independent upper/lower bound for that total.
    """

    rows = _worker_audit_rows(
        database_url,
        query=_CHAT_WORKER_AUDIT_SQL,
        params=(assistant_message_id, case_id),
        subject="chat",
    )
    current_attempt = _audit_int(
        rows[0].get("current_cycle_attempt_count"), subject="chat"
    )
    auto_retries = _audit_int(rows[0].get("auto_retry_count"), subject="chat")
    manual_retries = _audit_int(rows[0].get("manual_retry_count"), subject="chat")
    if (
        not 1 <= current_attempt <= MAX_WORKER_ATTEMPTS
        or auto_retries > 1
        or manual_retries != 0
    ):
        raise E2EFailure("Local E2E chat worker audit is invalid")

    minimum_total = auto_retries + current_attempt
    maximum_total = (auto_retries + 1) * MAX_WORKER_ATTEMPTS
    if not minimum_total <= len(rows) <= maximum_total:
        raise E2EFailure("Local E2E chat worker audit is invalid")

    worker_ids: list[str] = []
    cycle_starts = 0
    previous_attempt = 0
    for index, row in enumerate(rows):
        if (
            _audit_int(row.get("current_cycle_attempt_count"), subject="chat")
            != current_attempt
            or _audit_int(row.get("auto_retry_count"), subject="chat")
            != auto_retries
            or _audit_int(row.get("manual_retry_count"), subject="chat")
            != manual_retries
        ):
            raise E2EFailure("Local E2E chat worker audit is invalid")
        attempt_no, worker_id = _audit_metadata(row, subject="chat")
        if not 1 <= attempt_no <= MAX_WORKER_ATTEMPTS:
            raise E2EFailure("Local E2E chat worker audit is invalid")
        if attempt_no == 1:
            cycle_starts += 1
        elif previous_attempt != attempt_no - 1:
            raise E2EFailure("Local E2E chat worker audit is invalid")
        expected_status = "succeeded" if index == len(rows) - 1 else "failed"
        if row.get("status") != expected_status:
            raise E2EFailure("Local E2E chat worker audit is invalid")
        previous_attempt = attempt_no
        worker_ids.append(worker_id)

    if cycle_starts != auto_retries + 1 or previous_attempt != current_attempt:
        raise E2EFailure("Local E2E chat worker audit is invalid")
    return _WorkerAttemptAudit(
        final_worker_id=worker_ids[-1],
        attempt_count=len(rows),
        attempt_worker_ids=tuple(worker_ids),
    )


def _require_public_ml_projection(result_body: Mapping[str, object]) -> None:
    """Verify that the public result route exposes all three safe ML messages.

    The HTTP contract intentionally excludes internal status, scores, and model
    diagnostics.  Persisted ``OK`` status is therefore checked separately via
    :func:`_require_live_ml_results`; this check protects the public projection
    from silently dropping a model section or its server-assembled message.
    """

    ml = result_body.get("ml")
    if not isinstance(ml, Mapping):
        raise E2EFailure("FastAPI result ML response is invalid")
    for model_name in ("model_1", "model_2", "model_3"):
        model = ml.get(model_name)
        message = model.get("message") if isinstance(model, Mapping) else None
        if not isinstance(message, str) or not message.strip():
            raise E2EFailure("FastAPI result ML response is invalid")


def _required(values: dict[str, str | None], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise E2EFailure(f"required local setting is missing: {name}")
    return value


def _validated_origin(value: str, *, setting_name: str) -> str:
    """Accept only a normalized HTTP(S) origin, never a URL with a path."""

    origin = value.strip()
    try:
        parsed = urlsplit(origin)
        # Reading ``port`` forces urlsplit to reject an invalid port instead of
        # forwarding a surprising Origin header to the API.
        _ = parsed.port
    except ValueError:
        raise E2EFailure(f"{setting_name} must be an HTTP(S) origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise E2EFailure(f"{setting_name} must be an HTTP(S) origin")
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_api_base_url(value: str) -> str:
    """Limit plaintext deployment tests to the local machine.

    An operator may use HTTPS for any deployment hostname.  Plain HTTP is
    deliberately limited to a loopback API, which keeps an accidental CLI
    invocation from sending the generated test-user password over the LAN.
    """

    base_url = _validated_origin(value, setting_name="--api-base-url")
    parsed = urlsplit(base_url)
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise E2EFailure("--api-base-url permits HTTP only for a loopback host")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


async def _external_api_build_identity(
    client: httpx.AsyncClient,
    *,
    expected_build_id: str,
) -> str:
    """Verify the deployed image identity is this clean checkout's context."""

    if _BUILD_ID_PATTERN.fullmatch(expected_build_id) is None:
        raise E2EFailure("Clean checkout Docker build identity is unavailable")

    try:
        response = await client.get("/health/ready")
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise E2EFailure("External FastAPI build identity is unavailable") from None
    header_identity = response.headers.get("X-PreReview-Build-Id")
    body_identity = payload.get("build_id") if isinstance(payload, Mapping) else None
    if (
        response.status_code != 200
        or not isinstance(header_identity, str)
        or not isinstance(body_identity, str)
        or header_identity != body_identity
        or _BUILD_ID_PATTERN.fullmatch(header_identity) is None
    ):
        raise E2EFailure("External FastAPI build identity is unavailable")
    if header_identity != expected_build_id:
        raise E2EFailure(
            "External FastAPI build identity does not match the clean checkout Docker context"
        )
    return header_identity


def _external_request_origin(
    *,
    explicit_origin: str | None,
    backend_env: Path,
) -> str:
    """Choose an explicit test origin or the first configured server origin."""

    if explicit_origin is not None:
        return _validated_origin(explicit_origin, setting_name="--request-origin")

    provider = dotenv_values(backend_env)
    raw_origins = (
        os.environ["PREREVIEW_AUTH_ALLOWED_ORIGINS"]
        if "PREREVIEW_AUTH_ALLOWED_ORIGINS" in os.environ
        else provider.get("PREREVIEW_AUTH_ALLOWED_ORIGINS")
    )
    for candidate in str(raw_origins or "").split(","):
        if candidate.strip():
            return _validated_origin(
                candidate,
                setting_name="PREREVIEW_AUTH_ALLOWED_ORIGINS",
            )
    raise E2EFailure(
        "--api-base-url requires --request-origin or "
        "PREREVIEW_AUTH_ALLOWED_ORIGINS in backend/.env"
    )


def _configure_environment(
    root_env: Path,
    supabase_env: Path,
    *,
    auth_allowed_origins: str | None = TEST_ORIGIN,
    llm_model: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    require_cursor_signing_secret: bool = False,
) -> None:
    if not 1 <= top_k <= 5:
        raise E2EFailure("--top-k must be between 1 and 5")
    provider = dotenv_values(root_env)
    local = dotenv_values(supabase_env)
    password = _required(local, "POSTGRES_PASSWORD")
    tenant = _required(local, "POOLER_TENANT_ID")
    database = str(local.get("POSTGRES_DB") or "postgres").strip() or "postgres"
    username, database_port = _database_endpoint(tenant)
    database_url = (
        f"postgresql://{username}:{quote(password, safe='')}"
        f"@127.0.0.1:{database_port}/{quote(database, safe='')}?sslmode=disable"
    )

    settings = {
        "PREREVIEW_OFFLINE_MODE": "false",
        "PREREVIEW_AUTH_COOKIE_SECURE": "false",
        "DATABASE_URL": database_url,
        "SUPABASE_URL": "http://127.0.0.1:8000",
        "SUPABASE_ANON_KEY": _required(local, "ANON_KEY"),
        "SUPABASE_SERVICE_ROLE_KEY": _required(local, "SERVICE_ROLE_KEY"),
        "OPENAI_API_KEY": _required(provider, "OPENAI_API_KEY"),
        "OPENAI_LLM_MODEL": llm_model
        or str(provider.get("OPENAI_LLM_MODEL") or "gpt-5.6-luna"),
        "OPENAI_EMBEDDING_MODEL": str(
            provider.get("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-small"
        ),
        "OPENAI_TIMEOUT_SECONDS": str(
            provider.get("OPENAI_TIMEOUT_SECONDS") or "120"
        ),
        "PREREVIEW_WORKER_TOP_K": str(top_k),
        "PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS": "120",
        "PREREVIEW_WORKER_DATABASE_CONNECT_TIMEOUT_SECONDS": "20",
    }
    if sys.platform != "win32":
        settings["PREREVIEW_FREETYPE_LIB"] = "/lib/x86_64-linux-gnu/libfreetype.so.6"
    if auth_allowed_origins is not None:
        settings["PREREVIEW_AUTH_ALLOWED_ORIGINS"] = auth_allowed_origins
    if require_cursor_signing_secret:
        # Inline mode imports FastAPI in this process, so its opaque-cursor
        # routes require the server-only signing secret here too.  Preserve an
        # explicitly exported process value (including an explicit invalid
        # blank) before consulting backend/.env, matching deployment loading
        # precedence without ever rendering the value.
        secret_values: dict[str, str | None]
        if CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY in os.environ:
            secret_values = {
                CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY: os.environ.get(
                    CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY
                )
            }
        else:
            secret_values = provider
        settings[CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY] = _required(
            secret_values,
            CURSOR_SIGNING_SECRET_ENVIRONMENT_KEY,
        )
    # Keep the common fallback independent from stage-specific models.  Shell
    # values take precedence just as python-dotenv's ``override=False`` does;
    # the Request Profile override defaults to Terra for this live smoke test.
    for name in OPTIONAL_STAGE_MODEL_ENVIRONMENT_KEYS:
        default = "gpt-5.6-terra" if name == "OPENAI_REQUEST_PROFILE_MODEL" else ""
        if name in os.environ:
            configured = os.environ[name]
        elif name in provider:
            configured = provider[name]
        else:
            configured = default
        value = str(configured or "").strip()
        if value:
            settings[name] = value
    # ``dotenv_values`` does not update the process.  Carry the server-only ML
    # paths explicitly so an operator does not get silent UNAVAILABLE/FAILED
    # model results after correctly filling backend/.env.  An explicit shell
    # environment wins, matching python-dotenv's normal ``override=False``
    # deployment semantics.
    for name in ML_ENVIRONMENT_KEYS:
        configured = os.environ[name] if name in os.environ else provider.get(name)
        value = str(configured or "").strip()
        if value:
            settings[name] = value
    os.environ.update(settings)


async def _create_confirmed_test_user() -> tuple[str, str, str]:
    base_url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    marker = uuid4().hex
    email = f"pre-review-e2e-{marker}@example.invalid"
    password = f"E2E-{uuid4().hex}-aA1!"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(
            f"{base_url}/auth/v1/admin/users",
            headers=headers,
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
            },
        )
    if response.status_code not in {200, 201}:
        raise E2EFailure(
            f"Supabase test-user creation failed with HTTP {response.status_code}"
        )
    try:
        user_id = response.json()["id"]
    except (ValueError, KeyError, TypeError):
        raise E2EFailure("Supabase test-user response is invalid") from None
    if not isinstance(user_id, str) or not user_id:
        raise E2EFailure("Supabase test-user response has no user id")
    return user_id, email, password


async def _analysis_state(client: httpx.AsyncClient, run_id: str) -> dict[str, object]:
    response = await client.get(f"/api/v1/analysis-runs/{run_id}")
    if response.status_code != 200:
        raise E2EFailure(
            f"FastAPI status polling failed with HTTP {response.status_code}"
        )
    try:
        state = response.json()
    except ValueError:
        raise E2EFailure("FastAPI status polling response is invalid") from None
    if not isinstance(state, dict) or state.get("analysis_run_id") != run_id:
        raise E2EFailure("FastAPI status polling response is invalid")
    return state


async def _assistant_message_state(
    client: httpx.AsyncClient,
    case_id: str,
    assistant_message_id: str,
) -> dict[str, object]:
    response = await client.get(
        f"/api/v1/analysis-cases/{case_id}/messages/{assistant_message_id}"
    )
    if response.status_code != 200:
        raise E2EFailure(
            f"FastAPI chat polling failed with HTTP {response.status_code}"
        )
    try:
        message = response.json()
    except ValueError:
        raise E2EFailure("FastAPI chat polling response is invalid") from None
    if not isinstance(message, dict):
        raise E2EFailure("FastAPI chat polling response is invalid")
    if (
        message.get("message_id") != assistant_message_id
        or message.get("analysis_case_id") != case_id
        or message.get("role") != "assistant"
    ):
        raise E2EFailure("FastAPI chat polling response is invalid")
    if message.get("status") not in {"generating", "completed", "failed"}:
        raise E2EFailure("FastAPI chat polling response is invalid")
    return message


async def _poll_external_analysis_until_terminal(
    *,
    client: httpx.AsyncClient,
    run_id: str,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, object], int]:
    """Wait for an already-running worker through the public status route.

    This intentionally has no repository, queue-claim, or worker-runtime
    dependency.  It proves the deployed worker consumed the API-created job
    while keeping the E2E command from taking work away from that worker.
    """

    effective_timeout = (
        ANALYSIS_POLL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    )
    deadline = asyncio.get_running_loop().time() + effective_timeout
    polls = 0
    while True:
        state = await _analysis_state(client, run_id)
        polls += 1
        status = state.get("status")
        if status == "succeeded":
            return state, polls
        if status in {"failed", "cancelled"}:
            raise E2EFailure("external analysis worker reached a terminal failure")
        if status not in {"uploading", "queued", "running", "cleanup_pending"}:
            raise E2EFailure("external analysis worker returned an invalid status")
        if asyncio.get_running_loop().time() >= deadline:
            raise E2EFailure("external analysis worker polling timed out")
        await asyncio.sleep(ANALYSIS_POLL_INTERVAL_SECONDS)


async def _poll_external_chat_until_terminal(
    *,
    client: httpx.AsyncClient,
    case_id: str,
    assistant_message_id: str,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, object], int]:
    """Wait for an already-running chat worker through the public route."""

    effective_timeout = CHAT_POLL_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = asyncio.get_running_loop().time() + effective_timeout
    polls = 0
    while True:
        message = await _assistant_message_state(
            client,
            case_id,
            assistant_message_id,
        )
        polls += 1
        status = message["status"]
        if status == "completed":
            content = message.get("content")
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content) > 12_000
            ):
                raise E2EFailure("completed chat response is invalid")
            return message, polls
        if status == "failed":
            raise E2EFailure("external chat worker reached a terminal failure")
        if asyncio.get_running_loop().time() >= deadline:
            raise E2EFailure("external chat worker polling timed out")
        await asyncio.sleep(CHAT_POLL_INTERVAL_SECONDS)


async def _poll_chat_message_while_worker_runs(
    *,
    client: httpx.AsyncClient,
    case_id: str,
    assistant_message_id: str,
    worker_task: asyncio.Task[object],
) -> dict[str, object]:
    """Poll the public route while one scoped runtime processes its job.

    A first failed attempt is intentionally returned as ``generating`` by the
    queue so the caller can run the bounded second attempt immediately.
    """

    deadline = asyncio.get_running_loop().time() + CHAT_POLL_TIMEOUT_SECONDS
    while True:
        message = await _assistant_message_state(
            client, case_id, assistant_message_id
        )
        if message["status"] in {"completed", "failed"}:
            return message
        if worker_task.done():
            # The first GET may have raced the worker's final commit.  Read
            # once more after the task has settled rather than handing the
            # caller a stale ``generating`` snapshot for a completed job.
            await asyncio.sleep(0)
            return await _assistant_message_state(
                client, case_id, assistant_message_id
            )
        if asyncio.get_running_loop().time() >= deadline:
            # Do not abandon a running thread which owns a DB lease.  Its
            # result must settle before returning.  Re-read after the commit:
            # a slow but successful provider call must not become a false E2E
            # failure merely because public polling reached its soft limit.
            outcome = await worker_task
            settled = await _assistant_message_state(
                client, case_id, assistant_message_id
            )
            outcome_value = str(getattr(outcome, "value", outcome))
            if settled["status"] in {"completed", "failed"}:
                return settled
            # A retryable first failure intentionally leaves the public row in
            # ``generating``; let the bounded caller perform attempt two.
            if outcome_value == "failed":
                return settled
            raise E2EFailure("chat worker polling timed out")
        await asyncio.sleep(CHAT_POLL_INTERVAL_SECONDS)


def _chat_reference_count(
    database_url: str,
    *,
    assistant_message_id: str,
    case_id: str,
) -> int:
    """Require at least one persisted, case-scoped reference for this E2E question."""

    try:
        with psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    _CHAT_REFERENCE_COUNTS_SQL,
                    (case_id, assistant_message_id),
                )
                row = cursor.fetchone()
    except (psycopg.Error, OSError):
        raise E2EFailure("Local E2E chat reference validation is unavailable") from None
    if not isinstance(row, Mapping):
        raise E2EFailure("Local E2E chat reference validation is invalid")
    count = row.get("reference_count")
    valid_count = row.get("valid_reference_count")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not isinstance(valid_count, int)
        or isinstance(valid_count, bool)
        or count <= 0
        or valid_count != count
    ):
        raise E2EFailure("Local E2E chat references are invalid")
    return count


async def _run_target_until_terminal(
    *,
    runtime: Any,
    repository: _TargetRunRepository,
    client: httpx.AsyncClient,
    run_id: str,
) -> tuple[dict[str, object], str, int]:
    """Drive at most the database queue's two attempts for one target run."""

    last_outcome = "not_started"
    for expected_attempt in range(1, MAX_WORKER_ATTEMPTS + 1):
        outcome = await asyncio.to_thread(runtime.run_once)
        last_outcome = str(getattr(outcome, "value", outcome))
        if getattr(repository, "claim_error", None) is not None:
            raise repository.claim_error
        if repository.unexpected_claim:
            raise E2EFailure("Local E2E queue isolation failed")
        if repository.target_claims != expected_attempt:
            raise E2EFailure(
                "Local E2E worker did not claim the requested analysis run"
            )

        state = await _analysis_state(client, run_id)
        run_status = state.get("status")
        if run_status == "succeeded" and last_outcome == "completed":
            return state, last_outcome, expected_attempt
        if run_status == "failed":
            raise E2EFailure(
                f"worker reached terminal failure after {expected_attempt} attempt(s)"
            )
        if run_status == "queued" and last_outcome == "failed":
            if expected_attempt < MAX_WORKER_ATTEMPTS:
                continue
            raise E2EFailure(
                "worker retry budget was exhausted without a terminal state"
            )
        raise E2EFailure(
            f"worker left the target run in an unexpected state: "
            f"outcome={last_outcome}, status={run_status}"
        )

    raise E2EFailure("worker retry loop ended without a terminal state")


async def _run_target_chat_until_terminal(
    *,
    runtime: Any,
    repository: _TargetChatRepository,
    client: httpx.AsyncClient,
    case_id: str,
    assistant_message_id: str,
) -> tuple[dict[str, object], str, int]:
    """Drive at most the queue's two automatic chat attempts for one row."""

    last_outcome = "not_started"
    for expected_attempt in range(1, MAX_WORKER_ATTEMPTS + 1):
        worker_task = asyncio.create_task(asyncio.to_thread(runtime.run_once))
        worker_id: str | None = None
        try:
            message = await _poll_chat_message_while_worker_runs(
                client=client,
                case_id=case_id,
                assistant_message_id=assistant_message_id,
                worker_task=worker_task,
            )
        except Exception:
            # A polling/contract error must not leave an unobserved worker
            # task holding a DB lease in the background.
            try:
                await worker_task
            except Exception:
                pass
            raise
        try:
            outcome = await worker_task
        except Exception:
            raise E2EFailure("Local E2E chat worker execution failed") from None
        last_outcome = str(getattr(outcome, "value", outcome))
        if repository.claim_error is not None:
            raise repository.claim_error
        if repository.unexpected_claim:
            raise E2EFailure("Local E2E chat queue isolation failed")
        if repository.target_claims != expected_attempt:
            raise E2EFailure(
                "Local E2E worker did not claim the requested chat message"
            )

        status = message["status"]
        if status == "completed" and last_outcome == "completed":
            content = message.get("content")
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content) > 12_000
            ):
                raise E2EFailure("completed chat response is invalid")
            return message, last_outcome, expected_attempt
        if status == "failed":
            raise E2EFailure(
                f"chat worker reached terminal failure after {expected_attempt} attempt(s)"
            )
        if status == "generating" and last_outcome == "failed":
            if expected_attempt < MAX_WORKER_ATTEMPTS:
                continue
            raise E2EFailure(
                "chat worker retry budget was exhausted without a terminal state"
            )
        raise E2EFailure(
            "chat worker left the target message in an unexpected state: "
            f"outcome={last_outcome}, status={status}"
        )

    raise E2EFailure("chat worker retry loop ended without a terminal state")


async def _post_chat_message(
    client: httpx.AsyncClient,
    *,
    case_id: str,
    request_origin: str,
) -> httpx.Response:
    """Create an E2E chat turn with the v0.2 idempotency contract."""

    return await client.post(
        f"/api/v1/analysis-cases/{case_id}/messages",
        headers={
            "Origin": request_origin,
            "Idempotency-Key": str(uuid4()),
        },
        json={"content": E2E_CHAT_QUESTION},
    )


async def _run(
    source: Path,
    *,
    worker_mode: str = "inline",
    api_base_url: str | None = None,
    request_origin: str = TEST_ORIGIN,
    analysis_poll_timeout_seconds: float = ANALYSIS_POLL_TIMEOUT_SECONDS,
    chat_poll_timeout_seconds: float = CHAT_POLL_TIMEOUT_SECONDS,
    trace_dir: Path | None = None,
    source_content: bytes | None = None,
    source_mime_type: str | None = None,
) -> dict[str, object]:
    from worker.main import configure_runtime_logging

    if worker_mode not in {"inline", "external"}:
        raise E2EFailure("worker mode must be inline or external")
    if worker_mode == "external" and api_base_url is None:
        raise E2EFailure("external worker mode requires a deployed --api-base-url")
    if worker_mode == "inline" and api_base_url is not None:
        raise E2EFailure("--api-base-url requires --worker-mode external")

    expected_external_build_id: str | None = None
    if worker_mode == "external":
        expected_external_build_id = await asyncio.to_thread(
            _require_clean_checkout_build_context_digest
        )

    # OPENAI_LOG=debug can make the SDK log its full request options, including
    # the uploaded document text.  Apply the same transport-logger floor as the
    # long-running worker before any provider call in this operator entrypoint.
    configure_runtime_logging(level=os.getenv("LOG_LEVEL", "INFO").upper())

    if source_content is None or source_mime_type is None:
        source_content, source_mime_type = _validated_source(source)

    await asyncio.to_thread(
        _assert_queues_quiescent,
        os.environ["DATABASE_URL"],
    )
    user_id, email, password = await _create_confirmed_test_user()
    trace_handler: object | None = None
    cpl_diagnostics: list[dict[str, object]] = []
    if api_base_url is None:
        # Keep the default fast, isolated contract mode.  The API deployment
        # mode below intentionally does not import this local application.
        from main import create_app

        client_options: dict[str, object] = {
            "transport": httpx.ASGITransport(app=create_app()),
            "base_url": TEST_ORIGIN,
        }
    else:
        client_options = {"base_url": api_base_url}
    deployed_api_build_id: str | None = None
    async with httpx.AsyncClient(
        **client_options,
        timeout=60,
        follow_redirects=False,
    ) as client:
        if worker_mode == "external":
            assert expected_external_build_id is not None
            deployed_api_build_id = await _external_api_build_identity(
                client,
                expected_build_id=expected_external_build_id,
            )
        signed_in = await client.post(
            "/api/v1/auth/sign-in",
            headers={"Origin": request_origin},
            json={"email": email, "password": password},
        )
        if signed_in.status_code != 200:
            raise E2EFailure(
                f"FastAPI sign-in failed with HTTP {signed_in.status_code}"
            )

        uploaded = await client.post(
            "/api/v1/analysis-runs",
            headers={
                "Origin": request_origin,
                "Idempotency-Key": str(uuid4()),
            },
            files={
                "file": (
                    source.name,
                    source_content,
                    source_mime_type,
                )
            },
        )
        if uploaded.status_code != 202:
            raise E2EFailure(
                f"FastAPI upload failed with HTTP {uploaded.status_code}"
            )
        payload = uploaded.json()
        run_id = payload.get("analysis_run_id")
        if not isinstance(run_id, str) or payload.get("status") != "queued":
            raise E2EFailure("FastAPI upload response contract is invalid")

        try:
            if worker_mode == "external":
                state, analysis_poll_count = await _poll_external_analysis_until_terminal(
                    client=client,
                    run_id=run_id,
                    timeout_seconds=analysis_poll_timeout_seconds,
                )
                analysis_worker_audit = await asyncio.to_thread(
                    _require_external_analysis_worker_audit,
                    os.environ["DATABASE_URL"],
                    run_id=run_id,
                )
                worker_outcome: str | None = "external"
                worker_attempts: int | None = analysis_worker_audit.attempt_count
                worker_id: str | None = analysis_worker_audit.final_worker_id
                worker_attempt_worker_ids: list[str] | None = list(
                    analysis_worker_audit.attempt_worker_ids
                )
            else:
                from worker.main import build_worker
                from worker.runtime import WorkerRuntime

                composition = build_worker()
                trace_handler = composition.handler
                # CPL/recheck and structure diagnostics are intentionally not
                # part of the public result response.  Preserve them for a
                # requested trace, but never let reporting affect the job.
                composition.handler.set_diagnostics_sink(
                    lambda stage, rows: _collect_diagnostics(
                        cpl_diagnostics, stage, rows
                    )
                )
                # The live run can take several minutes.  The worker already
                # emits stage events, so expose them without contaminating the
                # command's final stdout JSON.
                composition.handler.set_stage_callback(_report_analysis_stage)
                target_repository = _TargetRunRepository(
                    composition.repository,
                    target_run_id=run_id,
                    database_url=os.environ["DATABASE_URL"],
                )
                worker_id = f"local-e2e-{uuid4().hex[:12]}"
                runtime = WorkerRuntime(
                    target_repository,
                    composition.handler,
                    worker_id=worker_id,
                    heartbeat_seconds=composition.settings.heartbeat_seconds,
                    lease_seconds=composition.settings.lease_seconds,
                    idle_poll_seconds=composition.settings.idle_poll_seconds,
                )
                state, worker_outcome, worker_attempts = await _run_target_until_terminal(
                    runtime=runtime,
                    repository=target_repository,
                    client=client,
                    run_id=run_id,
                )
                worker_attempt_worker_ids = [worker_id] * worker_attempts
                analysis_poll_count = None
        except E2EFailure:
            if trace_handler is None and trace_dir is not None:
                # In external mode this constructs a local read adapter only;
                # it never claims or executes the API-created job.
                try:
                    from worker.main import build_worker

                    trace_handler = build_worker().handler
                except Exception:  # noqa: BLE001 - preserve original failure
                    pass
            await _capture_analysis_failure_trace(
                client=client,
                trace_dir=trace_dir,
                upload=payload,
                handler=trace_handler,
                run_id=run_id,
                cpl_diagnostics=cpl_diagnostics,
                execution_manifest=_partial_execution_manifest(
                    source_content=source_content,
                    worker_mode=worker_mode,
                    analysis_worker_id=worker_id,
                    database_url=os.environ["DATABASE_URL"],
                    analysis_run_id=run_id,
                    deployed_api_build_id=deployed_api_build_id,
                ),
            )
            raise
        case_id = state.get("analysis_case_id")
        if not isinstance(case_id, str) or not case_id:
            raise E2EFailure("completed run has no analysis case id")

        result = await client.get(f"/api/v1/analysis-cases/{case_id}")
        if result.status_code != 200:
            raise E2EFailure(
                f"FastAPI result read failed with HTTP {result.status_code}"
            )
        body = result.json()
        if not isinstance(body, dict):
            raise E2EFailure("FastAPI result response is invalid")
        _require_public_ml_projection(body)

        current_ready_response = await client.get("/api/v1/analysis/current")
        if current_ready_response.status_code != 200:
            raise E2EFailure(
                "FastAPI current analysis read failed with HTTP "
                f"{current_ready_response.status_code}"
            )
        current_ready = current_ready_response.json()
        if (
            not isinstance(current_ready, Mapping)
            or current_ready.get("state") != "ready"
            or not isinstance(current_ready.get("session"), Mapping)
            or current_ready["session"].get("analysis_case_id") != case_id
        ):
            raise E2EFailure("FastAPI current analysis ready state is invalid")

        active_history_response = await client.get("/api/v1/analysis-history")
        if active_history_response.status_code != 200:
            raise E2EFailure(
                "FastAPI active-session history read failed with HTTP "
                f"{active_history_response.status_code}"
            )
        active_history = active_history_response.json()
        if not isinstance(active_history, Mapping) or not isinstance(
            active_history.get("items"), list
        ):
            raise E2EFailure("FastAPI active-session history response is invalid")
        if any(
            isinstance(item, Mapping) and item.get("analysis_case_id") == case_id
            for item in active_history["items"]
        ):
            raise E2EFailure("Active analysis leaked into historical analysis list")

        candidates = (body.get("sim") or {}).get("candidates", [])
        if not isinstance(candidates, list) or not candidates:
            raise E2EFailure("FastAPI result returned no SIM candidate to inspect")
        first_candidate = candidates[0]
        candidate_id = (
            first_candidate.get("sim_candidate_id")
            if isinstance(first_candidate, Mapping)
            else None
        )
        if not isinstance(candidate_id, str):
            raise E2EFailure("FastAPI SIM candidate summary is invalid")
        candidate_response = await client.get(f"/api/v1/sim-candidates/{candidate_id}")
        if candidate_response.status_code != 200:
            raise E2EFailure(
                "FastAPI SIM candidate detail failed with HTTP "
                f"{candidate_response.status_code}"
            )
        candidate_detail = candidate_response.json()
        if (
            not isinstance(candidate_detail, Mapping)
            or candidate_detail.get("analysis_case_id") != case_id
            or candidate_detail.get("sim_candidate_id") != candidate_id
            or not isinstance(candidate_detail.get("axes"), Mapping)
            or set(candidate_detail["axes"]) != {"purpose", "target", "support", "delivery"}
            or not isinstance(candidate_detail.get("evidences"), list)
        ):
            raise E2EFailure("FastAPI SIM candidate detail response is invalid")
        evidence_by_side = {"request": set(), "existing": set()}
        for evidence in candidate_detail["evidences"]:
            if not isinstance(evidence, Mapping):
                raise E2EFailure("FastAPI SIM candidate evidence is invalid")
            evidence_id = evidence.get("evidence_id")
            side = evidence.get("side")
            if not isinstance(evidence_id, str) or side not in evidence_by_side:
                raise E2EFailure("FastAPI SIM candidate evidence is invalid")
            evidence_by_side[side].add(evidence_id)
        referenced_by_side = {"request": set(), "existing": set()}
        for axis in candidate_detail["axes"].values():
            if not isinstance(axis, Mapping):
                raise E2EFailure("FastAPI SIM candidate axis is invalid")
            for side, field in (
                ("request", "request_evidence_ids"),
                ("existing", "existing_evidence_ids"),
            ):
                references = axis.get(field)
                if not isinstance(references, list) or not all(
                    isinstance(item, str) for item in references
                ):
                    raise E2EFailure("FastAPI SIM candidate axis evidence is invalid")
                referenced_by_side[side].update(references)
        if (
            not referenced_by_side["request"]
            or not referenced_by_side["existing"]
            or not referenced_by_side["request"].issubset(evidence_by_side["request"])
            or not referenced_by_side["existing"].issubset(evidence_by_side["existing"])
        ):
            raise E2EFailure("FastAPI SIM candidate evidence linkage is invalid")
        ml_statuses = await _require_live_ml_results_with_failure_trace(
            client=client,
            trace_dir=trace_dir,
            upload=payload,
            handler=trace_handler,
            run_id=run_id,
            cpl_diagnostics=cpl_diagnostics,
            result=body,
            database_url=os.environ["DATABASE_URL"],
            case_id=case_id,
            execution_manifest=_partial_execution_manifest(
                source_content=source_content,
                worker_mode=worker_mode,
                analysis_worker_id=worker_id,
                database_url=os.environ["DATABASE_URL"],
                analysis_run_id=run_id,
                deployed_api_build_id=deployed_api_build_id,
            ),
        )

        created_chat = await _post_chat_message(
            client,
            case_id=case_id,
            request_origin=request_origin,
        )
        if created_chat.status_code != 202:
            raise E2EFailure(
                f"FastAPI chat creation failed with HTTP {created_chat.status_code}"
            )
        try:
            chat_turn = created_chat.json()
        except ValueError:
            raise E2EFailure("FastAPI chat creation response is invalid") from None
        if (
            not isinstance(chat_turn, dict)
            or chat_turn.get("status") != "generating"
            or not isinstance(chat_turn.get("assistant_message_id"), str)
            or not isinstance(chat_turn.get("user_message_id"), str)
            or not isinstance(chat_turn.get("analysis_session_id"), str)
        ):
            raise E2EFailure("FastAPI chat creation response is invalid")
        assistant_message_id = chat_turn["assistant_message_id"]

        if worker_mode == "external":
            chat_message, chat_poll_count = await _poll_external_chat_until_terminal(
                client=client,
                case_id=case_id,
                assistant_message_id=assistant_message_id,
                timeout_seconds=chat_poll_timeout_seconds,
            )
            chat_worker_audit = await asyncio.to_thread(
                _require_external_chat_worker_audit,
                os.environ["DATABASE_URL"],
                assistant_message_id=assistant_message_id,
                case_id=case_id,
            )
            chat_worker_outcome: str | None = "external"
            chat_worker_attempts: int | None = chat_worker_audit.attempt_count
            chat_worker_id: str | None = chat_worker_audit.final_worker_id
            chat_worker_attempt_worker_ids: list[str] | None = list(
                chat_worker_audit.attempt_worker_ids
            )
        else:
            from worker.chat_main import build_chat_worker
            from worker.runtime import WorkerRuntime

            chat_composition = build_chat_worker()
            target_chat_repository = _TargetChatRepository(
                chat_composition.repository,
                target_assistant_message_id=assistant_message_id,
                database_url=os.environ["DATABASE_URL"],
            )
            chat_worker_id = f"local-e2e-chat-{uuid4().hex[:12]}"
            chat_runtime = WorkerRuntime(
                target_chat_repository,
                chat_composition.handler,
                worker_id=chat_worker_id,
                heartbeat_seconds=chat_composition.settings.heartbeat_seconds,
                lease_seconds=chat_composition.settings.lease_seconds,
                idle_poll_seconds=chat_composition.settings.idle_poll_seconds,
            )
            chat_message, chat_worker_outcome, chat_worker_attempts = (
                await _run_target_chat_until_terminal(
                    runtime=chat_runtime,
                    repository=target_chat_repository,
                    client=client,
                    case_id=case_id,
                    assistant_message_id=assistant_message_id,
                )
            )
            chat_worker_attempt_worker_ids = [chat_worker_id] * chat_worker_attempts
            chat_poll_count = None
        chat_reference_count = await asyncio.to_thread(
            _chat_reference_count,
            os.environ["DATABASE_URL"],
            assistant_message_id=assistant_message_id,
            case_id=case_id,
        )

        chat_history_response = await client.get(
            f"/api/v1/analysis-cases/{case_id}/messages?limit=50"
        )
        if chat_history_response.status_code != 200:
            raise E2EFailure(
                "FastAPI chat history read failed with HTTP "
                f"{chat_history_response.status_code}"
            )
        chat_history = chat_history_response.json()
        history_items = chat_history.get("items") if isinstance(chat_history, Mapping) else None
        if not isinstance(history_items, list) or not {
            chat_turn["user_message_id"],
            assistant_message_id,
        }.issubset(
            {
                item.get("message_id")
                for item in history_items
                if isinstance(item, Mapping)
            }
        ):
            raise E2EFailure("FastAPI chat history omitted the completed turn")

        analysis_session_id = chat_turn["analysis_session_id"]
        close_response = await client.post(
            f"/api/v1/analysis-sessions/{analysis_session_id}/close",
            headers={"Origin": request_origin},
        )
        if close_response.status_code != 204:
            raise E2EFailure(
                "FastAPI analysis session close failed with HTTP "
                f"{close_response.status_code}"
            )

        current_closed_response = await client.get("/api/v1/analysis/current")
        if current_closed_response.status_code != 200:
            raise E2EFailure(
                "FastAPI current state after close failed with HTTP "
                f"{current_closed_response.status_code}"
            )
        current_closed = current_closed_response.json()
        if not isinstance(current_closed, Mapping) or current_closed.get("state") != "idle":
            raise E2EFailure("FastAPI current state was not idle after close")

        closed_history_response = await client.get("/api/v1/analysis-history")
        if closed_history_response.status_code != 200:
            raise E2EFailure(
                "FastAPI closed-session history read failed with HTTP "
                f"{closed_history_response.status_code}"
            )
        closed_history = closed_history_response.json()
        closed_items = closed_history.get("items") if isinstance(closed_history, Mapping) else None
        if not isinstance(closed_items, list) or not any(
            isinstance(item, Mapping) and item.get("analysis_case_id") == case_id
            for item in closed_items
        ):
            raise E2EFailure("Closed analysis was not added to historical analysis list")

        retained_chat_response = await client.get(
            f"/api/v1/analysis-cases/{case_id}/messages?limit=50"
        )
        if retained_chat_response.status_code != 200:
            raise E2EFailure(
                "FastAPI retained chat history failed with HTTP "
                f"{retained_chat_response.status_code}"
            )
        retained_chat = retained_chat_response.json()
        retained_items = (
            retained_chat.get("items") if isinstance(retained_chat, Mapping) else None
        )
        if not isinstance(retained_items, list) or not any(
            isinstance(item, Mapping)
            and item.get("message_id") == assistant_message_id
            and item.get("status") == "completed"
            for item in retained_items
        ):
            raise E2EFailure("Closed analysis did not retain completed chat history")

        if not isinstance(worker_id, str) or not isinstance(chat_worker_id, str):
            raise E2EFailure("Worker runtime identity is invalid")
        execution_manifest = await asyncio.to_thread(
            _execution_manifest,
            source_content=source_content,
            worker_mode=worker_mode,
            analysis_worker_id=worker_id,
            chat_worker_id=chat_worker_id,
            database_url=os.environ["DATABASE_URL"],
            analysis_run_id=run_id,
            deployed_api_build_id=deployed_api_build_id,
        )
        outcome: dict[str, object] = {
            "status": "ok",
            "worker_mode": worker_mode,
            "api_mode": "deployed" if api_base_url is not None else "asgi",
            "test_user_id": user_id,
            "analysis_run_id": run_id,
            "analysis_case_id": case_id,
            "worker_outcome": worker_outcome,
            "worker_id": worker_id,
            "worker_attempts": worker_attempts,
            "worker_attempt_worker_ids": worker_attempt_worker_ids,
            "analysis_poll_count": analysis_poll_count,
            "cpl_items": len(body.get("cpl", {}).get("items", [])),
            "fit_items": len(body.get("fit", {}).get("items", [])),
            "sim_candidates": len(body.get("sim", {}).get("candidates", [])),
            "evidences": len(body.get("evidences", [])),
            "ml_statuses": ml_statuses,
            "chat_assistant_message_id": assistant_message_id,
            "chat_status": chat_message["status"],
            "chat_worker_outcome": chat_worker_outcome,
            "chat_worker_id": chat_worker_id,
            "chat_worker_attempts": chat_worker_attempts,
            "chat_worker_attempt_worker_ids": chat_worker_attempt_worker_ids,
            "chat_poll_count": chat_poll_count,
            "chat_reference_count": chat_reference_count,
            "candidate_detail_checked": candidate_id,
            "active_history_excluded_case": True,
            "closed_history_included_case": True,
            "chat_history_reloaded": True,
            "session_closed": True,
            "execution_manifest": execution_manifest,
        }
        if trace_dir is not None:
            if trace_handler is None:
                # External mode still reads only the artifacts the deployed
                # worker wrote; it never runs or claims a worker locally.
                from worker.main import build_worker

                trace_handler = build_worker().handler
            common_ir, structured_profile = _cached_artifacts(trace_handler, run_id)
            if structured_profile is None:
                raise E2EFailure("worker did not persist the structured profile")
            _write_trace(
                trace_dir,
                upload=payload,
                common_ir=common_ir,
                structured_profile=structured_profile,
                result=body,
                run_state=state,
                cpl_diagnostics={
                    "analysis_run_id": run_id,
                    "diagnostics": cpl_diagnostics,
                },
                execution_manifest=execution_manifest,
            )
            outcome["trace_dir"] = str(trace_dir)
        return outcome


def _positive_timeout_seconds(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and positive")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--backend-env",
        "--root-env",
        dest="backend_env",
        type=Path,
        default=DEFAULT_BACKEND_ENV,
        help="FastAPI/worker runtime .env (default: backend/.env)",
    )
    parser.add_argument("--supabase-env", type=Path, default=DEFAULT_SUPABASE_ENV)
    parser.add_argument(
        "--llm-model",
        help="override OPENAI_LLM_MODEL for this one E2E run",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"SIM retrieval breadth for this run (default: {DEFAULT_TOP_K})",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        help="new directory for actual upload, stage, result, and CPL diagnostic JSON",
    )
    parser.add_argument(
        "--worker-mode",
        choices=("inline", "external"),
        default="inline",
        help=(
            "inline claims only this E2E job in-process (default); external polls "
            "already-running analysis and chat workers without claiming jobs"
        ),
    )
    parser.add_argument(
        "--api-base-url",
        help=(
            "optional deployed FastAPI base URL; accepts HTTPS or loopback HTTP "
            "only (for example http://127.0.0.1:8001)"
        ),
    )
    parser.add_argument(
        "--request-origin",
        help=(
            "explicit allowed HTTP(S) Origin for deployed API requests; defaults "
            "to the first PREREVIEW_AUTH_ALLOWED_ORIGINS entry in backend/.env"
        ),
    )
    parser.add_argument(
        "--analysis-poll-timeout-seconds",
        type=_positive_timeout_seconds,
        default=ANALYSIS_POLL_TIMEOUT_SECONDS,
        help=(
            "external analysis polling deadline in seconds "
            f"(default: {ANALYSIS_POLL_TIMEOUT_SECONDS})"
        ),
    )
    parser.add_argument(
        "--chat-poll-timeout-seconds",
        type=_positive_timeout_seconds,
        default=CHAT_POLL_TIMEOUT_SECONDS,
        help=(
            "external chat polling deadline in seconds "
            f"(default: {CHAT_POLL_TIMEOUT_SECONDS})"
        ),
    )
    args = parser.parse_args()
    if not args.file.is_file():
        raise E2EFailure("--file must be an existing HWP or HWPX file")
    source_content, source_mime_type = _validated_source(args.file)
    if args.trace_dir is not None and args.trace_dir.exists():
        raise E2EFailure("--trace-dir must not already exist")
    api_base_url = (
        _validated_api_base_url(args.api_base_url)
        if args.api_base_url is not None
        else None
    )
    if args.worker_mode == "external" and api_base_url is None:
        raise E2EFailure("--worker-mode external requires --api-base-url")
    if args.worker_mode == "inline" and api_base_url is not None:
        raise E2EFailure("--api-base-url requires --worker-mode external")
    if args.worker_mode == "external":
        # Reject a non-release checkout before loading local service settings
        # or touching the deployment. _run repeats this as a programmatic
        # entrypoint defense.
        _require_clean_checkout_build_context_digest()
    if args.request_origin is not None and api_base_url is None:
        raise E2EFailure("--request-origin requires --api-base-url")
    request_origin = (
        _external_request_origin(
            explicit_origin=args.request_origin,
            backend_env=args.backend_env,
        )
        if api_base_url is not None
        else TEST_ORIGIN
    )
    _configure_environment(
        args.backend_env,
        args.supabase_env,
        auth_allowed_origins=TEST_ORIGIN if api_base_url is None else None,
        llm_model=args.llm_model,
        top_k=args.top_k,
        require_cursor_signing_secret=args.worker_mode == "inline",
    )
    result = asyncio.run(
        _run(
            args.file.resolve(),
            worker_mode=args.worker_mode,
            api_base_url=api_base_url,
            request_origin=request_origin,
            analysis_poll_timeout_seconds=args.analysis_poll_timeout_seconds,
            chat_poll_timeout_seconds=args.chat_poll_timeout_seconds,
            trace_dir=args.trace_dir.resolve() if args.trace_dir is not None else None,
            source_content=source_content,
            source_mime_type=source_mime_type,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as error:
        print(f"live E2E failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
