"""FastAPI surface for a Tailscale-only persistent RunPod."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from hmac import compare_digest
import json
import re
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from .service import (
    JobCapacityExceeded,
    JobConflict,
    JobNotCancellable,
    JobSubmissionInvalid,
    PersistentJobService,
)
from .settings import PersistentApiSettings, load_persistent_api_settings
from .store import DurableJobStore, JobStoreError
from ..surya_layout_worker.settings import build_worker_composition, load_worker_settings
from worker.contracts.accelerator import SuryaLayoutRequest

__all__ = ["create_persistent_app", "create_persistent_app_from_environment"]


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{15,63}")
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MAX_JSON_DEPTH = 32


def create_persistent_app(
    *,
    handler: object,
    settings: PersistentApiSettings,
    store: DurableJobStore | None = None,
) -> FastAPI:
    """Create an app with one GPU consumer and no credential persistence."""

    job_store = store or DurableJobStore(
        settings.state_directory,
        max_records=settings.max_job_records,
        mac_key=settings.journal_mac_key,
    )
    service = PersistentJobService(
        handler=handler,  # type: ignore[arg-type]
        store=job_store,
        settings=settings,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        job_store.open()
        try:
            service.start()
            yield
        finally:
            clean = await service.shutdown()
            if clean:
                job_store.close()

    app = FastAPI(
        title="PreReview persistent Surya worker",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.persistent_job_service = service

    async def authorize(request: Request) -> None:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise HTTPException(status_code=401, detail="unauthorized")
        scheme, separator, supplied = values[0].partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not supplied
            or not compare_digest(supplied, settings.bearer_token)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(JobStoreError)
    async def job_store_unavailable(_: Request, __: JobStoreError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "worker_unavailable"})

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok" if service.accepting else "not_ready",
            "ready": service.accepting,
            "queue_depth": service.queue_depth,
            "queue_capacity": service.queue_capacity,
            "gpu_concurrency": 1,
        }

    @app.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def submit_job(
        request: Request,
        _: None = Depends(authorize),
    ) -> JSONResponse:
        raw = await _bounded_json(request, max_bytes=settings.max_request_bytes)
        if set(raw) != {"input"} or not isinstance(raw["input"], Mapping):
            raise HTTPException(status_code=400, detail="job_request_invalid")
        idempotency_values = request.headers.getlist("idempotency-key")
        if len(idempotency_values) != 1:
            raise HTTPException(status_code=400, detail="idempotency_key_invalid")
        requested_id = idempotency_values[0]
        if _IDEMPOTENCY_KEY.fullmatch(requested_id) is None:
            raise HTTPException(status_code=400, detail="idempotency_key_invalid")
        try:
            parsed_request = SuryaLayoutRequest.from_wire_payload(raw["input"])
        except Exception:
            raise HTTPException(status_code=400, detail="job_request_invalid") from None
        if requested_id != parsed_request.logical_compute_key:
            raise HTTPException(status_code=400, detail="idempotency_key_invalid")
        try:
            record, created = service.submit(
                raw["input"],
                requested_job_id=requested_id,
            )
        except JobSubmissionInvalid:
            raise HTTPException(status_code=400, detail="job_request_invalid") from None
        except JobConflict:
            raise HTTPException(status_code=409, detail="idempotency_conflict") from None
        except JobCapacityExceeded:
            raise HTTPException(
                status_code=429,
                detail="worker_capacity_exceeded",
                headers={"Retry-After": "5"},
            ) from None
        return JSONResponse(
            status_code=202 if created else 200,
            content=record.public_dict(),
            headers={"Location": f"/jobs/{record.job_id}"},
        )

    @app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
    async def get_job(job_id: str) -> dict[str, Any]:
        if _JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        record = service.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return record.public_dict()

    @app.delete(
        "/jobs/{job_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(authorize)],
    )
    async def cancel_job(job_id: str) -> Response:
        if _JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        try:
            record = service.cancel(job_id)
        except JobNotCancellable:
            raise HTTPException(status_code=409, detail="job_not_cancellable") from None
        if record is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return Response(status_code=204)

    @app.post(
        "/jobs/{job_id}/cancel",
        dependencies=[Depends(authorize)],
    )
    async def cancel_job_with_status(job_id: str) -> dict[str, Any]:
        """AcceleratorPort-compatible cancel returning the terminal status."""

        if _JOB_ID.fullmatch(job_id) is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        try:
            record = service.cancel(job_id)
        except JobNotCancellable:
            raise HTTPException(status_code=409, detail="job_not_cancellable") from None
        if record is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return record.public_dict()

    return app


def create_persistent_app_from_environment() -> FastAPI:
    """Production factory: compose the pinned adapter before opening a port."""

    settings = load_persistent_api_settings()
    composition = build_worker_composition(load_worker_settings())
    return create_persistent_app(
        handler=composition.handler,
        settings=settings,
    )


async def _bounded_json(request: Request, *, max_bytes: int) -> dict[str, object]:
    if request.headers.get("content-encoding") not in {None, "", "identity"}:
        raise HTTPException(status_code=415, detail="content_encoding_unsupported")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="content_type_unsupported")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="job_request_invalid") from None
        if declared_size < 0 or declared_size > max_bytes:
            raise HTTPException(status_code=413, detail="job_request_too_large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="job_request_too_large")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="job_request_invalid")
    try:
        parsed = json.loads(
            body,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_constant,
        )
        _assert_json_depth(parsed, depth=0)
    except Exception:
        raise HTTPException(status_code=400, detail="job_request_invalid") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="job_request_invalid")
    return parsed


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise ValueError("non-finite JSON")


def _assert_json_depth(value: object, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON depth exceeded")
    if isinstance(value, dict):
        for nested in value.values():
            _assert_json_depth(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _assert_json_depth(nested, depth=depth + 1)
