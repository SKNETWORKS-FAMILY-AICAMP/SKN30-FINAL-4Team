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
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
from uuid import uuid4

import httpx
from dotenv import dotenv_values


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
# Deployment default, and the retrieval breadth the stored traces under
# .runtime/pipeline-traces were produced with.  Keep runs comparable.
DEFAULT_TOP_K = 5


class E2EFailure(RuntimeError):
    """A safe stage-level failure that never contains credentials or content."""


def _required(values: dict[str, str | None], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise E2EFailure(f"required local setting is missing: {name}")
    return value


def _configure_environment(
    root_env: Path,
    supabase_env: Path,
    *,
    llm_model: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> None:
    if not 1 <= top_k <= 100:
        raise E2EFailure("--top-k must be between 1 and 100")
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


def _validated_source(source: Path) -> tuple[bytes, str]:
    """Read and validate the exact upload bytes and return their MIME type."""

    content = source.read_bytes()
    from app.models.pipeline import PipelineKind
    from app.pipelines.formats import validate_format

    try:
        decision = validate_format(PipelineKind.REQUEST, source, content=content)
    except ValueError as error:
        raise E2EFailure(f"--file format is invalid: {error}") from None
    return content, decision.mime_type


def _write_trace(
    trace_dir: Path,
    *,
    upload: object,
    common_ir: object,
    structured_profile: object,
    result: dict[str, object],
) -> None:
    """Write the actual E2E stage outputs without rerunning any stage."""

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
    )
    for name, payload in stages:
        (trace_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


async def _run(
    source: Path,
    *,
    trace_dir: Path | None = None,
    source_content: bytes | None = None,
    source_mime_type: str | None = None,
) -> dict[str, object]:
    if source_content is None or source_mime_type is None:
        source_content, source_mime_type = _validated_source(source)
    # Import only after the environment is complete: both composition roots
    # intentionally read their deployment configuration at construction time.
    from main import create_app
    from worker.main import build_worker
    from worker.runtime import WorkerRuntime

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

        composition = build_worker()
        runtime = WorkerRuntime(
            composition.repository,
            composition.handler,
            worker_id=f"local-e2e-{uuid4().hex[:12]}",
            heartbeat_seconds=composition.settings.heartbeat_seconds,
            lease_seconds=composition.settings.lease_seconds,
            idle_poll_seconds=composition.settings.idle_poll_seconds,
        )
        worker_outcome = await asyncio.to_thread(runtime.run_once)

        polled = await client.get(f"/api/v1/analysis-runs/{run_id}")
        if polled.status_code != 200:
            raise E2EFailure(
                f"FastAPI status polling failed with HTTP {polled.status_code}"
            )
        state = polled.json()
        if state.get("status") != "succeeded":
            raise E2EFailure(
                f"worker did not complete the run: outcome={worker_outcome.value}, "
                f"status={state.get('status')}"
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
        if not isinstance(body, dict):
            raise E2EFailure("FastAPI result response contract is invalid")
        if trace_dir is not None:
            # The persisted artifacts are the exact structure consumed by the
            # worker below; exporting them here avoids a second parse/LLM run.
            cached = composition.handler._store.cached_request_profile(
                analysis_run_id=run_id
            )
            if cached is None:
                raise E2EFailure("worker did not persist the structured profile")
            _write_trace(
                trace_dir,
                upload=payload,
                common_ir=composition.handler._load_json_artifact(cached.common_ir),
                structured_profile=composition.handler._load_json_artifact(
                    cached.structured_profile
                ),
                result=body,
            )
        outcome: dict[str, object] = {
            "status": "ok",
            "test_user_id": user_id,
            "analysis_run_id": run_id,
            "analysis_case_id": case_id,
            "worker_outcome": worker_outcome.value,
            "cpl_items": len(body.get("cpl", {}).get("items", [])),
            "fit_items": len(body.get("fit", {}).get("items", [])),
            "sim_candidates": len(body.get("sim", {}).get("candidates", [])),
            "evidences": len(body.get("evidences", [])),
        }
        if trace_dir is not None:
            outcome["trace_dir"] = str(trace_dir)
        return outcome


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
        help=(
            "SIM retrieval breadth for this run "
            f"(default: {DEFAULT_TOP_K}; the deployment default and the "
            "baseline the stored traces were produced with)"
        ),
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        help="new directory for actual Common IR, profile, CPL, FIT, SIM, ML, and result JSON",
    )
    args = parser.parse_args()
    if not args.file.is_file():
        raise E2EFailure("--file must be an existing HWP or HWPX file")
    source_content, source_mime_type = _validated_source(args.file)
    if args.trace_dir is not None and args.trace_dir.exists():
        raise E2EFailure("--trace-dir must not already exist")
    _configure_environment(
        args.backend_env,
        args.supabase_env,
        llm_model=args.llm_model,
        top_k=args.top_k,
    )
    result = asyncio.run(
        _run(
            args.file.resolve(),
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
