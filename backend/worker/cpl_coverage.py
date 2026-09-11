"""구조화가 라벨 구역을 비우고 지나갔는지 본다.

실문서에서 관측한 형태: 원문에 ``○ (사업목적) …`` 구역이 있고 다른 필드 27 개가
정상인데 ``purpose_goal`` 만 ``not_found`` 로 나왔다. 문서에 없어서 빈 것과
구조화가 놓쳐서 빈 것은 사용자가 해야 할 다음 행동이 다른데, 지금은 둘 다
``not_found`` 한 값으로 보인다.

여기서 하는 일은 **후보 표시까지**다. 라벨이 있는데 값이 비었다는 사실은
결정적으로 셀 수 있지만, 그것이 기술적 추출 실패인지 값을 특정할 수 없는
서술인지(``추후 결정`` 같은) 명시적 부재인지는 이 정보만으로 갈리지 않는다.
재검이나 상태 확정은 이 신호를 받은 쪽이 정한다.

라벨 문법은 닫혀 있다. 서식이 정해 둔 표기라 표현이 열려 있지 않고, 실문서
10 건에서 세 가지 표기가 관측됐다.

    ○ 사업목적 :        ○ (사업목적)        ○ 사업 목적 :

의미 키워드 목록을 늘리는 것이 아니다. 새 필드를 다루려면 그 필드의 라벨
문법을 확인하고 여기에 더한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from .analysis_inputs import field_states_by_name, read_path


# 글머리 기호·괄호·콜론은 서식마다 다르고 라벨 안 공백도 문서마다 다르다.
# 라벨 단어 자체는 바꾸지 않는다.
_LABEL_GRAMMAR = r"(?:^|\n)\s*[○□■●▪·\-]?\s*\(?\s*{label}\s*\)?\s*[:：)]?"

# 필드 하나에 라벨 하나. 별칭이 필요하면 그 필드의 표기를 실제로 확인한 뒤 더한다.
_FIELD_LABELS: dict[str, str] = {
    "comparison_profile.purpose_goal": r"사업\s*목적",
}

_PATTERNS = {
    path: re.compile(_LABEL_GRAMMAR.format(label=label))
    for path, label in _FIELD_LABELS.items()
}


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """라벨 구역은 있는데 값이 비어 있는 필드 하나."""

    profile_field: str
    field_name: str
    status: str | None
    label_block_ids: tuple[str, ...]


def _block_texts(common_ir: Mapping[str, Any]) -> list[tuple[str, str]]:
    """블록 id 와 본문. 표 셀 occurrence 도 각각 본다."""

    rows: list[tuple[str, str]] = []
    for block in common_ir.get("blocks") or []:
        if not isinstance(block, Mapping):
            continue
        block_id = str(block.get("block_id") or "")
        for occurrence in block.get("occurrences") or []:
            if isinstance(occurrence, Mapping) and occurrence.get("text"):
                rows.append((block_id, str(occurrence["text"])))
    return rows


def detect_coverage_gaps(
    profile: Mapping[str, Any], common_ir: Mapping[str, Any]
) -> list[CoverageGap]:
    """라벨 구역이 있는데 값이 비어 있는 필드를 모은다. 상태를 바꾸지 않는다."""

    states = field_states_by_name(profile)
    texts = _block_texts(common_ir)
    gaps: list[CoverageGap] = []
    for path, pattern in _PATTERNS.items():
        name = path.rsplit(".", 1)[-1]
        rows = read_path(profile, path)
        if rows:
            continue
        state = (states.get(name) or {}).get("status")
        # 명시적 부재는 후보가 아니다. 문서가 "해당 없음" 이라고 말한 것을
        # 추출 누락으로 되돌리지 않는다.
        if state == "not_applicable":
            continue
        found = tuple(
            block_id for block_id, text in texts if pattern.search(text)
        )
        if found:
            gaps.append(
                CoverageGap(
                    profile_field=path,
                    field_name=name,
                    status=state,
                    label_block_ids=found,
                )
            )
    return gaps


__all__ = ["CoverageGap", "detect_coverage_gaps"]
