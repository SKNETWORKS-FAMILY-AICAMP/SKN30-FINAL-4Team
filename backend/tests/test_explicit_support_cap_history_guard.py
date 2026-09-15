from __future__ import annotations

import pytest

from worker import vendor  # noqa: F401 - installs the vendored package

from semantic_structuring.explicit_support_cap_candidates import (
    explicit_support_cap_spans_in_text,
    extract_explicit_support_cap_candidates,
)
from semantic_structuring.models import CandidatePack
from semantic_structuring.source_selection import build_numeric_candidates


def _pack(text: str) -> CandidatePack:
    return CandidatePack.model_validate({
        "pack_id": "support-cap-history-pack",
        "notice_id": "PBLN-support-cap-history",
        "question": "support cap history guard",
        "blocks": [{"block_id": "body[0]", "text": text, "relation": "candidate"}],
    })


@pytest.mark.parametrize(
    "text",
    [
        "기업당 최대 5천만원을 지원받은 이력이 없는 기업",
        "과제당 최대 1억원 지원을 받은 실적 보유기관",
        "기업별 최대 5천만원을 지원받은 기업은 제외",
        "기업당 최대 5천만원 기지원 받은 기업 제외",
        "기업당 최대 5천만원 지원받지 않은 기업",
        "지원제외: 기업당 최대 5천만원 지원받은 기업",
        "기업당 최대 5천만원 지원 이력이 없는 기업",
        "기업당 최대 5천만원을 이미 지원받은 기업",
        "기업당 최대 5천만원을 과거 지원받은 기업",
        "기업당 최대 5천만원을 기존에 지원받은 기업",
        "기업당 최대 5천만원을 이전에 지원받은 기업",
        "기업당 최대 5천만원을 종전에 지원받은 기업",
        "기업당 최대 5천만원을 2024년에 지원받은 기업",
        "기업당 최대 5천만원을 지원받았던 기업",
        "기업당 최대 5천만원의 지원을 받은 기업은 제외",
        "기업당 최대 5천만원을 지급받은 기업은 제외",
        "기업당 최대 5천만원 지원 이력 기업 제외",
        "기업당 최대 5천만원 규모로 지원받은 이력이 있는 기업",
        "기업당 최대 5천만원을 최근 지원받은 기업",
        "기업당 최대 5천만원을 최근에 지원받은 기업",
        "과거 기업당 최대 5천만원을 지원받은 기업",
        "지난해 기업당 최대 5천만원을 지원받은 기업",
        "전년도 기업당 최대 5천만원을 지급받은 기업",
        "2024년에 기업당 최대 5천만원을 지원받은 기업",
        "과거 지원금은 기업당 최대 5천만원이었다",
        "지난해 기업당 최대 5천만원 지원",
        "전년도 지원실적: 기업당 최대 5천만원",
        "기지원금 최대 5천만원 수령 기업 제외",
        "지원 이력: 기업당 최대 5천만원",
        "기업당 최대 5천만원을 지원받은 경험이 있는 기업",
        "기업당 최대 5천만원을 지원받은 사실이 있는 기업",
        "기업당 최대 5천만원을 지원받은 바 있는 기업",
    ],
)
def test_historical_support_or_exclusion_is_not_a_current_support_cap(text: str) -> None:
    assert extract_explicit_support_cap_candidates(_pack(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "기업당 최대 5천만원 지원 예정",
        "기업당 최대 5천만원 지원 가능",
        "기업당 최대 5천만원 지급 예정",
        "기업당 최대 5천만원 지원받은 후 정산",
        "기업당 최대 5천만원을 지원받은 후 정산",
        "기업당 최대 5천만원 지원받을 수 있음",
        "기업당 최대 5천만원 규모로 지원받은 후 정산",
        "기업당 최대 5천만원 규모의 지원을 받은 후 정산",
        "기업당 최대 5천만원을 지급받은 후 정산",
        "과거 공고와 달리 기업당 최대 5천만원 지원 예정",
    ],
)
def test_future_support_plan_remains_a_current_support_cap(text: str) -> None:
    assert [
        item.anchor_text
        for item in extract_explicit_support_cap_candidates(_pack(text))
    ] == ["기업당 최대 5천만원"]


def test_current_cap_after_a_historical_comparison_is_the_only_candidate() -> None:
    text = "전년도 지원금 최대 5천만원, 금년도 지원금 최대 1억원"

    assert [
        item.anchor_text
        for item in extract_explicit_support_cap_candidates(_pack(text))
    ] == ["최대 1억원"]


@pytest.mark.parametrize(
    "text",
    [
        "지원금 최대 5억원달러",
        "지원금 최대 1억 5000만원 USD",
    ],
)
def test_foreign_currency_suffix_cannot_leave_a_partial_krw_prefix(text: str) -> None:
    assert explicit_support_cap_spans_in_text(text) == []
    assert extract_explicit_support_cap_candidates(_pack(text)) == []
    assert build_numeric_candidates(_pack(text)) == []
