"""Focused contracts for opt-in, lossless native CandidatePack transforms."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from worker import vendor  # noqa: F401 - install vendored semantic_structuring paths

from semantic_structuring import native_line_atoms, native_span_composition
from semantic_structuring.models import CandidatePack, SourceBlock, SourceRelation
from semantic_structuring.native_exact_transform import (
    DISABLED_NATIVE_EXACT_TRANSFORMS,
    ENABLED_NATIVE_EXACT_TRANSFORMS,
    LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    NativeExactTransformOptions,
    NATIVE_EXACT_TRANSFORM_GENERATOR,
    NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    apply_native_exact_transforms,
    augment_pack_with_native_exact_transforms,
    replay_persisted_native_exact_transforms,
)
from semantic_structuring.final_profile_assembler import assemble_final_profile
from semantic_structuring.request_profile_v012 import _evidence, candidate_pack_artifact
from semantic_structuring.source_selection import _materialized_source_block


def _block(block_id: str, text: str, order: int, *, kind: str = "paragraph") -> SourceBlock:
    return SourceBlock(
        block_id=block_id,
        text=text,
        relation="candidate",
        block_kind=kind,
        section_id="main_notice",
        source_order=order,
        source_occurrence_ids=[f"{block_id}:occ"],
        common_ir_block_id=block_id,
        common_ir_occurrence_ids=(f"{block_id}:occ",),
    )


def _pack(*blocks: SourceBlock) -> CandidatePack:
    return CandidatePack(
        pack_id="native-exact-test-pack",
        notice_id="PBLN-native-exact-test",
        question="Synthetic source-preserving test only",
        blocks=list(blocks),
        generator="semantic_structuring.common_ir_v1",
        generator_version="1",
        common_ir_document_id="hwpx:PBLN-native-exact-test",
    )


def _document() -> dict[str, object]:
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": "hwpx:PBLN-native-exact-test",
            "source_kind": "hwpx",
            "provenance": {
                "source_sha256": "a" * 64,
                "source_location": "synthetic://native-exact-test",
            },
        },
    }


def test_disabled_transform_returns_the_original_pack_without_augmentation() -> None:
    pack = _pack(_block("p0", "지원 대상은\n중소기업", 0))
    before = pack.model_dump(mode="json")

    actual = augment_pack_with_native_exact_transforms(
        pack, options=DISABLED_NATIVE_EXACT_TRANSFORMS
    )

    assert actual is pack
    assert actual.model_dump(mode="json") == before
    assert "parent_pack_id" not in before
    assert "source_spans" not in before["blocks"][0]
    assert "native_parent_block_id" not in before["blocks"][0]
    assert [block.block_id for block in actual.blocks] == ["p0"]


def test_atomic_provenance_serialization_preserves_legacy_shape() -> None:
    pack = _pack(_block("p0", "지원 대상은 중소기업", 0))
    block = pack.blocks[0]

    materialized = _materialized_source_block(pack, block, text=block.text)
    evidence = _evidence(pack, block)
    artifact = candidate_pack_artifact(pack, _document())

    assert "native_parent_span" not in materialized
    assert "source_spans" not in materialized
    assert "native_parent_span" not in evidence
    assert "source_spans" not in evidence
    assert "parent_pack_id" not in artifact
    assert "parent_generator" not in artifact
    assert "parent_generator_version" not in artifact
    assert "native_parent_span" not in artifact["blocks"][0]
    assert "source_spans" not in artifact["blocks"][0]


def test_materialized_occurrence_ids_keep_unique_wire_bytes_unchanged() -> None:
    pack = _pack(_block("p0", "지원 대상은 중소기업", 0))
    block = pack.blocks[0]
    expected = {
        "source_block_id": "p0",
        "text": "지원 대상은 중소기업",
        "section_id": "main_notice",
        "source_occurrence_ids": ["p0:occ"],
        "common_ir_document_id": "hwpx:PBLN-native-exact-test",
        "common_ir_block_id": "p0",
        "common_ir_occurrence_ids": ["p0:occ"],
    }

    materialized = _materialized_source_block(pack, block, text=block.text)

    def canonical(value: object) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    assert canonical(materialized) == canonical(expected)


def test_materialized_occurrence_ids_stably_dedupe_without_rewriting_native_spans() -> None:
    left = _block("p0", "지원 대상은", 0).model_copy(update={
        "source_occurrence_ids": ["shared", "left", "shared"],
        "common_ir_occurrence_ids": ("shared", "left", "shared"),
    })
    right = _block("p1", "중소기업이다.", 1).model_copy(update={
        "source_occurrence_ids": ["shared", "right"],
        "common_ir_occurrence_ids": ("shared", "right"),
    })
    pack = augment_pack_with_native_exact_transforms(
        _pack(left, right),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    composite = next(
        block for block in pack.blocks if block.block_kind == "native_composite"
    )
    source_spans_before = [
        span.model_dump(mode="json") for span in composite.source_spans
    ]

    materialized = _materialized_source_block(
        pack, composite, text=composite.text
    )

    assert materialized["source_occurrence_ids"] == ["shared", "left", "right"]
    assert materialized["common_ir_occurrence_ids"] == [
        "shared", "left", "right",
    ]
    assert materialized["source_spans"] == source_spans_before
    assert materialized["source_spans"][0]["common_ir_occurrence_ids"] == [
        "shared", "left", "shared",
    ]
    assert "native_parent_span" not in materialized


def test_native_provenance_survives_request_existing_and_pack_artifacts() -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원\n대상은", 0),
            _block("p1", "중소기업이다.", 1),
        ),
        options=ENABLED_NATIVE_EXACT_TRANSFORMS,
    )
    line = next(block for block in pack.blocks if block.block_kind == "native_line_atom")
    composite = next(block for block in pack.blocks if block.block_kind == "native_composite")

    line_evidence = _evidence(pack, line)
    composite_evidence = _evidence(pack, composite)
    artifact = candidate_pack_artifact(pack, _document())
    artifact_blocks = {block["source_block_id"]: block for block in artifact["blocks"]}

    assert line_evidence["native_parent_span"] == {
        "source_block_id": "p0",
        "start_char": line.native_start_char,
        "end_char": line.native_end_char,
        "exact_text": line.text,
    }
    assert composite_evidence["source_spans"] == [
        span.model_dump(mode="json") for span in composite.source_spans
    ]
    assert artifact_blocks[line.block_id]["native_parent_span"] == line_evidence["native_parent_span"]
    assert artifact_blocks[composite.block_id]["source_spans"] == composite_evidence["source_spans"]
    assert artifact["parent_pack_id"] == "native-exact-test-pack"
    assert artifact["parent_generator"] == "semantic_structuring.common_ir_v1"
    assert artifact["parent_generator_version"] == "1"

    source = _materialized_source_block(pack, composite, text=composite.text)
    profile = assemble_final_profile(
        {
            "selection": {"notice_id": pack.notice_id, "support_components": []},
            "common_ir_identity": {
                "document_id": pack.common_ir_document_id,
                "source_kind": "hwpx",
                "source_sha256": "a" * 64,
            },
            "materialized_evidence": [{
                "fact_id": "purpose",
                "field_name": "purpose_goal",
                "status": "identified",
                "source_blocks": [source],
                "context_blocks": [],
            }],
        },
        {
            "notice_id": pack.notice_id,
            "common_ir": {
                "document_id": pack.common_ir_document_id,
                "schema_version": "common_ir_v1",
                "source_kind": "hwpx",
                "source_sha256": "a" * 64,
                "source_location": "synthetic://native-exact-test",
            },
        },
    )
    final_evidence = profile["comparison_profile"]["purpose_goal"][0]["evidence"][0]
    assert final_evidence["source_spans"] == composite_evidence["source_spans"]


def test_native_provenance_serialization_rechecks_composite_exact_spans() -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원 대상은", 0),
            _block("p1", "중소기업이다.", 1),
        ),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    composite = next(block for block in pack.blocks if block.block_kind == "native_composite")
    # ``model_copy`` intentionally bypasses Pydantic validation.  The
    # serialization boundary must still refuse a forged slice rather than
    # weakening the exact-span contract while preserving provenance.
    forged = composite.model_copy(update={"text": "위조된 결합 텍스트"})
    forged_pack = pack.model_copy(update={
        "blocks": [forged if block.block_id == composite.block_id else block for block in pack.blocks],
    })

    with pytest.raises(ValueError, match="native composite provenance does not exactly match"):
        _evidence(forged_pack, forged)


@pytest.mark.parametrize(
    "updates,error",
    [
        ({"native_start_char": -7}, "requires exact parent offsets"),
        ({"common_ir_block_id": "forged"}, "does not exactly match its parent span"),
        ({"relation": SourceRelation.FOLLOWING_CONTEXT}, "native candidate contract"),
    ],
)
def test_native_provenance_serialization_rechecks_line_contract(
    updates: dict[str, object], error: str,
) -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(_block("p0", "abc\ndef", 0)),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    line = next(block for block in pack.blocks if block.block_kind == "native_line_atom")
    forged = line.model_copy(update=updates)
    forged_pack = pack.model_copy(update={
        "blocks": [forged if block.block_id == line.block_id else block for block in pack.blocks],
    })

    with pytest.raises(ValueError, match=error):
        _evidence(forged_pack, forged)


def test_native_provenance_serialization_rejects_negative_composite_span() -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원 대상은", 0),
            _block("p1", "중소기업이다.", 1),
        ),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    composite = next(block for block in pack.blocks if block.block_kind == "native_composite")
    first_span = composite.source_spans[0]
    forged_span = first_span.model_copy(update={"start_char": -len(first_span.exact_text)})
    forged = composite.model_copy(update={
        "source_spans": (forged_span, *composite.source_spans[1:]),
    })
    forged_pack = pack.model_copy(update={
        "blocks": [forged if block.block_id == composite.block_id else block for block in pack.blocks],
    })

    with pytest.raises(ValueError, match="native composite span"):
        _evidence(forged_pack, forged)


def test_native_provenance_serialization_rejects_forged_composite_parent_kind() -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원 대상은", 0),
            _block("p1", "중소기업이다.", 1),
        ),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    composite = next(block for block in pack.blocks if block.block_kind == "native_composite")
    parent = next(block for block in pack.blocks if block.block_id == "p0")
    forged_parent = parent.model_copy(update={"block_kind": "forged_kind"})
    forged_pack = pack.model_copy(update={
        "blocks": [
            forged_parent if block.block_id == parent.block_id else block
            for block in pack.blocks
        ],
    })

    with pytest.raises(ValueError, match="native composite span"):
        _evidence(forged_pack, composite)
    with pytest.raises(ValueError, match="source kind is not eligible"):
        CandidatePack.model_validate(forged_pack.model_dump(mode="python"))


def test_native_heading_composite_remains_supported_and_auditable() -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("h0", "사업목적:", 0, kind="heading"),
            _block("h1", "중소기업 지원 확대", 1, kind="heading"),
        ),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    composite = next(block for block in pack.blocks if block.block_kind == "native_composite")

    assert composite.text == "사업목적: 중소기업 지원 확대"
    assert _evidence(pack, composite)["source_spans"] == [
        span.model_dump(mode="json") for span in composite.source_spans
    ]


@pytest.mark.parametrize(
    "following_text",
    [
        "□ 별도 항목",
        "▪ 하위 항목",
        "2. 다음 항목",
        "※ 각주 항목",
    ],
)
def test_native_continuations_do_not_absorb_new_structural_following_items(
    following_text: str,
) -> None:
    pack = _pack(
        _block("p0", "지원 대상은", 0),
        _block("p1", following_text, 1),
    )

    composites = native_span_composition.build_native_continuation_candidates(pack)

    assert composites == []


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("지원 대상은 (", "중소기업)", "지원 대상은 ( 중소기업)"),
        ("지원 대상은,", "중소기업이다.", "지원 대상은, 중소기업이다."),
        ("지원 대상 및", "신청 자격", "지원 대상 및 신청 자격"),
        ("사업 참여를 위", "한 기업", "사업 참여를 위 한 기업"),
    ],
)
def test_native_continuations_keep_markerless_lexical_joins(
    left: str, right: str, expected: str,
) -> None:
    composites = native_span_composition.build_native_continuation_candidates(
        _pack(_block("p0", left, 0), _block("p1", right, 1))
    )

    assert len(composites) == 1
    assert composites[0].text == expected
    assert [span.source_block_id for span in composites[0].source_spans] == [
        "p0", "p1",
    ]


@pytest.mark.parametrize(
    "kind,updates,error",
    [
        (
            "native_line_atom",
            {
                "native_parent_block_id": None,
                "native_start_char": None,
                "native_end_char": None,
            },
            "requires parent provenance",
        ),
        (
            "native_line_atom",
            {"native_parent_block_id": None},
            "offsets require parent provenance",
        ),
        (
            "native_composite",
            {"source_spans": ()},
            "requires source span provenance",
        ),
    ],
)
def test_native_provenance_serialization_rejects_removed_required_provenance(
    kind: str, updates: dict[str, object], error: str,
) -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원\n대상은", 0),
            _block("p1", "중소기업이다.", 1),
        ),
        options=ENABLED_NATIVE_EXACT_TRANSFORMS,
    )
    original = next(block for block in pack.blocks if block.block_kind == kind)
    forged = original.model_copy(update=updates)
    forged_pack = pack.model_copy(update={
        "blocks": [forged if block.block_id == original.block_id else block for block in pack.blocks],
    })

    with pytest.raises(ValueError, match=error):
        _evidence(forged_pack, forged)


@pytest.mark.parametrize(
    "updates,error",
    [
        (
            {
                "parent_pack_id": None,
                "parent_generator": None,
                "parent_generator_version": None,
            },
            "requires parent lineage",
        ),
        ({"parent_pack_id": ""}, "requires complete parent lineage"),
        ({"parent_generator_version": None}, "requires complete parent lineage"),
    ],
)
def test_candidate_pack_artifact_rechecks_transformed_parent_lineage(
    updates: dict[str, object], error: str,
) -> None:
    pack = augment_pack_with_native_exact_transforms(
        _pack(_block("p0", "지원\n대상은", 0)),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    forged = pack.model_copy(update=updates)

    with pytest.raises(ValueError, match=error):
        candidate_pack_artifact(forged, _document())


def test_native_generator_requires_parent_lineage_even_without_derived_blocks() -> None:
    transformed = augment_pack_with_native_exact_transforms(
        _pack(_block("p0", "완결된 단일 문장이다.", 0)),
        options=ENABLED_NATIVE_EXACT_TRANSFORMS,
    )
    assert not any(
        block.source_spans or block.native_parent_block_id is not None
        for block in transformed.blocks
    )
    forged = transformed.model_copy(update={
        "parent_pack_id": None,
        "parent_generator": None,
        "parent_generator_version": None,
    })

    with pytest.raises(ValueError, match="require durable parent lineage"):
        CandidatePack.model_validate(forged.model_dump(mode="python"))


def test_enabled_transform_derives_lines_then_continuations_in_reviewed_order() -> None:
    pack = _pack(
        _block("p0", "지원\n대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )

    actual = augment_pack_with_native_exact_transforms(
        pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    composites = [block for block in actual.blocks if block.block_kind == "native_composite"]
    lines = [block for block in actual.blocks if block.block_kind == "native_line_atom"]
    assert len(composites) == 1
    assert composites[0].text == "지원\n대상은 중소기업이다."
    assert [span.source_block_id for span in composites[0].source_spans] == ["p0", "p1"]
    assert [span.separator_after for span in composites[0].source_spans] == [" ", ""]
    assert len(lines) == 2
    assert lines[0].native_parent_block_id == "p0"
    assert lines[0].text == "지원"
    # Reconstructing validates the complete parent/span graph, not only each
    # helper's own return values.
    assert CandidatePack.model_validate(actual.model_dump(mode="python")) == actual


def test_line_atoms_never_interrupt_atomic_continuation_order() -> None:
    pack = _pack(
        _block("a0", "지원\n대상은", 0),
        _block("a1", "중소기업이다.", 1),
    )

    actual = augment_pack_with_native_exact_transforms(
        pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    composites = [
        block for block in actual.blocks if block.block_kind == "native_composite"
    ]
    assert [block.text for block in composites] == ["지원\n대상은 중소기업이다."]
    assert [span.source_block_id for span in composites[0].source_spans] == [
        "a0",
        "a1",
    ]


def test_explicit_line_only_option_does_not_create_composites() -> None:
    pack = _pack(
        _block("p0", "지원\n대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )

    actual = augment_pack_with_native_exact_transforms(
        pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )

    assert [block.block_kind for block in actual.blocks].count("native_line_atom") == 2
    assert not any(block.source_spans for block in actual.blocks)


def test_candidate_pack_rejects_forged_line_atom_text() -> None:
    pack = _pack(_block("p0", "지원 대상은\n중소기업", 0))
    transformed = augment_pack_with_native_exact_transforms(
        pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    payload = transformed.model_dump(mode="python")
    atom = next(
        block for block in payload["blocks"] if block.get("native_parent_block_id")
    )
    atom["text"] = "위조된 원문"

    with pytest.raises(ValueError, match="native line atom does not exactly match"):
        CandidatePack.model_validate(payload)


def test_candidate_pack_rejects_line_atom_projected_from_a_table_cell() -> None:
    pack = _pack(_block("p0", "지원 대상은\n중소기업", 0))
    transformed = augment_pack_with_native_exact_transforms(
        pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    payload = transformed.model_dump(mode="python")
    parent = next(block for block in payload["blocks"] if block["block_id"] == "p0")
    parent["block_kind"] = "table_cell"

    with pytest.raises(ValueError, match="eligible atomic candidate parent"):
        CandidatePack.model_validate(payload)


def test_candidate_pack_rejects_reserved_kinds_without_native_provenance() -> None:
    payload = _pack(_block("p0", "지원 대상", 0)).model_dump(mode="python")
    payload["blocks"][0]["block_kind"] = "native_line_atom"

    with pytest.raises(ValueError, match="reserved block kinds"):
        CandidatePack.model_validate(payload)


def test_candidate_pack_rejects_composite_spans_backed_by_line_atoms() -> None:
    transformed = augment_pack_with_native_exact_transforms(
        _pack(
            _block("p0", "지원 대상은\n중소기업", 0),
            _block("p1", "지원 내용은\n컨설팅", 1),
        ),
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    payload = transformed.model_dump(mode="python")
    atoms = [
        block for block in payload["blocks"]
        if block.get("native_parent_block_id") is not None
    ][:2]
    spans = [
        {
            "source_block_id": atom["block_id"],
            "exact_text": atom["text"],
            "start_char": 0,
            "end_char": len(atom["text"]),
            "separator_after": " " if index == 0 else "",
            "source_order": atom["source_order"],
            "section_id": atom["section_id"],
            "common_ir_block_id": atom["common_ir_block_id"],
            "common_ir_occurrence_ids": atom["common_ir_occurrence_ids"],
            "common_ir_cell_id": atom["common_ir_cell_id"],
        }
        for index, atom in enumerate(atoms)
    ]
    occurrences = [
        occurrence
        for span in spans
        for occurrence in span["common_ir_occurrence_ids"]
    ]
    payload["blocks"].append(
        {
            "block_id": "composite:forged-from-lines",
            "text": " ".join(atom["text"] for atom in atoms),
            "relation": "candidate",
            "block_kind": "native_composite",
            "section_id": spans[0]["section_id"],
            "source_order": spans[0]["source_order"],
            "source_occurrence_ids": occurrences,
            "common_ir_block_id": spans[0]["common_ir_block_id"],
            "common_ir_cell_id": None,
            "common_ir_occurrence_ids": occurrences,
            "source_spans": spans,
        }
    )

    with pytest.raises(ValueError, match="atomic pack block"):
        CandidatePack.model_validate(payload)


def test_candidate_pack_rejects_forged_composite_separator_and_table_join() -> None:
    pack = _pack(
        _block("p0", "지원 대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )
    transformed = augment_pack_with_native_exact_transforms(
        pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    payload = transformed.model_dump(mode="python")
    composite = next(block for block in payload["blocks"] if block.get("source_spans"))
    composite["source_spans"][0]["separator_after"] = ""
    with pytest.raises(ValueError, match="lossless span composition"):
        CandidatePack.model_validate(payload)

    table_pack = _pack(
        _block("p0", "지원 대상은", 0),
        _block("table0", "중소기업이다.", 1, kind="table_cell"),
    )
    table_actual = augment_pack_with_native_exact_transforms(
        table_pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )
    assert not any(block.source_spans for block in table_actual.blocks)


def test_enabled_facade_is_idempotent_and_rejects_duplicate_ids() -> None:
    pack = _pack(
        _block("p0", "지원\n대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )
    once = augment_pack_with_native_exact_transforms(
        pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    twice = augment_pack_with_native_exact_transforms(
        once, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    assert twice.model_dump(mode="python") == once.model_dump(mode="python")
    assert once.pack_id.startswith(
        "native-exact-test-pack-native-exact-v2-lines+continuations-"
    )
    assert len(once.pack_id.rsplit("-", 1)[1]) == 16
    assert once.generator == NATIVE_EXACT_TRANSFORM_GENERATOR
    assert once.generator_version == (
        f"{NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION}:lines+continuations"
    )
    assert once.common_ir_document_id == "hwpx:PBLN-native-exact-test"

    result = apply_native_exact_transforms(pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS)
    assert result.pack == once
    assert result.parent_pack_id == "native-exact-test-pack"
    assert result.parent_generator == "semantic_structuring.common_ir_v1"

    line_only = augment_pack_with_native_exact_transforms(
        pack,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=True,
            include_continuations=False,
        ),
    )
    assert line_only.pack_id != once.pack_id
    assert line_only.generator_version != once.generator_version
    repeated_result = apply_native_exact_transforms(once, options=ENABLED_NATIVE_EXACT_TRANSFORMS)
    assert repeated_result.parent_pack_id == result.parent_pack_id
    assert repeated_result.parent_generator == result.parent_generator
    assert repeated_result.parent_generator_version == result.parent_generator_version

    duplicate = once.model_dump(mode="python")
    duplicate["blocks"].append(deepcopy(duplicate["blocks"][0]))
    with pytest.raises(ValueError, match="block_id values must be unique"):
        CandidatePack.model_validate(duplicate)

    missing_parent = once.model_dump(mode="python")
    missing_parent.pop("parent_pack_id")
    missing_parent.pop("parent_generator")
    missing_parent.pop("parent_generator_version")
    with pytest.raises(ValueError, match="require durable parent lineage"):
        CandidatePack.model_validate(missing_parent)

    changed_source = _pack(
        _block("p0", "지원\n대상은 변경됨", 0),
        _block("p1", "중소기업이다.", 1),
    )
    changed = augment_pack_with_native_exact_transforms(
        changed_source, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    assert changed.pack_id != once.pack_id


def test_v1_producer_replays_legacy_continuation_identity_while_v2_is_safe() -> None:
    base = _pack(
        _block("p0", "지원 대상은", 0),
        _block("p1", "□ 별도 항목", 1),
    )

    legacy = replay_persisted_native_exact_transforms(
        base,
        producer_version=LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
        include_line_atoms=False,
        include_continuations=True,
    )
    current = augment_pack_with_native_exact_transforms(
        base,
        options=NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=True,
        ),
    )

    assert legacy.pack_id.startswith(
        "native-exact-test-pack-native-exact-v1-continuations-"
    )
    assert legacy.generator_version == "1:continuations"
    assert any(block.block_kind == "native_composite" for block in legacy.blocks)
    assert current.pack_id.startswith(
        "native-exact-test-pack-native-exact-v2-continuations-"
    )
    assert current.generator_version == "2:continuations"
    assert not any(block.block_kind == "native_composite" for block in current.blocks)

def test_enabled_facade_identity_is_stable_when_input_blocks_are_not_preordered() -> None:
    unordered = _pack(
        _block("p1", "중소기업이다.", 1),
        _block("p0", "지원 대상은", 0),
    )
    ordered = _pack(
        _block("p0", "지원 대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )

    once = augment_pack_with_native_exact_transforms(
        unordered, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    twice = augment_pack_with_native_exact_transforms(
        once, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    canonical = augment_pack_with_native_exact_transforms(
        ordered, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    assert twice.pack_id == once.pack_id
    assert twice.model_dump(mode="python") == once.model_dump(mode="python")
    assert canonical.pack_id == once.pack_id
    assert canonical.model_dump(mode="python") == once.model_dump(mode="python")


def test_transform_identity_changes_when_the_pack_question_changes() -> None:
    pack = _pack(_block("p0", "지원 대상", 0))
    changed_question = pack.model_copy(update={"question": "Another trusted task"})

    original = augment_pack_with_native_exact_transforms(
        pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )
    changed = augment_pack_with_native_exact_transforms(
        changed_question, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    assert original.pack_id != changed.pack_id


def test_transform_preserves_trusted_private_table_geometry() -> None:
    table_cell = _block("table0#r0c0p0", "표 값", 0, kind="table_cell")
    table_cell._common_ir_cell_geometry = (0, 1, 0, 1)
    pack = _pack(
        table_cell,
        _block("p0", "지원 대상은", 1),
        _block("p1", "중소기업이다.", 2),
    )

    actual = augment_pack_with_native_exact_transforms(
        pack, options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    preserved = next(block for block in actual.blocks if block.block_id == table_cell.block_id)
    assert preserved._common_ir_cell_geometry == (0, 1, 0, 1)


def test_options_require_explicit_contract_object() -> None:
    pack = _pack(_block("p0", "지원 대상은\n중소기업", 0))
    with pytest.raises(TypeError, match="NativeExactTransformOptions"):
        augment_pack_with_native_exact_transforms(pack, options=deepcopy({"enabled": True}))  # type: ignore[arg-type]


def test_public_transform_options_cannot_select_a_legacy_producer() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        NativeExactTransformOptions(producer_version="1")  # type: ignore[call-arg]


def test_enabled_transform_rejects_an_empty_transform_set_and_legacy_lineage() -> None:
    with pytest.raises(ValueError, match="at least one transform"):
        NativeExactTransformOptions(
            enabled=True,
            include_line_atoms=False,
            include_continuations=False,
        )
    legacy = CandidatePack(
        pack_id="legacy-pack",
        notice_id="legacy-notice",
        question="Synthetic legacy source",
        blocks=[SourceBlock(block_id="legacy", text="a\nb", relation="candidate")],
    )
    with pytest.raises(ValueError, match="complete Common IR CandidatePack lineage"):
        augment_pack_with_native_exact_transforms(
            legacy, options=ENABLED_NATIVE_EXACT_TRANSFORMS
        )


def test_enabled_transform_validates_lineage_even_when_no_candidates_are_derived() -> None:
    pack = _pack(_block("p0", "완결된 문장이다.", 0))
    forged = pack.model_copy(
        update={
            "generator": "another.valid.generator",
            "generator_version": "1",
            "parent_pack_id": "original-pack",
            "parent_generator": NATIVE_EXACT_TRANSFORM_GENERATOR,
            "parent_generator_version": (
                f"{NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION}:lines+continuations"
            ),
        }
    )
    # The input lineage is valid; applying the requested variant would make
    # generator identity equal to its parent unless the final graph is always
    # validated, including the no-derived-block path.
    CandidatePack.model_validate(forged.model_dump(mode="python"))

    with pytest.raises(ValueError, match="generator identity must differ"):
        augment_pack_with_native_exact_transforms(
            forged, options=ENABLED_NATIVE_EXACT_TRANSFORMS
        )


def test_context_blocks_are_never_promoted_to_native_candidates() -> None:
    context = _block("p0", "지원 대상은\n중소기업", 0).model_copy(
        update={"relation": SourceRelation.PRECEDING_CONTEXT}
    )
    following = _block("p1", "지원 내용", 1).model_copy(
        update={"relation": SourceRelation.FOLLOWING_CONTEXT}
    )

    actual = augment_pack_with_native_exact_transforms(
        _pack(context, following), options=ENABLED_NATIVE_EXACT_TRANSFORMS
    )

    assert [block.block_id for block in actual.blocks] == ["p0", "p1"]
    assert not any(
        block.native_parent_block_id is not None or block.source_spans
        for block in actual.blocks
    )


def test_candidate_pack_rejects_control_characters_before_composite_identity() -> None:
    with pytest.raises(ValueError, match="ASCII control characters"):
        _pack(
            _block("a\0b", "지원 대상은", 0),
            _block("c", "중소기업이다.", 1),
        )


def test_native_derived_candidate_resource_limits_fail_instead_of_truncating(
    monkeypatch,
) -> None:
    multiline = _pack(_block("p0", "첫째\n둘째", 0))
    monkeypatch.setattr(native_line_atoms, "MAX_NATIVE_LINE_ATOMS", 1)
    with pytest.raises(ValueError, match="native line atom limit exceeded"):
        augment_pack_with_native_exact_transforms(
            multiline,
            options=NativeExactTransformOptions(
                enabled=True,
                include_line_atoms=True,
                include_continuations=False,
            ),
        )

    monkeypatch.setattr(native_line_atoms, "MAX_NATIVE_LINE_ATOMS", 10_000)
    monkeypatch.setattr(
        native_line_atoms, "MAX_NATIVE_LINE_PROVENANCE_REFERENCES", 1
    )
    with pytest.raises(ValueError, match="provenance reference limit exceeded"):
        augment_pack_with_native_exact_transforms(
            multiline,
            options=NativeExactTransformOptions(
                enabled=True,
                include_line_atoms=True,
                include_continuations=False,
            ),
        )

    monkeypatch.setattr(
        native_line_atoms, "MAX_NATIVE_LINE_PROVENANCE_REFERENCES", 100_000
    )
    monkeypatch.setattr(native_line_atoms, "MAX_NATIVE_LINE_TEXT_BYTES", 1)
    with pytest.raises(ValueError, match="line text byte limit exceeded"):
        augment_pack_with_native_exact_transforms(
            multiline,
            options=NativeExactTransformOptions(
                enabled=True,
                include_line_atoms=True,
                include_continuations=False,
            ),
        )

    continuation = _pack(
        _block("p0", "지원 대상은", 0),
        _block("p1", "중소기업이다.", 1),
    )
    monkeypatch.setattr(
        native_span_composition, "MAX_NATIVE_CONTINUATION_CANDIDATES", 0
    )
    with pytest.raises(ValueError, match="continuation candidate limit exceeded"):
        augment_pack_with_native_exact_transforms(
            continuation,
            options=NativeExactTransformOptions(
                enabled=True,
                include_line_atoms=False,
                include_continuations=True,
            ),
        )


    monkeypatch.setattr(
        native_span_composition, "MAX_NATIVE_CONTINUATION_CANDIDATES", 10_000
    )
    monkeypatch.setattr(
        native_span_composition, "MAX_NATIVE_CONTINUATION_TEXT_BYTES", 1
    )
    with pytest.raises(ValueError, match="continuation text byte limit exceeded"):
        augment_pack_with_native_exact_transforms(
            continuation,
            options=NativeExactTransformOptions(
                enabled=True,
                include_line_atoms=False,
                include_continuations=True,
            ),
        )
