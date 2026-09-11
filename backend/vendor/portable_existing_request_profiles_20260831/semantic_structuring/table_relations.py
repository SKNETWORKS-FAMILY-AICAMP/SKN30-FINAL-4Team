"""표 안에서 세로로 짝지어지는 셀 쌍을 찾는다.

HWP 조직도는 칸을 배치해 그린 그림이라, 기관과 그 역할·행위가 한 행이 아니라
**같은 열의 다음 의미 행**에 온다. 이 모듈은 그 기하만 계산한다 — 어느 쪽이
기관이고 어느 쪽이 역할인지는 판단하지 않는다.

좌표는 Common IR 메타데이터에서만 읽는다(``cell_id`` · ``row_index`` ·
``col_index`` · ``col_span`` · ``text_occurrence_ids``). 블록 id 문자열을 파싱해
행·열을 얻지 않는다. 표기가 바뀌면 조용히 어긋나기 때문이다.

규칙은 ``request_profile_v012`` 의 ``table_column_pair`` 검증과 같다. 저쪽은
모델이 고른 관계를 fail-closed 로 다시 확인하는 자리이고 이쪽은 고를 거리를
만드는 자리다. 둘은 목적이 달라 함께 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# 한 쌍의 양쪽이 모두 여러 문단을 담고 있으면 어느 문단이 어느 문단과 짝인지
# 원문만으로 정할 수 없다. 곱집합을 만들지 않고 제외하되, 왜 빠졌는지는 남긴다.
DELIVERY_PAIR_AMBIGUOUS = "DELIVERY_PAIR_AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class TableCellRef:
    """표 한 칸의 좌표와 그 칸이 담은 비어 있지 않은 occurrence."""

    common_ir_cell_id: str
    row_index: int
    col_index: int
    col_span: int
    occurrence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableColumnPair:
    """같은 열에서 의미상 맞붙은 두 칸."""

    common_ir_block_id: str
    actor: TableCellRef
    member: TableCellRef


@dataclass(frozen=True, slots=True)
class TableColumnPairSkip:
    """짝이 될 수 있었지만 제외된 두 칸과 그 사유."""

    common_ir_block_id: str
    actor: TableCellRef
    member: TableCellRef
    reason_code: str


def _occurrence_text(document: dict[str, Any]) -> dict[str, str]:
    return {
        occurrence["occurrence_id"]: occurrence.get("text", "")
        for block in document.get("blocks") or []
        for occurrence in block.get("occurrences") or []
    }


def _filled(cell: dict[str, Any], texts: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        item
        for item in cell.get("text_occurrence_ids") or []
        if texts.get(item, "").strip()
    )


def find_table_column_pairs(
    document: dict[str, Any], table_id: str
) -> tuple[list[TableColumnPair], list[TableColumnPairSkip]]:
    """``table_id`` 안에서 세로로 맞붙은 칸 쌍과 제외된 쌍을 돌려준다.

    명시적 구조를 가진 표만 본다. 추론한 격자에서 관계를 만들지 않는다.
    """

    table = next(
        (
            block
            for block in document.get("blocks") or []
            if block.get("block_id") == table_id
        ),
        None,
    )
    if (
        table is None
        or table.get("kind") != "table"
        or table.get("structure_status") != "explicit"
    ):
        return [], []

    texts = _occurrence_text(document)
    by_position: dict[tuple[int, int, int], TableCellRef] = {}
    for cell in table.get("cells") or []:
        occurrences = _filled(cell, texts)
        if not occurrences:
            continue
        ref = TableCellRef(
            common_ir_cell_id=str(cell.get("cell_id") or ""),
            row_index=int(cell.get("row_index") or 0),
            col_index=int(cell.get("col_index") or 0),
            col_span=int(cell.get("col_span") or 1),
            occurrence_ids=occurrences,
        )
        by_position[(ref.row_index, ref.col_index, ref.col_span)] = ref

    # 비어 있지 않은 의미 행은 표 전체에서 센다. 열마다 따로 세면 멀리 떨어진
    # 두 칸이 "다음 행" 으로 붙어 관계가 아닌 것을 관계로 만든다.
    semantic_rows = sorted({ref.row_index for ref in by_position.values()})

    pairs: list[TableColumnPair] = []
    skipped: list[TableColumnPairSkip] = []
    for (row, col, span), actor in sorted(by_position.items()):
        following = [item for item in semantic_rows if item > row]
        if not following:
            continue
        member = by_position.get((following[0], col, span))
        if member is None:
            continue
        if len(actor.occurrence_ids) > 1 and len(member.occurrence_ids) > 1:
            skipped.append(
                TableColumnPairSkip(
                    common_ir_block_id=table_id,
                    actor=actor,
                    member=member,
                    reason_code=DELIVERY_PAIR_AMBIGUOUS,
                )
            )
            continue
        pairs.append(
            TableColumnPair(
                common_ir_block_id=table_id, actor=actor, member=member
            )
        )
    return pairs, skipped


__all__ = [
    "DELIVERY_PAIR_AMBIGUOUS",
    "TableCellRef",
    "TableColumnPair",
    "TableColumnPairSkip",
    "find_table_column_pairs",
]
