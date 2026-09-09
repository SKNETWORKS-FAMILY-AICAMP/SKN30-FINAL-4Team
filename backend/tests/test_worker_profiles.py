"""Slice 1 프로파일 파이프라인 테스트. 네트워크를 타지 않는다.

LLM 호출은 저장된 selection 을 되돌려주는 가짜 selector 로 대체한다.
"""

import json
import logging
from pathlib import Path

import pytest

import httpx

from app.ports.llm_client import LLMInvalidResponseError, LLMTimeoutError
from worker.adapters.vllm_llm_client import VllmLLMClient
from pydantic import ValidationError

from worker.profiles import (
    _ensure_request_type_checked_glyph_compatibility,
    _request_type_option_blocks,
    _selection_validation_messages,
    build_pack,
    make_vllm_selector,
    parse_to_common_ir,
    resolve_request_type,
    structure_request_profile,
)
from worker.contracts.profile_snapshot import (
    PARTIAL_MATERIALIZATION,
    LLM_INVALID_RESPONSE,
    LLM_TIMEOUT,
    REQUEST_TYPE_CONTAINER_AMBIGUOUS,
    REQUEST_TYPE_CONTAINER_MISSING,
    REQUEST_TYPE_SELECTION_INVALID,
    REPAIR_BUDGET_EXHAUSTED,
)
from worker.vendor import REPO_ROOT

import semantic_structuring.request_profile_v012 as request_profile_contract
from semantic_structuring.request_profile_v012 import RequestSourceSelectionV012


_SAMPLE = REPO_ROOT / "samples" / "hwpx" / "mockup_01_우수사례_AI바이오실증.hwpx"
_EXAMPLES = REPO_ROOT / "packages" / "profile_structuring" / "examples" / "request"
_REAL_REQUEST_SAMPLES = {
    "mockup_01_우수사례_AI바이오실증.hwpx": ("detail_program_new", "☑"),
    "mockup_02_우수사례_뿌리산업스마트제조.hwpx": ("detail_program_new", "☑"),
    "mockup_03_보통사례_청년로컬크리에이터.hwpx": ("detail_program_new", "☑"),
    "mockup_04_보통사례_친환경그린에너지.hwpx": ("program_content_change", "☑"),
    "mockup_05_저급사례_AI바우처_모순충돌.hwpx": ("detail_program_new", "☑"),
    "mockup_06_저급사례_해외수출_목적내용불일치.hwpx": ("detail_program_new", "☑"),
    "mockup_07_우수사례_서식기준_딥테크스케일업.hwpx": ("detail_program_new", "■"),
    "mockup_08_CPL전항목_스마트기술사업화.hwpx": ("sub_program_new", "■"),
    "사전협의요청서_미흡사례.hwpx": ("detail_program_new", "■"),
    "사전협의요청서_우수사례.hwpx": ("detail_program_new", "■"),
}


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


def _chat_response(content: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                }
            }
        ]
    }


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


def test_all_real_hwpx_request_types_resolve_including_filled_square_forms(tmp_path):
    pytest.importorskip("rhwp", reason="rhwp-python 런타임이 없으면 파싱 단계를 건너뛴다")

    for name, (expected_code, expected_glyph) in _REAL_REQUEST_SAMPLES.items():
        path = REPO_ROOT / "samples" / "hwpx" / name
        if not path.is_file():
            pytest.fail(f"샘플 문서가 없다: {path}")
        artifact = parse_to_common_ir(
            input_path=path,
            notice_id=path.stem,
            source_kind="hwpx",
            run_dir=tmp_path / path.stem,
        )
        request_type = resolve_request_type(build_pack(artifact.document))
        assert request_type["selected_code"] == expected_code
        assert request_type["value_raw"]
        assert request_type["selection_source"]["glyph_raw"] == expected_glyph


def test_worker_compatibility_shim_repairs_an_older_vendor_glyph_set(monkeypatch):
    document, _ = _stored_selection()
    pack = build_pack(document)
    # Keep the original block coordinates while replacing only the checked
    # glyph, as an older vendored package would have received the same text.
    original = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    filled = original.model_copy(update={"text": original.text.replace("☑ 사업내용 변경", "■ 사업내용 변경")})
    filled_pack = pack.model_copy(update={
        "blocks": [filled if block.block_id == filled.block_id else block for block in pack.blocks],
    })
    old_checked = frozenset(request_profile_contract.CHECKED_GLYPHS) - {"■"}
    monkeypatch.setattr(request_profile_contract, "CHECKED_GLYPHS", old_checked)

    _ensure_request_type_checked_glyph_compatibility()

    assert "■" in request_profile_contract.CHECKED_GLYPHS
    request_type = resolve_request_type(filled_pack)
    assert request_type["selection_source"]["glyph_raw"] == "■"
    assert filled.text[
        request_type["selection_source"]["start_char"]:
        request_type["selection_source"]["end_char"]
    ] == "■"


