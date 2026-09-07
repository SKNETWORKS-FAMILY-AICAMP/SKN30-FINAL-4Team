"""Slice 1 프로파일 파이프라인 테스트. 네트워크를 타지 않는다.

LLM 호출은 저장된 selection 을 되돌려주는 가짜 selector 로 대체한다.
"""

import json
from pathlib import Path

import pytest

import httpx

from app.ports.llm_client import LLMInvalidResponseError, LLMTimeoutError
from worker.adapters.vllm_llm_client import VllmLLMClient
from worker.profiles import (
    build_pack,
    make_vllm_selector,
    parse_to_common_ir,
    resolve_request_type,
    structure_request_profile,
)
from worker.contracts.profile_snapshot import (
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    REPAIR_BUDGET_EXHAUSTED,
)
from worker.vendor import REPO_ROOT

from semantic_structuring.request_profile_v012 import RequestSourceSelectionV012


_SAMPLE = REPO_ROOT / "samples" / "hwpx" / "mockup_01_우수사례_AI바이오실증.hwpx"
_EXAMPLES = REPO_ROOT / "packages" / "profile_structuring" / "examples" / "request"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _stored_selection_payload() -> dict:
    """저장된 selection 아티팩트를 러너의 ``_load_selection`` 과 같게 벗겨 읽는다."""

    raw = _load_json(_EXAMPLES / "source_selection_v012.json")
    raw = raw.get("selection", raw)
    for key in ("artifact_kind", "selection_contract", "remote_model_called"):
        raw.pop(key, None)
    return raw


def _stored_selection() -> tuple[dict, RequestSourceSelectionV012]:
    document = _load_json(_EXAMPLES / "common_ir_v1.json")
    return document, RequestSourceSelectionV012.model_validate(
        _stored_selection_payload()
    )


def test_real_hwpx_parses_and_request_type_comes_from_the_checkbox(tmp_path):
    pytest.importorskip("rhwp", reason="rhwp-python 런타임이 없으면 파싱 단계를 건너뛴다")
    if not _SAMPLE.is_file():
        pytest.skip(f"샘플 문서가 없다: {_SAMPLE}")

    artifact = parse_to_common_ir(
        input_path=_SAMPLE,
        notice_id="smoke01",
        source_kind="hwpx",
        run_dir=tmp_path / "run",
    )

    assert artifact.validation_errors == []
    assert artifact.common_ir_document_id == "hwpx:smoke01"
    assert artifact.source_sha256
    assert artifact.manifest["status"] == "ok"
    kinds = [block["kind"] for block in artifact.document["blocks"]]
    assert kinds == ["paragraph", "paragraph", "paragraph", "table"]
    table = artifact.document["blocks"][3]
    assert len(table["cells"]) == 16

    pack = build_pack(artifact.document)
    assert pack.pack_id == "hwpx:smoke01-request-v0.1.2"
    assert pack.blocks

    # 요청 유형은 원본 체크박스 글리프로 서버가 정한다. LLM 판정이 아니다.
    request_type = resolve_request_type(pack)
    assert request_type["selected_code"] == "detail_program_new"
    assert request_type["selection_source"]["glyph_raw"] == "☑"


def test_stored_selection_replays_into_an_ok_snapshot():
    document, selection = _stored_selection()
    pack = build_pack(document)
    calls: list[int] = []

    def selector(prior_selection, validation_errors):
        calls.append(1)
        return selection, {"repair": prior_selection is not None}

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=selection.profile_id,
        selector=selector,
        model_id="offline-fake",
        max_repairs=1,
    )

    assert snapshot.status == "OK"
    assert len(calls) == 1
    assert snapshot.profile is not None
    assert snapshot.profile["profile_id"] == selection.profile_id
    assert snapshot.candidate_pack_id == pack.pack_id
    assert snapshot.prompt_version == "request_source_selection_v0.1.2"
    assert snapshot.selection_attempts == 1
    assert all(
        diagnostic.reason_code is not None for diagnostic in snapshot.diagnostics
    )


def test_llm_timeout_is_isolated_into_a_failed_snapshot():
    document, selection = _stored_selection()
    pack = build_pack(document)

    def selector(prior_selection, validation_errors):
        raise LLMTimeoutError("LLM request timed out")

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=selection.profile_id,
        selector=selector,
        model_id="offline-fake",
        max_repairs=1,
    )

    assert snapshot.status == "FAILED"
    assert snapshot.profile is None
    assert snapshot.diagnostics
    assert snapshot.diagnostics[0].reason_code == LLM_TIMEOUT


def test_repair_budget_terminates_after_max_repairs_plus_one():
    document, selection = _stored_selection()
    pack = build_pack(document)
    # 원본에 없는 문자열로 앵커를 깨뜨려 서버 exact-span 재료화를 실패시킨다.
    payload = _stored_selection_payload()
    anchor = payload["facts"][0]["value_anchor"]
    anchor["anchor_text"] = "이 문장은 원본에 존재하지 않는다"
    anchor["value_span_candidate_id"] = None
    broken = RequestSourceSelectionV012.model_validate(payload)
    calls: list[tuple[bool, list[str] | None]] = []

    def selector(prior_selection, validation_errors):
        calls.append((prior_selection is not None, validation_errors))
        return broken, {"repair": prior_selection is not None}

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=selection.profile_id,
        selector=selector,
        model_id="offline-fake",
        max_repairs=1,
    )

    assert len(calls) == 2  # max_repairs + 1
    assert calls[0][0] is False and calls[1][0] is True
    assert calls[1][1]  # 두 번째 호출은 서버 검증 오류를 받아야 한다
    assert snapshot.status == "FAILED"
    assert snapshot.profile is None
    assert snapshot.diagnostics[-1].reason_code == REPAIR_BUDGET_EXHAUSTED
    assert snapshot.diagnostics[-1].terminated_because == REPAIR_BUDGET_EXHAUSTED


def test_vllm_selector_adapts_the_port_and_verifies_the_pack_ids():
    """selector 어댑터가 패키지 프롬프트를 그대로 싣고 id 를 검증하는지 본다."""

    document, selection = _stored_selection()
    pack = build_pack(document)
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": selection.model_dump_json(),
                        }
                    }
                ]
            },
        )

    client = VllmLLMClient(
        api_key="test-key",
        base_url="https://vllm.invalid/v1",
        model_profiles={"structuring": "served-gemma"},
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    selector = make_vllm_selector(
        client,
        model_profile="structuring",
        pack=pack,
        document=document,
        profile_id=selection.profile_id,
    )

    first, usage = selector(None, None)
    assert first.candidate_pack_id == pack.pack_id
    assert usage == {"repair": False}
    first_payload = json.loads(sent[-1]["messages"][1]["content"])
    assert "prior_selection" not in first_payload
    assert first_payload["read_only_context"]["request_type"]["selected_code"]

    selector(first, ["anchor_text not found in source block"])
    repair_body = sent[-1]
    assert "prior parsed selection failed server exact-span" in (
        repair_body["messages"][0]["content"]
    )
    repair_payload = json.loads(repair_body["messages"][1]["content"])
    assert repair_payload["prior_selection"]["candidate_pack_id"] == pack.pack_id
    assert repair_payload["server_validation_errors"] == [
        "anchor_text not found in source block"
    ]


def test_pack_id_mismatch_is_isolated_into_a_failed_snapshot():
    """다른 pack 을 가리키는 응답이 raw 예외로 새지 않고 FAILED 로 격리되는지 본다.

    패키지 오케스트레이터는 selector 예외 중 RequestSourceSelectionParseError 만
    잡는다. id 불일치를 ValueError 로 던지면 아무도 잡지 않아 스냅샷 계약이 깨진다.
    """

    document, selection = _stored_selection()
    pack = build_pack(document)
    foreign = selection.model_copy(update={"candidate_pack_id": "hwpx:other-pack"})

    class _ForeignPackClient:
        async def generate_structured(self, **_: object):
            return foreign

    selector = make_vllm_selector(
        _ForeignPackClient(),
        model_profile="structuring",
        pack=pack,
        document=document,
        profile_id=selection.profile_id,
    )

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=selection.profile_id,
        selector=selector,
        model_id="offline-fake",
        max_repairs=1,
    )

    assert snapshot.status == "FAILED"
    assert snapshot.profile is None
    assert snapshot.diagnostics[-1].reason_code == LLM_INVALID_RESPONSE
