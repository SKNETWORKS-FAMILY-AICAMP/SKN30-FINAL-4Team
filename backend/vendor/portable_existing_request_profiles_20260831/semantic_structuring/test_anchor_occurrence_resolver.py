"""Focused executable checks for CandidatePack Anchor Occurrence Resolver v1."""

from __future__ import annotations

from pydantic import ValidationError

from semantic_structuring.anchor_occurrence_resolver import (
    AnchorOccurrenceRequest,
    materialize_selected_anchor_candidate,
    resolve_anchor_occurrences,
)
from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation


def common_ir_pack(text: str) -> CandidatePack:
    return CandidatePack(
        pack_id="PBLN-test-a-profile-v0.2",
        notice_id="PBLN-test",
        question="test",
        generator="semantic_structuring.common_ir_v1",
        generator_version="1",
        common_ir_document_id="hwpx:PBLN-test",
        blocks=[
            SourceBlock(
                block_id="hwpx:t4#r1c2p0",
                text=text,
                relation=SourceRelation.CANDIDATE,
                block_kind="table_cell",
                common_ir_block_id="hwpx:t4",
                common_ir_cell_id="hwpx:t4:c5",
                common_ir_occurrence_ids=("hwpx:occ:9",),
            )
        ],
    )


TRUSTED_SHA256 = "a" * 64


def request(anchor_text: str, *, source_sha256: str = TRUSTED_SHA256) -> AnchorOccurrenceRequest:
    return AnchorOccurrenceRequest(
        common_ir_document_id="hwpx:PBLN-test",
        common_ir_source_sha256=source_sha256,
        candidate_pack_id="PBLN-test-a-profile-v0.2",
        candidate_pack_generator="semantic_structuring.common_ir_v1",
        candidate_pack_generator_version="1",
        source_block_id="hwpx:t4#r1c2p0",
        anchor_text=anchor_text,
    )


def main() -> None:
    # Unique anchors retain the existing exact-span path and expose no IDs.
    unique = resolve_anchor_occurrences(
        request("300만원"),
        common_ir_pack("기업당 300만원 지원"),
        trusted_common_ir_source_sha256=TRUSTED_SHA256,
    )
    assert unique.status == "unique"
    assert unique.candidates == []
    assert unique.value_source is not None
    assert unique.value_source.source_block_id == "hwpx:t4#r1c2p0"
    assert unique.value_source.start_char == 4
    assert unique.value_source.end_char == 9

    # Repeated anchors return every actual occurrence, preserve table
    # provenance, and make stable CandidatePack-local IDs.
    repeated_text = "지원금 300만원, 추가 지원금 300만원"
    pack = common_ir_pack(repeated_text)
    first = resolve_anchor_occurrences(
        request("지원금"), pack, trusted_common_ir_source_sha256=TRUSTED_SHA256, context_window=6
    )
    second = resolve_anchor_occurrences(
        request("지원금"), pack, trusted_common_ir_source_sha256=TRUSTED_SHA256, context_window=6
    )
    assert first.status == "ambiguous"
    assert first.value_source is None
    assert len(first.candidates) == 2
    assert [candidate.start_char for candidate in first.candidates] == [
        repeated_text.find("지원금"), repeated_text.find("지원금", 1)
    ]
    assert [candidate.candidate_id for candidate in first.candidates] == [candidate.candidate_id for candidate in second.candidates]
    assert first.candidates[0].common_ir_block_id == "hwpx:t4"
    assert first.candidates[0].common_ir_cell_id == "hwpx:t4:c5"
    assert first.candidates[0].common_ir_occurrence_ids == ("hwpx:occ:9",)
    assert first.candidates[1].context_before == repeated_text[
        max(0, first.candidates[1].start_char - 6):first.candidates[1].start_char
    ]
    raw, source = materialize_selected_anchor_candidate(first, first.candidates[1].candidate_id)
    assert raw == "지원금"
    assert source.start_char == repeated_text.find("지원금", 1)
    assert source.end_char == source.start_char + len("지원금")

    # All true occurrences includes overlaps; no server-side first-hit rule.
    overlapping = resolve_anchor_occurrences(
        request("aa"), common_ir_pack("aaa"), trusted_common_ir_source_sha256=TRUSTED_SHA256
    )
    assert [candidate.start_char for candidate in overlapping.candidates] == [0, 1]

    # Exact-substring and CandidatePack lineage checks fail closed.
    try:
        resolve_anchor_occurrences(request("없는값"), pack, trusted_common_ir_source_sha256=TRUSTED_SHA256)
    except ValueError as exc:
        assert "exact substring" in str(exc)
    else:
        raise AssertionError("missing exact substring must fail")

    wrong_pack = pack.model_copy(update={"pack_id": "other-pack"})
    try:
        resolve_anchor_occurrences(request("지원금"), wrong_pack, trusted_common_ir_source_sha256=TRUSTED_SHA256)
    except ValueError as exc:
        assert "candidate_pack_id" in str(exc)
    else:
        raise AssertionError("lineage mismatch must fail")

    # Hash values are fixed-format lineage identifiers, never free-form text.
    try:
        request("지원금", source_sha256="A" * 64)
    except ValidationError as exc:
        assert "common_ir_source_sha256" in str(exc)
    else:
        raise AssertionError("uppercase source hash must fail validation")

    try:
        resolve_anchor_occurrences(
            request("지원금"),
            pack,
            trusted_common_ir_source_sha256="not-a-sha256",
        )
    except ValueError as exc:
        assert "trusted_common_ir_source_sha256" in str(exc)
    else:
        raise AssertionError("malformed trusted source hash must fail")

    try:
        resolve_anchor_occurrences(
            request("지원금", source_sha256="b" * 64),
            pack,
            trusted_common_ir_source_sha256=TRUSTED_SHA256,
        )
    except ValueError as exc:
        assert "does not match trusted Common IR lineage" in str(exc)
    else:
        raise AssertionError("request/trusted source hash mismatch must fail")


if __name__ == "__main__":
    main()