def test_request_type_preflight_distinguishes_missing_and_ambiguous_without_llm_call():
    document, selection = _stored_selection()
    pack = build_pack(document)
    source_blocks = _request_type_option_blocks(pack)
    assert len(source_blocks) == 1
    missing_pack = pack.model_copy(update={
        "blocks": [block for block in pack.blocks if block.block_id != source_blocks[0].block_id],
    })
    duplicate = source_blocks[0].model_copy(update={"block_id": source_blocks[0].block_id + "-duplicate"})
    ambiguous_pack = pack.model_copy(update={"blocks": [*pack.blocks, duplicate]})
    calls: list[int] = []

    def selector(prior_selection, validation_errors):
        calls.append(1)
        return selection, {"repair": False}

    missing = structure_request_profile(
        document=document, pack=missing_pack, profile_id=selection.profile_id,
        selector=selector, model_id="offline-fake", max_repairs=0,
    )
    ambiguous = structure_request_profile(
        document=document, pack=ambiguous_pack, profile_id=selection.profile_id,
        selector=selector, model_id="offline-fake", max_repairs=0,
    )

    assert missing.status == "FAILED"
    assert missing.diagnostics[0].reason_code == REQUEST_TYPE_CONTAINER_MISSING
    assert ambiguous.status == "FAILED"
    assert ambiguous.diagnostics[0].reason_code == REQUEST_TYPE_CONTAINER_AMBIGUOUS
    assert calls == []


def test_request_type_preflight_reports_invalid_single_container_without_llm_call():
    document, selection = _stored_selection()
    pack = build_pack(document)
    source = next(block for block in pack.blocks if "☑ 사업내용 변경" in block.text)
    malformed = source.model_copy(update={"text": source.text.replace("☑ 사업내용 변경", "☑ 잘못된 라벨")})
    malformed_pack = pack.model_copy(update={
        "blocks": [malformed if block.block_id == source.block_id else block for block in pack.blocks],
    })
    calls: list[int] = []

    def selector(prior_selection, validation_errors):
        calls.append(1)
        return selection, {"repair": False}

    snapshot = structure_request_profile(
        document=document, pack=malformed_pack, profile_id=selection.profile_id,
        selector=selector, model_id="offline-fake", max_repairs=0,
    )

    assert snapshot.status == "FAILED"
    assert snapshot.diagnostics[0].reason_code == REQUEST_TYPE_SELECTION_INVALID
    assert calls == []


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


def test_vllm_selector_normalizes_redundant_locator_without_a_second_call():
    document, selection = _stored_selection()
    pack = build_pack(document)
    invalid = _stored_selection_payload()
    invalid["facts"][8]["value_anchor"]["anchor_text"] = "redundant legacy locator"
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(json.dumps(invalid)))

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
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
        model_id="served-gemma",
        max_repairs=1,
    )

    assert snapshot.status == "OK"
    assert len(sent) == 1
    assert "prior_selection" not in json.loads(sent[0]["messages"][1]["content"])


def test_vllm_selector_repairs_missing_legacy_anchor_once():
    document, selection = _stored_selection()
    pack = build_pack(document)
    invalid = _stored_selection_payload()
    invalid["facts"][0]["value_anchor"]["anchor_text"] = None
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps(invalid) if len(sent) == 1 else selection.model_dump_json()
        return httpx.Response(200, json=_chat_response(content))

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
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
        model_id="served-gemma",
        max_repairs=1,
    )

    assert snapshot.status == "OK"
    assert len(sent) == 2
    repair_payload = json.loads(sent[1]["messages"][1]["content"])
    assert repair_payload["prior_selection"]["facts"][0]["value_anchor"]["anchor_text"] is None
    assert repair_payload["server_validation_errors"]
    assert "raw" not in json.dumps(repair_payload["server_validation_errors"])


def test_vllm_selector_second_schema_invalid_response_fails_without_a_third_call():
    document, selection = _stored_selection()
    pack = build_pack(document)
    invalid = _stored_selection_payload()
    invalid["facts"][0]["value_anchor"]["anchor_text"] = None
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(json.dumps(invalid)))

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
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
        model_id="served-gemma",
        max_repairs=1,
    )

    assert snapshot.status == "FAILED"
    assert len(sent) == 2
    assert all("raw" not in diagnostic.message for diagnostic in snapshot.diagnostics)
    assert all("anchor_text" not in diagnostic.message for diagnostic in snapshot.diagnostics)


def test_vllm_selector_json_failure_does_not_trigger_schema_repair():
    document, selection = _stored_selection()
    pack = build_pack(document)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="not-json")

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
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
        model_id="served-gemma",
        max_repairs=1,
    )

    assert snapshot.status == "FAILED"
    assert calls == 1


def test_vllm_selector_timeout_does_not_trigger_schema_repair():
    document, selection = _stored_selection()
    pack = build_pack(document)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
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
        model_id="served-gemma",
        max_repairs=1,
    )

    assert snapshot.status == "FAILED"
    assert calls == 1


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

