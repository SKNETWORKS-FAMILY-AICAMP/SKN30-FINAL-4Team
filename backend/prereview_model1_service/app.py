"""Small authenticated HTTP surface for resident Model 1 inference."""

from __future__ import annotations

from asyncio import CancelledError, Lock, create_task, shield, to_thread
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from hmac import compare_digest
import json
import re
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .runtime import EXPECTED_WEIGHT_SHA256, Model1Runtime, Model1RuntimeError, canonical_input_digest
from .settings import Model1ServiceSettings, load_settings

__all__ = ["create_app", "create_app_from_environment"]


_MAX_JSON_DEPTH = 16
_IDEMPOTENCY = re.compile(r"[0-9a-f]{64}")
_FIELDS = ("title", "purpose", "content", "target_text")


def create_app(*, settings: Model1ServiceSettings, runtime: Model1Runtime | None = None) -> FastAPI:
    """Create a docs-disabled, one-GPU-at-a-time Model 1 service."""

    service_runtime = runtime or Model1Runtime(
        runtime_directory=settings.runtime_directory,
        preprocessor_path=settings.preprocessor_path,
        require_cuda=settings.require_cuda,
        device=settings.device,
    )
    lock = Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Put blocking imports/model construction off the event loop.  A
        # failed warmup prevents Uvicorn from accepting connections at all.
        await to_thread(service_runtime.load_and_warm)
        yield

    app = FastAPI(
        title="PreReview Model 1 service",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.model1_runtime = service_runtime

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

    @app.get("/v1/model1/ready", dependencies=[Depends(authorize)])
    async def ready() -> dict[str, object]:
        return {
            "ready": True,
            "max_concurrency": 1,
            "max_request_bytes": settings.max_request_bytes,
            "device": settings.device,
            "producer": _producer(service_runtime),
        }

    @app.post("/v1/model1/predict", dependencies=[Depends(authorize)])
    async def predict(request: Request) -> JSONResponse:
        raw = await _bounded_json(request, max_bytes=settings.max_request_bytes)
        fields = _parse_input(raw)
        digest = canonical_input_digest(fields)
        idempotency = _single_header(request, "idempotency-key")
        if idempotency is None or _IDEMPOTENCY.fullmatch(idempotency) is None or not compare_digest(idempotency, digest):
            raise HTTPException(status_code=400, detail="idempotency_key_invalid")
        # Do not queue raw document text in process memory.  One request owns
        # the resident GPU; callers retry only after a short explicit 429.
        if lock.locked():
            raise HTTPException(
                status_code=429,
                detail="worker_capacity_exceeded",
                headers={"Retry-After": "1"},
            )
        async with lock:
            try:
                prediction = await _run_sync_without_abandoning(
                    service_runtime.predict,
                    fields,
                )
            except Model1RuntimeError:
                raise HTTPException(status_code=503, detail="worker_unavailable") from None
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": "prereview.model1-predict-result/v1",
                "input_digest": digest,
                "producer": _producer(service_runtime),
                "prediction": prediction,
            },
        )

    return app


def create_app_from_environment() -> FastAPI:
    return create_app(settings=load_settings())


def _producer(runtime: Model1Runtime) -> dict[str, str]:
    return {
        "service": "prereview-model1",
        "weight_sha256": EXPECTED_WEIGHT_SHA256,
        "runtime_manifest_sha256": runtime.runtime_manifest_sha256,
    }


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else None


async def _run_sync_without_abandoning(function: Any, *args: object) -> Any:
    """Keep the inference slot until its worker thread really stops.

    Cancelling ``asyncio.to_thread()`` only cancels the awaiting coroutine; it
    cannot stop the underlying native thread.  Waiting for that thread before
    propagating cancellation prevents a disconnected client from releasing
    the one-model-at-a-time lock while inference is still running.
    """

    task = create_task(to_thread(function, *args))
    try:
        return await shield(task)
    except CancelledError as cancellation:
        while not task.done():
            try:
                await shield(task)
            except CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise cancellation


def _parse_input(raw: Mapping[str, object]) -> dict[str, str]:
    if set(raw) != {"input"} or not isinstance(raw.get("input"), Mapping):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    input_value = raw["input"]
    if set(input_value) != set(_FIELDS):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    fields: dict[str, str] = {}
    for field in _FIELDS:
        value = input_value[field]
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="prediction_request_invalid")
        fields[field] = value
    if not any(value.strip() for value in fields.values()):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    return fields


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
            raise HTTPException(status_code=400, detail="prediction_request_invalid") from None
        if declared_size < 0 or declared_size > max_bytes:
            raise HTTPException(status_code=413, detail="prediction_request_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="prediction_request_too_large")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    try:
        value = json.loads(
            body,
            object_pairs_hook=_no_duplicate_object,
            parse_constant=_reject_nonfinite,
        )
        _assert_depth(value, depth=0)
    except Exception:
        raise HTTPException(status_code=400, detail="prediction_request_invalid") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    return value


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> object:
    raise ValueError("non-finite JSON")


def _assert_depth(value: object, *, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("JSON too deep")
    if isinstance(value, Mapping):
        for nested in value.values():
            _assert_depth(nested, depth=depth + 1)
    elif isinstance(value, list):
        for nested in value:
            _assert_depth(nested, depth=depth + 1)
