"""수행관계 재검에 넣을 후보 쌍을 서버가 결정적으로 만든다.

LLM 에게 179 개 블록을 평평하게 다시 주고 관계를 지어 보라고 하지 않는다.
어떤 칸과 어떤 칸이 짝이 될 수 있는지는 표 기하로 정해지므로 서버가 만든다.
모델이 할 일은 그중 무엇을 채택할지, 위가 기관인지, 아래가 역할인지 행위인지
**고르는 것**뿐이다. 기관명이나 역할 문구를 새로 쓰게 하지 않는다.

여기서는 후보만 만든다. LLM 호출도, Fact 승격도, API 직렬화도 하지 않는다.

문단이 아닌 표 경로만 다룬다. 한 문단에 기관과 행위가 함께 적힌 서식
(``부산테크노파크가 사업을 총괄하고 접수·평가를 수행한다``)은 기존 paragraph
관계 경로가 그대로 맡는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from semantic_structuring.table_relations import (
    DELIVERY_PAIR_AMBIGUOUS,
    TableCellRef,
    find_table_column_pairs,
)

from .cpl_coverage import build_fragments, evidence_ref

DELIVERY_FIELD = "comparison_profile.delivery_relations"
# 후보 id 는 좌표에서 나온다. 알고리즘이 바뀌면 같은 문서가 다른 id 를 내므로
# 버전을 id 재료에 함께 넣는다. 저장된 재검 기록과 대조할 때 어느 규칙으로
# 만들어진 id 인지 알 수 있어야 한다.
CANDIDATE_ID_ALGORITHM = "delivery_pair_v1"
# 모델이 고를 수 있는 member 종류. 기관 아래 칸은 역할이거나 행위다.
ALLOWED_MEMBER_KINDS = ("role", "action")


@dataclass(frozen=True, slots=True)
class DeliveryPairOccurrence:
    """후보 한쪽 칸이 담은 원문 하나."""

    common_ir_occurrence_id: str
    evidence_ref: str
    raw_text: str


@dataclass(frozen=True, slots=True)
class DeliveryPairSide:
    """후보 한쪽 칸의 좌표와 원문."""

    common_ir_cell_id: str
    row_index: int
    col_index: int
    col_span: int
    occurrences: tuple[DeliveryPairOccurrence, ...]


@dataclass(frozen=True, slots=True)
class DeliveryPairCandidate:
    """모델이 채택 여부만 고르면 되는 관계 후보 하나."""

    candidate_id: str
    common_ir_document_id: str | None
    common_ir_block_id: str
    actor: DeliveryPairSide
    member: DeliveryPairSide
    allowed_member_kinds: tuple[str, ...] = ALLOWED_MEMBER_KINDS

    def evidence_refs(self, side: str) -> tuple[str, ...]:
        target = self.actor if side == "actor" else self.member
        return tuple(row.evidence_ref for row in target.occurrences)


@dataclass(frozen=True, slots=True)
class DeliveryPairDiagnostic:
    """후보가 되지 못한 칸 쌍과 그 사유. 조용히 사라지지 않게 한다."""

    reason_code: str
    common_ir_block_id: str
    actor_cell_id: str
    member_cell_id: str


def _candidate_id(document_id: str | None, block_id: str, actor: TableCellRef, member: TableCellRef) -> str:
    """좌표에서 결정적으로 만든다.

    ``hash()`` 는 실행마다 달라지므로 쓰지 않는다. 정렬된 canonical JSON 의
    SHA-256 이라 같은 문서를 다시 처리하면 같은 id 가 나온다.
    """

    material = json.dumps(
        {
            "algorithm": CANDIDATE_ID_ALGORITHM,
            "common_ir_document_id": document_id or "",
            "common_ir_block_id": block_id,
            "actor": {
                "cell_id": actor.common_ir_cell_id,
                "row_index": actor.row_index,
                "col_index": actor.col_index,
                "col_span": actor.col_span,
                "occurrence_ids": sorted(actor.occurrence_ids),
            },
            "member": {
                "cell_id": member.common_ir_cell_id,
                "row_index": member.row_index,
                "col_index": member.col_index,
                "col_span": member.col_span,
                "occurrence_ids": sorted(member.occurrence_ids),
            },
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return f"dpc:{sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _side(
    document_id: str | None, cell: TableCellRef, texts: Mapping[str, str]
) -> DeliveryPairSide:
    return DeliveryPairSide(
        common_ir_cell_id=cell.common_ir_cell_id,
        row_index=cell.row_index,
        col_index=cell.col_index,
        col_span=cell.col_span,
        occurrences=tuple(
            DeliveryPairOccurrence(
                common_ir_occurrence_id=occurrence_id,
                evidence_ref=evidence_ref(
                    document_id, occurrence_id, 0, len(texts.get(occurrence_id, ""))
                ),
                raw_text=texts.get(occurrence_id, ""),
            )
            for occurrence_id in cell.occurrence_ids
        ),
    )


def _occurrence_text(common_ir: Mapping[str, Any]) -> dict[str, str]:
    return {
        occurrence["occurrence_id"]: occurrence.get("text", "")
        for block in common_ir.get("blocks") or []
        for occurrence in block.get("occurrences") or []
        if isinstance(occurrence, Mapping) and occurrence.get("occurrence_id")
    }


def build_delivery_pair_candidates(
    common_ir: Mapping[str, Any],
) -> tuple[list[DeliveryPairCandidate], list[DeliveryPairDiagnostic]]:
    """수행체계 구역 안의 표에서 관계 후보를 만든다.

    구역 밖의 표는 보지 않는다. 문서에 있는 모든 표에서 세로 인접 칸을 모으면
    관계와 무관한 쌍이 잔뜩 나온다.
    """

    texts = _occurrence_text(common_ir)
    document_id = (common_ir.get("document") or {}).get("document_id")
    # 구역 계산은 coverage 와 같은 것을 쓴다. 어느 표가 수행체계 자리인지에
    # 대해 두 곳이 다른 답을 내면 안 된다.
    table_ids: list[str] = []
    for fragment in build_fragments(common_ir, profile_field=DELIVERY_FIELD):
        if fragment.common_ir_block_id not in table_ids:
            table_ids.append(fragment.common_ir_block_id)

    candidates: list[DeliveryPairCandidate] = []
    diagnostics: list[DeliveryPairDiagnostic] = []
    for table_id in table_ids:
        pairs, skipped = find_table_column_pairs(dict(common_ir), table_id)
        for pair in pairs:
            candidates.append(
                DeliveryPairCandidate(
                    candidate_id=_candidate_id(
                        document_id, table_id, pair.actor, pair.member
                    ),
                    common_ir_document_id=document_id,
                    common_ir_block_id=table_id,
                    actor=_side(document_id, pair.actor, texts),
                    member=_side(document_id, pair.member, texts),
                )
            )
        diagnostics.extend(
            DeliveryPairDiagnostic(
                reason_code=row.reason_code,
                common_ir_block_id=row.common_ir_block_id,
                actor_cell_id=row.actor.common_ir_cell_id,
                member_cell_id=row.member.common_ir_cell_id,
            )
            for row in skipped
        )
    return candidates, diagnostics


__all__ = [
    "ALLOWED_MEMBER_KINDS",
    "CANDIDATE_ID_ALGORITHM",
    "DELIVERY_PAIR_AMBIGUOUS",
    "DeliveryPairCandidate",
    "DeliveryPairDiagnostic",
    "DeliveryPairOccurrence",
    "DeliveryPairSide",
    "build_delivery_pair_candidates",
]