def test_repair_hint_keeps_the_allowed_values_the_model_must_choose_from():
    """보완 힌트가 허용 목록을 온전히 담아야 한다.

    이전 구현은 raw 의 모든 문자열을 메시지에서 치환했다. facts 에 ``target``
    이 있으면 허용 목록의 ``support_target`` 이 ``support_[redacted]`` 가 되어,
    "이 중에서 고르라" 는 안내가 고를 수 없는 목록과 함께 나갔다. 실제 RunPod
    실행에서 보완 시도가 같은 진단을 반복한 원인이다.
    """
    invalid = _stored_selection_payload()
    # 허용 목록의 다른 값(support_target)의 부분 문자열인 값을 일부러 넣는다.
    invalid["facts"][0]["field_name"] = "target"

    with pytest.raises(ValidationError) as raised:
        RequestSourceSelectionV012.model_validate(invalid)
    messages = _selection_validation_messages(raised.value)

    joined = " ".join(messages)
    assert "redacted" not in joined
    assert "support_target" in joined
    assert "facts.0.field_name" in joined


def test_repair_hint_reaches_the_model_and_nothing_else(caplog):
    """힌트는 다음 호출 payload 로만 간다. 로그·예외 문구로 새지 않는다."""
    document, selection = _stored_selection()
    pack = build_pack(document)
    invalid = _stored_selection_payload()
    invalid["facts"][0]["value_anchor"]["anchor_text"] = None
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps(invalid) if len(sent) == 1 else selection.model_dump_json()
        return httpx.Response(200, json=_chat_response(content))

    selector = make_vllm_selector(
        VllmLLMClient(
            api_key="test-key",
            base_url="https://vllm.invalid/v1",
            model_profiles={"structuring": "served-gemma"},
            timeout_seconds=5,
            transport=httpx.MockTransport(handler),
        ),
        model_profile="structuring",
        pack=pack,
        document=document,
        profile_id=selection.profile_id,
    )

    with caplog.at_level(logging.DEBUG):
        snapshot = structure_request_profile(
            document=document,
            pack=pack,
            profile_id=selection.profile_id,
            selector=selector,
            model_id="served-gemma",
            max_repairs=1,
        )

    assert snapshot.status == "OK"
    hints = json.loads(sent[1]["messages"][1]["content"])["server_validation_errors"]
    assert hints
    # 힌트 문구가 로그로 새지 않는다.
    logged = " ".join(record.getMessage() for record in caplog.records)
    for hint in hints:
        assert hint not in logged


def _selector_returning(payload: dict):
    """LLM 없이 selector 를 흉내낸다. 재료화는 순수 로컬 함수다."""

    def selector(prior_selection, validation_errors):
        return RequestSourceSelectionV012.model_validate(payload), {"repair": False}

    return selector


def test_a_broken_delivery_relation_no_longer_destroys_the_whole_profile():
    """어긋난 묶음만 덜어내고 나머지를 살린다 (초안: 정상 결과 보존).

    지금까지는 delivery relation 하나가 컨테이너 규칙을 어기면 facts 26 개와
    support_components 까지 통째로 사라지고 FAILED 였다. 실제 요청서가 결과를
    하나도 못 내던 이유다.
    """
    document, _ = _stored_selection()
    pack = build_pack(document)
    payload = _stored_selection_payload()
    assert len(payload["delivery_relations"]) == 1
    assert len(payload["facts"]) == 26
    # actor 를 컨테이너와 다른 블록으로 옮긴다 — 관찰된 실패와 같은 모양이다.
    other = next(
        block.block_id
        for block in pack.blocks
        if block.block_id != payload["delivery_relations"][0]["actor_anchor"]["source_block_id"]
    )
    payload["delivery_relations"][0]["actor_anchor"]["source_block_id"] = other

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=payload["profile_id"],
        selector=_selector_returning(payload),
        model_id="served-gemma",
        max_repairs=0,
    )

    assert snapshot.status == "OK"
    assert snapshot.profile is not None
    # 본체는 살아남는다.
    assert snapshot.profile["support_components"]
    # 덜어낸 사실이 진단에 남는다. 남지 않으면 "조용히 버리기" 다.
    codes = [diagnostic.reason_code for diagnostic in snapshot.diagnostics]
    assert PARTIAL_MATERIALIZATION in codes
    partial = snapshot.diagnostics[-1]
    assert partial.unit == "delivery_relations"
    assert "delivery_relations=1" in partial.message


def test_a_broken_fact_still_fails_the_whole_profile():
    """본체는 덜어내지 않는다. 사실이 없는 프로필은 부분 결과가 아니다."""
    document, _ = _stored_selection()
    pack = build_pack(document)
    payload = _stored_selection_payload()
    payload["facts"][0]["value_anchor"]["source_block_id"] = "markdown:no-such-block"

    snapshot = structure_request_profile(
        document=document,
        pack=pack,
        profile_id=payload["profile_id"],
        selector=_selector_returning(payload),
        model_id="served-gemma",
        max_repairs=0,
    )

    assert snapshot.status == "FAILED"
    assert snapshot.profile is None
    assert PARTIAL_MATERIALIZATION not in [
        diagnostic.reason_code for diagnostic in snapshot.diagnostics
    ]
