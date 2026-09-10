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
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import quote
from uuid import uuid4

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
SOURCE_MIME_TYPES = {
    ".hwp": "application/x-hwp",
    ".hwpx": "application/vnd.hancom.hwpx",
}
MAX_WORKER_ATTEMPTS = 2

# Keep every other non-terminal row locked in a separate transaction while the
# normal migration-21 claim function runs.  That function uses SKIP LOCKED, so
# this local one-shot can exercise exactly the run it created without consuming
# an attempt from an older queued request.  Locking live rows too closes the
# edge where another run's lease expires between a preflight query and claim.
_LOCK_OTHER_ACTIVE_RUNS_SQL = """
SELECT ar.analysis_run_pk
FROM workspace.analysis_run AS ar
JOIN workspace.analysis_run_dispatch AS dispatch
  ON dispatch.analysis_run_pk = ar.analysis_run_pk
WHERE ar.analysis_run_pk <> %s::uuid
  AND ar.status IN ('uploading', 'queued', 'running', 'cleanup_pending')
ORDER BY ar.created_at, ar.analysis_run_pk
FOR UPDATE OF ar, dispatch
"""
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


class E2EFailure(RuntimeError):
    """A safe stage-level failure that never contains credentials or content."""


class _UnexpectedClaim(E2EFailure):
    """The guarded queue function selected a run other than this E2E's run."""


def _claim_value(row: Mapping[str, Any], key: str) -> Any:
    value = row.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise E2EFailure("Local E2E queue returned an incomplete claim")
    return value


def _acquire_target_claim_guard(cursor: Any, target_run_id: str) -> None:
    """Acquire the short-lived transaction guard for one operator E2E claim.

    Only this setup is an *isolation acquisition* step.  The caller executes
    ``claim_next_analysis_run`` after this function returns, so a database
    error from the claim itself is not incorrectly reported as a failure to
    acquire the guard.  The transaction deliberately ends immediately after
    the target row is claimed; it must not hold queue row locks while the
    worker performs long-running parsing or LLM work.
    """

    try:
        # Connection timeout does not bound waits after connecting.  Fail
        # clearly instead of leaving this operator command hung behind another
        # worker or an abandoned transaction.
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        # Serialise concurrent copies of this operator-only script.  A regular
        # worker need not know about this lock: the row locks below make it
        # skip non-target work for the brief claim call.
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_E2E_QUEUE_ADVISORY_LOCK,),
        )
        cursor.execute(_LOCK_OTHER_ACTIVE_RUNS_SQL, (target_run_id,))
        cursor.fetchall()
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
                _acquire_target_claim_guard(cursor, target_run_id)
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


def _source_mime_type(source: Path) -> str:
    try:
        return SOURCE_MIME_TYPES[source.suffix.lower()]
    except KeyError:
        raise E2EFailure("--file must be an HWP or HWPX file") from None


def _required(values: dict[str, str | None], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise E2EFailure(f"required local setting is missing: {name}")
    return value


def _configure_environment(root_env: Path, supabase_env: Path) -> None:
    provider = dotenv_values(root_env)
    local = dotenv_values(supabase_env)
    password = _required(local, "POSTGRES_PASSWORD")
    tenant = _required(local, "POOLER_TENANT_ID")
    database = str(local.get("POSTGRES_DB") or "postgres").strip() or "postgres"
    username = quote(f"postgres.{tenant}", safe="")
    database_url = (
        f"postgresql://{username}:{quote(password, safe='')}"
        f"@127.0.0.1:5432/{quote(database, safe='')}?sslmode=disable"
    )

    settings = {
        "PREREVIEW_OFFLINE_MODE": "false",
        "PREREVIEW_AUTH_ALLOWED_ORIGINS": TEST_ORIGIN,
        "PREREVIEW_AUTH_COOKIE_SECURE": "false",
        "DATABASE_URL": database_url,
        "SUPABASE_URL": "http://127.0.0.1:8000",
        "SUPABASE_ANON_KEY": _required(local, "ANON_KEY"),
        "SUPABASE_SERVICE_ROLE_KEY": _required(local, "SERVICE_ROLE_KEY"),
        "OPENAI_API_KEY": _required(provider, "OPENAI_API_KEY"),
        "OPENAI_LLM_MODEL": str(
            provider.get("OPENAI_LLM_MODEL") or "gpt-5.6-luna"
        ),
        "OPENAI_EMBEDDING_MODEL": str(
            provider.get("OPENAI_EMBEDDING_MODEL") or "text-embedding-3-small"
        ),
        "OPENAI_TIMEOUT_SECONDS": str(
            provider.get("OPENAI_TIMEOUT_SECONDS") or "120"
        ),
        "PREREVIEW_WORKER_TOP_K": "1",
        "PREREVIEW_WORKER_PARSE_TIMEOUT_SECONDS": "120",
        "PREREVIEW_FREETYPE_LIB": "/lib/x86_64-linux-gnu/libfreetype.so.6",
    }
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
        if run_status == "succeeded":
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


async def _run(source: Path) -> dict[str, object]:
    # Import only after the environment is complete: both composition roots
    # intentionally read their deployment configuration at construction time.
    from main import create_app
    from worker.main import build_worker, configure_runtime_logging
    from worker.runtime import WorkerRuntime

    # OPENAI_LOG=debug can make the SDK log its full request options, including
    # the uploaded document text.  Apply the same transport-logger floor as the
    # long-running worker before any provider call in this operator entrypoint.
    configure_runtime_logging(level=os.getenv("LOG_LEVEL", "INFO").upper())

    user_id, email, password = await _create_confirmed_test_user()
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://e2e.local",
        timeout=60,
    ) as client:
        signed_in = await client.post(
            "/api/v1/auth/sign-in",
            headers={"Origin": TEST_ORIGIN},
            json={"email": email, "password": password},
        )
        if signed_in.status_code != 200:
            raise E2EFailure(
                f"FastAPI sign-in failed with HTTP {signed_in.status_code}"
            )

        uploaded = await client.post(
            "/api/v1/analysis-runs",
            headers={
                "Origin": TEST_ORIGIN,
                "Idempotency-Key": str(uuid4()),
            },
            files={
                "file": (
                    source.name,
                    source.read_bytes(),
                    _source_mime_type(source),
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

        composition = build_worker()
        target_repository = _TargetRunRepository(
            composition.repository,
            target_run_id=run_id,
            database_url=os.environ["DATABASE_URL"],
        )
        runtime = WorkerRuntime(
            target_repository,
            composition.handler,
            worker_id=f"local-e2e-{uuid4().hex[:12]}",
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
        case_id = state.get("analysis_case_id")
        if not isinstance(case_id, str) or not case_id:
            raise E2EFailure("completed run has no analysis case id")

        result = await client.get(f"/api/v1/analysis-cases/{case_id}")
        if result.status_code != 200:
            raise E2EFailure(
                f"FastAPI result read failed with HTTP {result.status_code}"
            )
        body = result.json()
        return {
            "status": "ok",
            "test_user_id": user_id,
            "analysis_run_id": run_id,
            "analysis_case_id": case_id,
            "worker_outcome": worker_outcome,
            "worker_attempts": worker_attempts,
            "cpl_items": len(body.get("cpl", {}).get("items", [])),
            "fit_items": len(body.get("fit", {}).get("items", [])),
            "sim_candidates": len(body.get("sim", {}).get("candidates", [])),
            "evidences": len(body.get("evidences", [])),
        }


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
    args = parser.parse_args()
    if not args.file.is_file():
        raise E2EFailure("--file must be an existing HWP or HWPX file")
    _source_mime_type(args.file)
    _configure_environment(args.backend_env, args.supabase_env)
    result = asyncio.run(_run(args.file.resolve()))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except E2EFailure as error:
        print(f"live E2E failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
