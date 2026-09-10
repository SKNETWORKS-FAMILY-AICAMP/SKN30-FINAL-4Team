"""Offline contracts for the dependency-light reusable worker core.

These tests deliberately never start OCR, PostgreSQL, or an OpenAI request.
They guard the boundary that lets the worker be deployed without the retired
FastAPI application package.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from worker import vendor
from worker.adapters.openai_embedding_client import OpenAIEmbeddingClient
from worker.adapters.openai_llm_client import OpenAILLMClient
from worker.contracts.fit_result import FitStatus
from worker.contracts.sim_result import SimStatus
from worker.config import OpenAIConfig
from worker.cpl import build_cpl_result
from worker.fit import analyze_fit
from worker.sim import compare_candidate
from worker.sim_inputs import build_common_profile


_WORKER_DIR = Path(__file__).resolve().parents[1] / "worker"


def _worker_modules() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in _WORKER_DIR.rglob("*.py"):
        relative = path.relative_to(_WORKER_DIR)
        if relative.name == "__init__.py":
            suffix = relative.parts[:-1]
        else:
            suffix = relative.with_suffix("").parts
        name = "worker" + (f".{'.'.join(suffix)}" if suffix else "")
        modules[name] = path
    return modules


def _local_imports(
    *, module_name: str, path: Path, modules: dict[str, Path]
) -> set[str]:
    dependencies: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package_parts = package.split(".")
                if node.level > 1:
                    package_parts = package_parts[: -(node.level - 1)]
                imported_module = ".".join(
                    package_parts + ([node.module] if node.module else [])
                )
            else:
                imported_module = node.module or ""
            candidates = [imported_module]
            candidates.extend(
                f"{imported_module}.{alias.name}" for alias in node.names
            )
        else:
            continue
        for candidate in candidates:
            if candidate not in modules:
                continue
            dependencies.add(candidate)
            # Importing ``worker.adapters.openai_*`` executes both package
            # initialisers before the leaf module.  Keep those implicit
            # imports inside the guarded production graph as well.
            parts = candidate.split(".")
            for length in range(1, len(parts)):
                parent = ".".join(parts[:length])
                if parent in modules:
                    dependencies.add(parent)
    return dependencies


def _reachable_worker_modules(entrypoint: str) -> dict[str, Path]:
    modules = _worker_modules()
    pending = [entrypoint]
    reachable: dict[str, Path] = {}
    while pending:
        module_name = pending.pop()
        if module_name in reachable:
            continue
        path = modules[module_name]
        reachable[module_name] = path
        pending.extend(
            _local_imports(module_name=module_name, path=path, modules=modules)
        )
    return reachable


class _NoCallLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(self, **_values: object) -> BaseModel:
        self.calls += 1
        raise AssertionError("this deterministic fixture must be gated before an LLM call")


def _empty_profile(profile_id: str) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "schema_version": "request_profile.v0.1.2",
        "processing_metadata": {
            "common_ir_document_id": f"ir:{profile_id}",
            "pipeline_version": "request-profile-v0.1.2",
        },
        "comparison_profile": {},
        "request_context": {},
        "field_states": [],
    }


def test_vendor_paths_point_to_current_handover_packages() -> None:
    assert vendor.VENDOR_PATHS[0].is_dir()
    assert vendor.VENDOR_PATHS[1].is_dir()

    import common_ir_pipeline.schema  # noqa: F401
    import semantic_structuring.request_profile_v012  # noqa: F401


def test_production_worker_import_graph_has_no_dependency_on_retired_app_packages() -> None:
    active_modules = _reachable_worker_modules("worker.main")
    assert {
        "worker.analysis_job",
        "worker.postgres_analysis_store",
        "worker.postgres_repository",
        "worker.result_payload",
        "worker.retrieval_inputs",
        "worker.runtime",
        "worker.supabase_storage",
    } <= active_modules.keys()

    retired = (
        "app.db",
        "app.infrastructure.in_process_job_dispatcher",
        "app.infrastructure.local_object_storage",
        "app.ports.embedding_client",
        "app.ports.llm_client",
        "app.ports.object_storage",
        "app.ports.pdf_renderer",
        "app.schemas",
        "app.services.retrieval",
    )
    for module_name, path in active_modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.append(node.module)
        assert not any(module.startswith(retired) for module in imported), module_name


def test_production_worker_import_graph_is_importable() -> None:
    for module_name in sorted(_reachable_worker_modules("worker.main")):
        importlib.import_module(module_name)


def test_empty_profile_is_deterministically_gated_through_cpl_fit_and_sim() -> None:
    request = _empty_profile("request-1")
    candidate = _empty_profile("existing-1")
    no_llm = _NoCallLLM()

    cpl = build_cpl_result(request)
    assert len(cpl.items) == 13
    assert cpl.items[0].representative_status == "no_content"
    assert all(item.representative_status == "needs_confirmation" for item in cpl.items[1:])

    fit = analyze_fit(request, no_llm, model_profile="default")
    assert len(fit.relations) == 7
    assert all(relation.status is FitStatus.INSUFFICIENT for relation in fit.relations)

    request_common = build_common_profile(request, no_llm, model_profile="default")
    candidate_common = build_common_profile(candidate, no_llm, model_profile="default")
    comparison = compare_candidate(
        request_common, candidate_common, no_llm, model_profile="default"
    )
    assert len(comparison.axes) == 4
    assert all(axis.status is SimStatus.INSUFFICIENT for axis in comparison.axes)
    assert no_llm.calls == 0


class _ResultSchema(BaseModel):
    value: str


class _FakeChatCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response_format = kwargs["response_format"]
        parsed = None
        try:
            parsed = response_format.model_validate_json(self.content)
        except Exception:
            pass
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=self.content,
                        parsed=parsed,
                        refusal=None,
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model="text-embedding-test",
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            ],
            usage=SimpleNamespace(prompt_tokens=3, total_tokens=3),
        )


def test_openai_llm_rejects_an_empty_message_set_before_network() -> None:
    chat = _FakeChatCompletions('{"value":"grounded"}')
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        timeout_seconds=12,
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )
    with pytest.raises(ValueError, match="messages"):
        asyncio.run(
            llm.generate_structured(
                task_name="worker_core_test",
                messages=[],
                response_schema=_ResultSchema,
                model_profile="default",
            )
        )
    assert chat.calls == []


def test_openai_config_keeps_model_ids_in_environment_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_LLM_MODEL", "configured-llm")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "configured-embedding")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "17.5")

    config = OpenAIConfig.from_env()
    assert config.timeout_seconds == 17.5
    assert config.llm_model_profiles("profile", "repair") == {
        "profile": "configured-llm",
        "repair": "configured-llm",
    }


def test_openai_adapters_offline_happy_path_and_schema_failure() -> None:
    chat = _FakeChatCompletions('{"value":"grounded"}')
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        timeout_seconds=12,
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )
    from worker.ports.llm import Message, LLMInvalidResponseError

    result = asyncio.run(
        llm.generate_structured(
            task_name="worker_core_test",
            messages=[Message(role="user", content="minimal payload")],
            response_schema=_ResultSchema,
            model_profile="default",
        )
    )
    assert result == _ResultSchema(value="grounded")
    assert chat.calls[0]["model"] == "gpt-test"
    assert chat.calls[0]["timeout"] == 12.0
    assert chat.calls[0]["response_format"] is _ResultSchema
    assert "temperature" not in chat.calls[0]

    broken = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeChatCompletions('{"wrong":1}'))
        ),
    )
    with pytest.raises(LLMInvalidResponseError) as error:
        asyncio.run(
            broken.generate_structured(
                task_name="worker_core_test",
                messages=[Message(role="user", content="minimal payload")],
                response_schema=_ResultSchema,
                model_profile="default",
            )
        )
    assert error.value.raw == {"wrong": 1}

    embeddings = _FakeEmbeddings()
    embedding_client = OpenAIEmbeddingClient(
        api_key="test-key-not-a-real-secret",
        model_name="text-embedding-test",
        timeout_seconds=9,
        client=SimpleNamespace(embeddings=embeddings),
    )
    batch = asyncio.run(embedding_client.embed(["first", "second"]))
    assert batch.model_name == "text-embedding-test"
    assert batch.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert embeddings.calls[0]["model"] == "text-embedding-test"
    assert embeddings.calls[0]["timeout"] == 9.0
