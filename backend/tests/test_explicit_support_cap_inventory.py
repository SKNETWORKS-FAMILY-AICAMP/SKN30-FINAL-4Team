from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs the vendored package

from semantic_structuring.explicit_support_cap_candidates import (
    explicit_support_cap_spans_in_text,
    extract_explicit_support_cap_candidates,
)
from semantic_structuring.models import CandidatePack


def _pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "support-cap-inventory-pack",
        "notice_id": "PBLN-support-cap-inventory",
        "question": "support cap inventory",
        "blocks": [{"block_id": "body[0]", "text": text, "relation": "candidate"}],
    })


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("기업당 지원금 5,000만원 이내", ["기업당 지원금 5,000만원 이내"]),
        ("정부지원금은 총사업비의 70% 이내", ["70% 이내"]),
        ("기업당 5천만원 한도", ["기업당 5천만원 한도"]),
    ],
)
def test_suffix_only_support_caps_are_inventoryed_once(
    text: str,
    expected: list[str],
) -> None:
    candidates = extract_explicit_support_cap_candidates(_pack(text))

    assert [candidate.anchor_text for candidate in candidates] == expected
    assert [(start, end) for start, end in explicit_support_cap_spans_in_text(text)] == [
        (text.index(anchor), text.index(anchor) + len(anchor)) for anchor in expected
    ]


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 5,000만원 이내",
        "기업당 최대 5천만원 한도",
    ],
)
def test_prefix_and_suffix_limit_markers_do_not_duplicate_one_cap(text: str) -> None:
    assert len(extract_explicit_support_cap_candidates(_pack(text))) == 1


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 5천명",
        "지원금 최대 1만명",
        "지원금 최대 2천개사",
    ],
)
def test_recipient_counts_cannot_be_scanned_as_krw_caps(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("선정규모 20개사 내외", ["20개사 내외"]),
        ("선발 규모: 최대 8개팀 내외", ["8개팀 내외"]),
        ("선정규모 최대 5천명 내외", ["5천명 내외"]),
        ("선정규모 최대 1만2천5백명 내외", ["1만2천5백명 내외"]),
    ],
)
def test_named_selection_capacity_is_inventoryed_as_support_scale(
    text: str,
    expected: list[str],
) -> None:
    assert [
        candidate.anchor_text
        for candidate in extract_explicit_support_cap_candidates(_pack(text))
    ] == expected


@pytest.mark.parametrize(
    "text",
    [
        "신청 규모 20개사 내외",
        "접수 인원 20명 내외",
        "최대 20개사 신청",
        "모집 인원 20명 내외",
    ],
)
def test_application_or_reception_counts_are_not_inventoryed_as_selection_capacity(
    text: str,
) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


def test_historical_heading_excludes_its_body_and_next_heading_resets_scope() -> None:
    pack = CandidatePack.model_validate({
        "pack_id": "support-cap-history-heading-pack",
        "notice_id": "PBLN-support-cap-history-heading",
        "question": "heading-owned historical support scope",
        "blocks": [
            {
                "block_id": "heading-history",
                "text": "과거 지원 이력",
                "relation": "candidate",
                "block_kind": "heading",
                "source_order": 0,
            },
            {
                "block_id": "body-history",
                "text": "기업당 최대 5천만원",
                "relation": "candidate",
                "source_order": 1,
            },
            {
                "block_id": "heading-current",
                "text": "금년도 지원 내용",
                "relation": "candidate",
                "block_kind": "heading",
                "source_order": 2,
            },
            {
                "block_id": "body-current",
                "text": "기업당 최대 1억원",
                "relation": "candidate",
                "source_order": 3,
            },
        ],
    })

    assert [
        (candidate.source_block_id, candidate.anchor_text)
        for candidate in extract_explicit_support_cap_candidates(pack)
    ] == [("body-current", "기업당 최대 1억원")]


@pytest.mark.parametrize(
    "text",
    [
        "과거 지원 이력: 기업당 최대 5천만원",
        "지원 이력 기업당 최대 5천만원",
        "전년도 지원실적 기업당 최대 5천만원",
    ],
)
def test_same_line_historical_prefix_is_not_a_current_cap(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []
