"""Small authenticated HTTP surface for resident Model 2/3 inference."""

from __future__ import annotations

from asyncio import CancelledError, Lock, create_task, shield, to_thread
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from hmac import compare_digest
import json
import re
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .contract import (
    MODEL23_MAX_CONCURRENCY,
    MODEL2_RESULT_SCHEMA_VERSION,
    MODEL3_RESULT_SCHEMA_VERSION,
)
from .runtime import (
    EXPECTED_MODEL2_BUNDLE_SHA256,
    EXPECTED_MODEL3_POOL_SHA256,
    Model23Runtime,
    Model23RuntimeError,
    canonical_input_digest,
)
from .settings import Model23ServiceSettings, load_settings

__all__ = ["create_app", "create_app_from_environment"]


_MAX_JSON_DEPTH = 16
_IDEMPOTENCY = re.compile(r"[0-9a-f]{64}")
_QUANTITY_KEYS = {"support_scale", "cost_sharing", "support_period", "total_budget"}
_CARRY_KEYS = {
    "support_type",
    "support_type_status",
    "support_type_model_version",
}
_MODEL2_BASE_KEYS = {"title", "evidence_text", "quantities"}
_MODEL3_BASE_KEYS = {"evidence_text", "quantities"}


def create_app(*, settings: Model23ServiceSettings, runtime: Model23Runtime | None = None) -> FastAPI:
    """Create a docs-disabled service with one shared CPU inference slot."""

    service_runtime = runtime or Model23Runtime(ml_root=settings.ml_root)
    lock = Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await to_thread(service_runtime.load_and_warm)
        yield

    app = FastAPI(
        title="PreReview Model 2/3 service",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.model23_runtime = service_runtime

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

    @app.get("/v1/model23/ready", dependencies=[Depends(authorize)])
    async def ready() -> dict[str, object]:
        return {
            "ready": True,
            "max_concurrency": MODEL23_MAX_CONCURRENCY,
            "max_request_bytes": settings.max_request_bytes,
            "device": "cpu",
            "producer": _producer(service_runtime),
        }

    async def execute(
        request: Request,
        *,
        model: Literal["model2", "model3"],
    ) -> JSONResponse:
        raw = await _bounded_json(request, max_bytes=settings.max_request_bytes)
        payload = _parse_input(raw, model=model)
        digest = canonical_input_digest(payload)
        idempotency = _single_header(request, "idempotency-key")
        if (
            idempotency is None
            or _IDEMPOTENCY.fullmatch(idempotency) is None
            or not compare_digest(idempotency, digest)
        ):
            raise HTTPException(status_code=400, detail="idempotency_key_invalid")
        # Do not queue Common IR bodies in process memory.  The caller receives
        # a bounded retry signal while either model owns the shared runtime.
        if lock.locked():
            raise HTTPException(
                status_code=429,
                detail="worker_capacity_exceeded",
                headers={"Retry-After": "1"},
            )
        async with lock:
            try:
                if model == "model2":
                    prediction = await _run_sync_without_abandoning(
                        service_runtime.predict_model2,
                        payload,
                    )
                    schema_version = MODEL2_RESULT_SCHEMA_VERSION
                else:
                    prediction = await _run_sync_without_abandoning(
                        service_runtime.predict_model3,
                        payload,
                    )
                    schema_version = MODEL3_RESULT_SCHEMA_VERSION
            except Model23RuntimeError:
                raise HTTPException(status_code=503, detail="worker_unavailable") from None
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "schema_version": schema_version,
                "input_digest": digest,
                "producer": _producer(service_runtime),
                "prediction": prediction,
            },
        )

    @app.post("/v1/model2/predict", dependencies=[Depends(authorize)])
    async def predict_model2(request: Request) -> JSONResponse:
        return await execute(request, model="model2")

    @app.post("/v1/model3/predict", dependencies=[Depends(authorize)])
    async def predict_model3(request: Request) -> JSONResponse:
        return await execute(request, model="model3")

    return app


def create_app_from_environment() -> FastAPI:
    return create_app(settings=load_settings())


def _producer(runtime: Model23Runtime) -> dict[str, str]:
    return {
        "service": "prereview-model23",
        "runtime_manifest_sha256": runtime.runtime_manifest_sha256,
        "model2_bundle_sha256": EXPECTED_MODEL2_BUNDLE_SHA256,
        "model3_pool_sha256": EXPECTED_MODEL3_POOL_SHA256,
    }


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    return values[0] if len(values) == 1 else None


async def _run_sync_without_abandoning(function: Any, *args: object) -> Any:
    """Keep the shared inference slot until its worker thread really stops."""

    task = create_task(to_thread(function, *args))
    try:
        return await shield(task)
    except CancelledError as cancellation:
        # ``to_thread`` cannot terminate its native thread.  Delay cancellation
        # propagation until it completes so the surrounding lock cannot admit
        # a second Model 2/3 call while the first one still mutates import caches.
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


def _parse_input(
    raw: Mapping[str, object],
    *,
    model: Literal["model2", "model3"],
) -> dict[str, object]:
    if set(raw) != {"input"} or not isinstance(raw.get("input"), Mapping):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    payload = dict(raw["input"])
    base_keys = _MODEL2_BASE_KEYS if model == "model2" else _MODEL3_BASE_KEYS
    keys = set(payload)
    if keys not in {frozenset(base_keys), frozenset(base_keys | _CARRY_KEYS)}:
        raise HTTPException(status_code=400, detail="prediction_request_invalid")

    evidence = payload.get("evidence_text")
    if evidence is not None and not isinstance(evidence, str):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    if model == "model2":
        if not isinstance(payload.get("title"), str) or not isinstance(evidence, str) or not evidence.strip():
            raise HTTPException(status_code=400, detail="prediction_request_invalid")

    quantities = payload.get("quantities")
    if not isinstance(quantities, Mapping) or not set(quantities).issubset(_QUANTITY_KEYS):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")
    has_quantity = False
    clean_quantities: dict[str, list[str]] = {}
    for key, values in quantities.items():
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise HTTPException(status_code=400, detail="prediction_request_invalid")
        clean_quantities[str(key)] = list(values)
        has_quantity = has_quantity or any(value.strip() for value in values)
    payload["quantities"] = clean_quantities
    if model == "model3" and not ((isinstance(evidence, str) and evidence.strip()) or has_quantity):
        raise HTTPException(status_code=400, detail="prediction_request_invalid")

    if _CARRY_KEYS.issubset(keys):
        if (
            not isinstance(payload.get("support_type"), str)
            or not payload["support_type"].strip()
            or not isinstance(payload.get("support_type_status"), str)
            or not payload["support_type_status"].strip()
            or (
                payload.get("support_type_model_version") is not None
                and not isinstance(payload.get("support_type_model_version"), str)
            )
        ):
            raise HTTPException(status_code=400, detail="prediction_request_invalid")
    return payload


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
