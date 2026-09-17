"""Offline safety tests for the RunPod SDK entrypoint wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from prereview_runpod_worker.surya_layout_worker import entrypoint
from prereview_runpod_worker.surya_layout_worker.settings import RunPodSuryaWorkerComposition


FALLBACK = {"state": "infra_retryable", "reason_code": "worker_unavailable"}


class _CoreHandler:
    def __init__(self, response: object | BaseException) -> None:
        self.response = response
        self.events: list[object] = []

    def handle(self, event: object) -> object:
        self.events.append(event)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _composition(response: object | BaseException, *, max_bytes: int = 10_000) -> tuple[RunPodSuryaWorkerComposition, _CoreHandler]:
    core = _CoreHandler(response)
    composition = RunPodSuryaWorkerComposition(
        handler=core,  # type: ignore[arg-type]
        policy=SimpleNamespace(max_terminal_output_bytes=max_bytes),  # type: ignore[arg-type]
    )
    return composition, core


def test_safe_handler_composes_once_at_cold_start_and_forwards_bounded_core_output() -> None:
    composition, core = _composition({"state": "succeeded", "result": {"id": "x"}})
    calls = 0

    def factory() -> RunPodSuryaWorkerComposition:
        nonlocal calls
        calls += 1
        return composition

    handler = entrypoint._SafeHandler(factory)
    assert calls == 1
    assert handler.ready is True
    assert handler({"id": "job-1", "input": {}}) == {"state": "succeeded", "result": {"id": "x"}}
    assert handler({"id": "job-2", "input": {}})["state"] == "succeeded"
    assert calls == 1
    assert len(core.events) == 2


def test_startup_failure_is_constant_redacted_and_never_retried_per_job() -> None:
    calls = 0

    def factory() -> RunPodSuryaWorkerComposition:
        nonlocal calls
        calls += 1
        raise RuntimeError("https://signed.example.test/?secret=must-not-escape")

    handler = entrypoint._SafeHandler(factory)
    assert handler.ready is False
    assert handler({"id": "job", "input": {}}) == FALLBACK
    assert handler({"id": "job", "input": {}}) == FALLBACK
    assert calls == 1


def test_keyboard_interrupt_and_system_exit_are_not_hidden_by_the_safe_boundary() -> None:
    with pytest.raises(KeyboardInterrupt):
        entrypoint._SafeHandler(
            lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        )

    composition, _ = _composition(SystemExit(12))
    handler = entrypoint._SafeHandler(lambda: composition)
    with pytest.raises(SystemExit, match="12"):
        handler({"id": "job", "input": {}})


def test_invalid_event_and_invocation_exception_are_constant_redacted() -> None:
    composition, core = _composition(RuntimeError("storage=https://secret.example.test"))
    handler = entrypoint._SafeHandler(lambda: composition)

    assert handler(["not", "a", "mapping"]) == FALLBACK
    assert core.events == []
    assert handler({"id": "job", "input": {}}) == FALLBACK
    assert "secret" not in str(handler({"id": "job", "input": {}}))


@pytest.mark.parametrize(
    "response",
    [
        "not an object",
        {"state": "succeeded", "raw": float("nan")},
        {"state": "succeeded", "raw": "x" * 1_000},
    ],
)
def test_entrypoint_replaces_non_json_or_oversized_top_level_responses(response: object) -> None:
    composition, _ = _composition(response, max_bytes=100)
    handler = entrypoint._SafeHandler(lambda: composition)

    assert handler({"id": "job", "input": {}}) == FALLBACK


def test_sdk_loader_requires_the_exact_reviewed_distribution_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entrypoint, "distribution_version", lambda _: "1.12.1")
    monkeypatch.setattr(entrypoint, "import_module", lambda _: (_ for _ in ()).throw(AssertionError("must not import")))

    with pytest.raises(RuntimeError, match="^RunPod Surya worker unavailable$"):
        entrypoint._load_runpod_sdk()


def test_main_passes_the_single_safe_handler_to_runpod(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class Serverless:
        @staticmethod
        def start(value: dict[str, object]) -> None:
            calls.append(value)

    fake_runpod = SimpleNamespace(serverless=Serverless())
    composition, _ = _composition({"state": "succeeded"})
    fake_safe_handler = entrypoint._SafeHandler(lambda: composition)
    monkeypatch.setattr(entrypoint, "safe_handler", fake_safe_handler)
    monkeypatch.setattr(entrypoint, "_load_runpod_sdk", lambda: fake_runpod)

    entrypoint.main()

    assert calls == [{"handler": fake_safe_handler}]


def test_main_refuses_to_start_when_cold_start_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = entrypoint._SafeHandler(
        lambda: (_ for _ in ()).throw(RuntimeError("sensitive startup issue"))
    )
    monkeypatch.setattr(entrypoint, "safe_handler", unavailable)

    with pytest.raises(RuntimeError, match="^RunPod Surya worker unavailable$"):
        entrypoint.main()
