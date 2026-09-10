from __future__ import annotations

from copy import deepcopy

from app.retrieval.embedding_inputs import (
    EMBEDDING_ASSEMBLY_VERSION as EXISTING_ASSEMBLY_VERSION,
    assemble_embedding_inputs,
)
from worker.analysis_job import EMBEDDING_ASSEMBLY_VERSION as REQUEST_ASSEMBLY_VERSION
from worker.retrieval_inputs import assemble_inputs


MODEL = "text-embedding-3-small"


def _fact(
    fact_id: str,
    field_name: str,
    value_raw: str,
    *,
    block: str,
    start_char: int = 0,
) -> dict:
    return {
        "fact_id": fact_id,
        "field_name": field_name,
        "status": "identified",
        "value_raw": value_raw,
        "value_source": {
            "source_block_id": block,
            "start_char": start_char,
        },
    }


def _component(name_raw: str, *, block: str, start_char: int) -> dict:
    return {
        "support_component_id": f"component:{block}:{start_char}",
        "component_kind": "support_package",
        "name_raw": name_raw,
        "value_source": {
            "source_block_id": block,
            "start_char": start_char,
        },
    }


def _base_profile() -> dict:
    return {
        "comparison_profile": {
            "purpose_goal": [
                _fact("purpose:1", "purpose_goal", "고성장 기업으로 도약", block="hwp:b13")
            ],
            "support_target": [
                _fact("target:1", "support_target", "ICT분야 중소기업", block="hwp:b44")
            ],
        },
        "support_components": [],
    }


def test_support_component_names_are_deduplicated_and_source_ordered() -> None:
    profile = _base_profile()
    profile["comparison_profile"].update({
        "support_activities": [
            _fact(
                "activity:1",
                "support_activities",
                "시장수요최적화기술개발",
                block="hwp:b54",
                start_char=9,
            )
        ],
    })
    profile["support_components"] = [
        _component("고성장기업도약기술개발", block="hwp:b90", start_char=0),
        _component("후속지원", block="hwp:b70", start_char=0),
        _component("시장수요최적화기술개발", block="hwp:b54", start_char=9),
        _component("고성장기업도약기술개발", block="hwp:b54", start_char=29),
    ]

    expected = """[support_components]
시장수요최적화기술개발
고성장기업도약기술개발
후속지원"""
    original = assemble_inputs(profile, model=MODEL, max_input_tokens=8192)["support"].text

    reversed_profile = deepcopy(profile)
    reversed_profile["support_components"].reverse()
    reordered = assemble_inputs(
        reversed_profile, model=MODEL, max_input_tokens=8192
    )["support"].text

    assert original == expected
    assert reordered == expected
    assert original.count("시장수요최적화기술개발") == 1
    assert original.count("고성장기업도약기술개발") == 1


def test_ict_request_profile_support_axis_keeps_packages_and_content() -> None:
    profile = _base_profile()
    profile["comparison_profile"].update({
        "support_period": [
            _fact(
                "period:1",
                "support_period",
                "최대 3년(2년+1년)",
                block="hwp:b48",
                start_char=9,
            )
        ],
        "support_activities": [
            _fact(
                "activity:1",
                "support_activities",
                "先시장・수요검증 + 後기술개발 추진",
                block="hwp:b56",
                start_char=56,
            )
        ],
        "support_items": [
            _fact(
                "item:1",
                "support_items",
                "시장검증보고서",
                block="hwp:t91#r8c6p0",
                start_char=6,
            )
        ],
        "support_content": [
            _fact(
                "content:1",
                "support_content",
                "3차년 선별지원",
                block="hwp:b50",
                start_char=34,
            )
        ],
    })
    profile["support_components"] = [
        _component("시장수요최적화기술개발", block="hwp:b54", start_char=9),
        _component("고성장기업도약기술개발", block="hwp:b54", start_char=29),
    ]

    support = assemble_inputs(
        profile, model=MODEL, max_input_tokens=8192
    )["support"].text

    assert support == """[support_components]
시장수요최적화기술개발
고성장기업도약기술개발

[support_activities]
先시장・수요검증 + 後기술개발 추진

[support_items]
시장검증보고서

[support_content]
3차년 선별지원"""
    assert "support_period" not in support
    assert "최대 3년(2년+1년)" not in support


def test_existing_and_request_component_shapes_render_the_same_support_axis() -> None:
    request_profile = _base_profile()
    request_profile["comparison_profile"]["support_content"] = [
        _fact("content:1", "support_content", "후속 사업화 지원", block="hwp:b20")
    ]
    request_profile["support_components"] = [
        _component("시장수요최적화기술개발", block="hwp:b9", start_char=0),
        _component("고성장기업도약기술개발", block="hwp:b10", start_char=0),
    ]

    existing_profile = deepcopy(request_profile)
    existing_profile["support_components"] = [
        {
            "support_component_id": "existing:market",
            "component_kind": "support_package",
            "name_raw": "시장수요최적화기술개발",
            "name_status": "identified",
            "name_source_block_id": "hwp:b9",
            "facts": [],
        },
        {
            "support_component_id": "existing:growth",
            "component_kind": "support_package",
            "name_raw": "고성장기업도약기술개발",
            "name_status": "identified",
            "name_source_block_id": "hwp:b10",
            "facts": [],
        },
    ]

    request_support = assemble_inputs(
        request_profile, model=MODEL, max_input_tokens=8192
    )["support"].text
    existing_support = assemble_embedding_inputs(
        existing_profile, model=MODEL, max_input_tokens=8192
    )["support"].text

    assert existing_support == request_support


def test_existing_and_request_use_the_same_component_assembly_version() -> None:
    assert EXISTING_ASSEMBLY_VERSION == REQUEST_ASSEMBLY_VERSION
    assert EXISTING_ASSEMBLY_VERSION == "approved-facts-components-role-aware-v2"


def test_support_deduplication_preserves_distinct_semantic_roles() -> None:
    profile = _base_profile()
    profile["comparison_profile"]["support_content"] = [
        {
            **_fact(
                "content:operator",
                "support_content",
                "정기 지원",
                block="hwp:b20",
            ),
            "semantic_role": "운영기관",
        },
        {
            **_fact(
                "content:recipient",
                "support_content",
                "정기 지원",
                block="hwp:b21",
            ),
            "semantic_role": "수행기관",
        },
    ]
    profile["support_components"] = [
        _component("정기 지원", block="hwp:b19", start_char=0),
    ]

    support = assemble_inputs(
        profile, model=MODEL, max_input_tokens=8192
    )["support"].text

    assert "[support_components]\n정기 지원" in support
    assert "운영기관: 정기 지원" in support
    assert "수행기관: 정기 지원" in support
