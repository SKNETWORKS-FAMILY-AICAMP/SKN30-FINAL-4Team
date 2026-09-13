"""Offline contracts for the dependency-light reusable worker core.

These tests deliberately never start OCR, PostgreSQL, or an OpenAI request.
They guard the boundary that lets the worker be deployed without the retired
FastAPI application package.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from jsonschema import validate as validate_json_schema
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

    fit = analyze_fit(cpl, no_llm, model_profile="default")
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
    def __init__(
        self,
        content: str,
        *,
        finish_reason: str | None = "stop",
        refusal: str | None = None,
    ) -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.refusal = refusal
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason,
                    message=SimpleNamespace(
                        content=self.content,
                        refusal=self.refusal,
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


def test_openai_config_defaults_to_a_document_safe_timeout() -> None:
    config = OpenAIConfig.from_env(
        {
            "OPENAI_API_KEY": "test",
            "OPENAI_LLM_MODEL": "configured-llm",
            "OPENAI_EMBEDDING_MODEL": "configured-embedding",
        }
    )

    assert config.timeout_seconds == 120.0
    assert config.max_repairs == 2


def test_openai_config_uses_stage_model_overrides_without_changing_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_LLM_MODEL", "configured-luna")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "configured-embedding")
    monkeypatch.setenv("OPENAI_REQUEST_PROFILE_MODEL", "configured-terra")
    monkeypatch.setenv("OPENAI_FIT_MODEL", "configured-fit")
    monkeypatch.setenv("OPENAI_SIM_MODEL", "configured-sim")
    monkeypatch.setenv("OPENAI_CHAT_MODEL", "configured-chat")

    config = OpenAIConfig.from_env()

    assert config.llm_model_profiles("request_profile", "fit", "sim", "chat", "other") == {
        "request_profile": "configured-terra",
        "fit": "configured-fit",
        "sim": "configured-sim",
        "chat": "configured-chat",
        "other": "configured-luna",
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
    response_format = chat.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "_ResultSchema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert chat.calls[0]["n"] == 1
    assert chat.calls[0]["store"] is False
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


def test_openai_llm_preserves_cross_field_invalid_raw_for_normalization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from semantic_structuring.request_profile_v012 import RequestSourceSelectionV012
    from semantic_structuring.run_request_profile_v012 import (
        _normalize_remote_selection_payload,
    )
    from worker.ports.llm import LLMInvalidResponseError, Message

    raw = {
        "profile_id": "request:test",
        "candidate_pack_id": "pack:test",
        "program_hierarchy": [],
        "facts": [
            {
                "fact_id": "fact:purpose",
                "field_name": "purpose_goal",
                "value_anchor": {
                    "source_block_id": "block:purpose",
                    "anchor_text": "private-cross-field-sentinel",
                    "value_span_candidate_id": "span:purpose",
                },
                "context_source_block_ids": [],
                "status": "identified",
                "primary_component_id": None,
                "program_node_id": None,
            }
        ],
        "support_components": [],
        "delivery_relations": [],
        "delivery_methods": [],
        "field_states": [],
    }
    chat = _FakeChatCompletions(json.dumps(raw, ensure_ascii=False))
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"request_profile": "gpt-test"},
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(LLMInvalidResponseError) as error:
        asyncio.run(
            llm.generate_structured(
                task_name="request_source_selection_v012",
                messages=[Message(role="user", content="minimal payload")],
                response_schema=RequestSourceSelectionV012,
                model_profile="request_profile",
            )
        )

    assert error.value.raw == raw
    response_format = chat.calls[0]["response_format"]
    validate_json_schema(raw, response_format["json_schema"]["schema"])
    normalized = _normalize_remote_selection_payload(error.value.raw)
    repaired = RequestSourceSelectionV012.model_validate(normalized)
    assert repaired.facts[0].value_anchor.value_span_candidate_id == "span:purpose"
    assert repaired.facts[0].value_anchor.anchor_text is None
    assert "private-cross-field-sentinel" not in str(error.value)
    assert "private-cross-field-sentinel" not in repr(error.value)
    assert "private-cross-field-sentinel" not in caplog.text


def test_openai_llm_records_usage_before_invalid_response_without_content_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from worker.ports.llm import LLMInvalidResponseError, Message

    sentinel = "private-invalid-response-sentinel"
    chat = _FakeChatCompletions(json.dumps({"wrong": sentinel}))
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )
    caplog.set_level(logging.INFO, logger="worker.adapters.openai_llm_client")

    with pytest.raises(LLMInvalidResponseError):
        asyncio.run(
            llm.generate_structured(
                task_name="invalid_response_usage_test",
                messages=[Message(role="user", content="private prompt content")],
                response_schema=_ResultSchema,
                model_profile="default",
            )
        )

    assert (
        "OpenAI structured response received task=invalid_response_usage_test "
        "model=gpt-test prompt_tokens=4 completion_tokens=2 total_tokens=6"
    ) in caplog.text
    assert "structured request completed" not in caplog.text
    assert sentinel not in caplog.text
    assert "private prompt content" not in caplog.text


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        (
            {
                "kind": "paragraph",
                "source_block_id": "block:paragraph",
                "anchor_text": "문단 전체",
                "common_ir_block_id": "common:block:paragraph",
                "row_index": 3,
            },
            {
                "kind": "paragraph",
                "source_block_id": "block:paragraph",
                "anchor_text": "문단 전체",
                "common_ir_block_id": "common:block:paragraph",
            },
        ),
        (
            {
                "kind": "table_row",
                "source_block_id": "redundant:block",
                "anchor_text": "redundant text",
                "common_ir_block_id": "common:table",
                "row_index": 2,
            },
            {
                "kind": "table_row",
                "common_ir_block_id": "common:table",
                "row_index": 2,
            },
        ),
        (
            {
                "kind": "table_column_pair",
                "source_block_id": "redundant:block",
                "anchor_text": "redundant text",
                "common_ir_block_id": "common:table",
                "row_index": 2,
            },
            {
                "kind": "table_column_pair",
                "common_ir_block_id": "common:table",
            },
        ),
    ],
)
def test_remote_relation_container_normalization_is_kind_specific(
    container: dict[str, object],
    expected: dict[str, object],
) -> None:
    from semantic_structuring.request_profile_v012 import RequestSourceSelectionV012
    from semantic_structuring.run_request_profile_v012 import (
        _normalize_remote_selection_payload,
    )

    raw = {
        "profile_id": "request:test",
        "candidate_pack_id": "pack:test",
        "delivery_relations": [
            {
                "delivery_relation_id": "relation:test",
                "actor_anchor": {
                    "source_block_id": "block:actor",
                    "anchor_text": "actor",
                },
                "role_anchor": {
                    "source_block_id": "block:role",
                    "anchor_text": "role",
                },
                "actions": [],
                "relation_container": container,
            }
        ],
    }

    normalized = _normalize_remote_selection_payload(raw)
    assert normalized["delivery_relations"][0]["relation_container"] == expected
    RequestSourceSelectionV012.model_validate(normalized)


@pytest.mark.parametrize(
    "chat",
    [
        _FakeChatCompletions(
            '{"value":"private partial content"}', finish_reason="length"
        ),
        _FakeChatCompletions(
            '{"value":"well-formed private content"}',
            refusal="private refusal content",
        ),
    ],
)
def test_openai_llm_handles_incomplete_and_refusal_without_content_leak(
    chat: _FakeChatCompletions,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from worker.ports.llm import LLMInvalidResponseError, Message

    caplog.set_level(logging.DEBUG)
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )
    with pytest.raises(LLMInvalidResponseError) as error:
        asyncio.run(
            llm.generate_structured(
                task_name="worker_core_test",
                messages=[Message(role="user", content="private prompt content")],
                response_schema=_ResultSchema,
                model_profile="default",
            )
        )

    assert error.value.raw is None
    assert str(error.value) == "OpenAI returned an invalid structured response"
    assert "private" not in caplog.text


def test_openai_llm_rejects_missing_finish_reason_without_raw_content() -> None:
    from worker.ports.llm import LLMInvalidResponseError, Message

    chat = _FakeChatCompletions(
        '{"value":"private complete-looking content"}',
        finish_reason=None,
    )
    llm = OpenAILLMClient(
        api_key="test-key-not-a-real-secret",
        model_profiles={"default": "gpt-test"},
        client=SimpleNamespace(chat=SimpleNamespace(completions=chat)),
    )

    with pytest.raises(LLMInvalidResponseError) as error:
        asyncio.run(
            llm.generate_structured(
                task_name="worker_core_test",
                messages=[Message(role="user", content="private prompt content")],
                response_schema=_ResultSchema,
                model_profile="default",
            )
        )

    assert error.value.raw is None


def test_request_completeness_failure_cannot_be_delivery_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semantic_structuring.request_profile_v012 import (
        RequestCompletenessError,
        RequestSourceSelectionV012,
    )
    from semantic_structuring.run_request_profile_v012 import (
        RequestMaterializationError,
    )
    from worker import profiles as worker_profiles

    selection = RequestSourceSelectionV012.model_validate({
        "profile_id": "request-completeness-isolation",
        "candidate_pack_id": "pack-completeness-isolation",
    })
    cause = RequestCompletenessError(
        "Request completeness failed: implementation_plan is missing",
        materialized_evidence_keys=frozenset(),
    )
    error = RequestMaterializationError(
        cause,
        selection=selection,
        retry_count=1,
        diagnostics=[],
        usage=[],
    )

    def unexpected_assembly(*args: object, **kwargs: object) -> object:
        raise AssertionError("delivery isolation must not retry completeness errors")

    monkeypatch.setattr(
        worker_profiles,
        "assemble_request_profile_v012",
        unexpected_assembly,
    )

    assert worker_profiles._materialize_isolated(
        error,
        document={},
        pack=object(),
        model_id="mock-luna",
    ) is None

    later_generic_error = RequestMaterializationError(
        ValueError("generic relation materialization failure"),
        selection=selection,
        retry_count=1,
        diagnostics=[{
            "repair_attempt": 1,
            "error_type": "RequestCompletenessError",
            "validation_error": "safe completeness diagnostic",
        }, {
            "repair_attempt": 2,
            "error_type": "ValueError",
            "validation_error": "safe relation diagnostic",
        }],
        usage=[],
    )
    assert worker_profiles._materialize_isolated(
        later_generic_error,
        document={},
        pack=object(),
        model_id="mock-luna",
    ) is None
