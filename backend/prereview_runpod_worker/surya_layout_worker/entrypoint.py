"""RunPod Serverless entrypoint with a deliberately tiny public failure surface.

Importing this module eagerly attempts local composition once, but never lets
configuration or optional-dependency errors abort a worker import.  RunPod,
Pillow and Surya are not imported here.  The first two are imported only by
their deployment paths, and RunPod itself is imported only by :func:`main`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from importlib import import_module
from importlib.metadata import version as distribution_version
import json
from threading import Lock
from typing import Any, Protocol

from .settings import RunPodSuryaWorkerComposition, build_worker_composition, load_worker_settings

__all__ = ["RUNPOD_SDK_VERSION", "main", "safe_handler"]


RUNPOD_SDK_DISTRIBUTION = "runpod"
RUNPOD_SDK_VERSION = "1.12.0"

_FALLBACK_RESPONSE: dict[str, str] = {
    "state": "infra_retryable",
    "reason_code": "worker_unavailable",
}
_MAIN_UNAVAILABLE_MESSAGE = "RunPod Surya worker unavailable"


class _HandlerLike(Protocol):
    def handle(self, event: Mapping[str, object]) -> Mapping[str, object]: ...


CompositionFactory = Callable[[], RunPodSuryaWorkerComposition]


class _SafeHandler:
    """Compose once and make every failure a fixed, non-diagnostic response."""

    def __init__(self, composition_factory: CompositionFactory) -> None:
        self._composition_factory = composition_factory
        self._lock = Lock()
        self._composition: RunPodSuryaWorkerComposition | None = None
        self._startup_attempted = False
        # RunPod imports the handler once per cold process.  Perform the
        # expensive adapter composition now, not once per invocation.
        self._ensure_composed()

    @property
    def ready(self) -> bool:
        return self._composition is not None

    def _ensure_composed(self) -> RunPodSuryaWorkerComposition | None:
        with self._lock:
            if self._startup_attempted:
                return self._composition
            self._startup_attempted = True
            try:
                composition = self._composition_factory()
                if not isinstance(composition, RunPodSuryaWorkerComposition):
                    raise TypeError("invalid worker composition")
                self._composition = composition
            except Exception:
                # Config/provider exceptions can contain paths, URLs or
                # environment details.  They are intentionally not logged or
                # retained as an exception object.
                self._composition = None
            return self._composition

    def __call__(self, event: object) -> dict[str, object]:
        composition = self._ensure_composed()
        if composition is None or not isinstance(event, Mapping):
            return _fallback_response()
        try:
            response = composition.handler.handle(event)
            return _bounded_response(
                response,
                max_bytes=composition.policy.max_terminal_output_bytes,
            )
        except Exception:
            return _fallback_response()


def _compose_from_environment() -> RunPodSuryaWorkerComposition:
    return build_worker_composition(load_worker_settings())


def _fallback_response() -> dict[str, object]:
    # Always return a fresh object.  A caller must not be able to mutate the
    # next invocation's terminal envelope through a shared module constant.
    return dict(_FALLBACK_RESPONSE)


def _bounded_response(response: object, *, max_bytes: int) -> dict[str, object]:
    """Return only a compact JSON object that fits the policy response cap."""

    if not isinstance(response, Mapping):
        return _fallback_response()
    try:
        copied = dict(response)
        encoded = json.dumps(
            copied,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        return _fallback_response()
    if len(encoded) > max_bytes:
        return _fallback_response()
    return copied


def _load_runpod_sdk() -> Any:
    """Load exactly the reviewed SDK version only when launching a worker."""

    try:
        if distribution_version(RUNPOD_SDK_DISTRIBUTION) != RUNPOD_SDK_VERSION:
            raise RuntimeError
        runpod = import_module("runpod")
        serverless = getattr(runpod, "serverless")
        if not callable(getattr(serverless, "start", None)):
            raise RuntimeError
    except Exception:
        raise RuntimeError(_MAIN_UNAVAILABLE_MESSAGE) from None
    return runpod


def main() -> None:
    """Start the reviewed SDK only after a successful cold-start composition."""

    if not safe_handler.ready:
        raise RuntimeError(_MAIN_UNAVAILABLE_MESSAGE)
    runpod = _load_runpod_sdk()
    try:
        runpod.serverless.start({"handler": safe_handler})
    except Exception:
        # ``main`` is an operator boundary, not a JSON handler response.  Its
        # fixed exception is still safe for a launcher/supervisor to display.
        raise RuntimeError(_MAIN_UNAVAILABLE_MESSAGE) from None


# This name is intentionally a callable object rather than a decorator.  The
# SDK receives it unchanged, and tests can construct isolated _SafeHandler
# instances without importing the optional RunPod package.
safe_handler = _SafeHandler(_compose_from_environment)


if __name__ == "__main__":  # pragma: no cover - exercised by the image command
    main()
