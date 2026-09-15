from __future__ import annotations

from copy import deepcopy
import csv
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from scripts import verify_existing_gold100 as verifier


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _seal(root: Path, pblanc_id: str) -> None:
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name not in {"freeze_manifest.json", "artifact_index.json", "README.md"}
    )
    artifact_rows = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        artifact_rows.append(
            {
                "notice_id": pblanc_id if relative.startswith(f"notices/{pblanc_id}/") else None,
                "artifact": path.stem if path.parent.name != "governance" else f"governance:{path.name}",
                "path": relative,
                "sha256": _digest(path),
                "bytes": path.stat().st_size,
            }
        )
    _write_json(
        root / "artifact_index.json",
        {"schema_version": verifier.ARTIFACT_INDEX_SCHEMA, "artifacts": artifact_rows},
    )
    _write_json(
        root / "freeze_manifest.json",
        {
            "contract": verifier.FREEZE_CONTRACT,
            "freeze_status": "FROZEN",
            "dataset_version": "synthetic-existing-profile-gold/v5",
            "notice_count": 1,
            "role_counts": {"answer": 1},
            "source_counts": {"synthetic": 1},
            "strict_validation": {"passed": 1, "failed": 0},
            "fact_count": 1,
            "fact_status_counts": {"identified": 1},
            "freeze_policy": {
                "source_mutation": "forbidden",
                "ocr_only_core_evidence": "forbidden",
                "exact_provenance_required": True,
            },
            "retrieval_ambiguity_overlay": {
                "notice_count": 1,
                "notice_ids": [pblanc_id],
                "decision_ledger_sha256": _digest(root / "governance" / "decisions.jsonl"),
                "semantic_regression_report_sha256": _digest(root / "governance" / "semantic_regression_report.json"),
            },
            "artifact_index_sha256": _digest(root / "artifact_index.json"),
            "profile_manifest_sha256": _digest(root / "profile_manifest.json"),
            "sample_100_sha256": _digest(root / "sample_100.csv"),
        },
    )


def _write_gold(root: Path) -> str:
    pblanc_id = "PBLN_SYNTHETIC_001"
    document_id = f"pdf:{pblanc_id}"
    source_sha = "a" * 64
    source_text = "명시적 지원\n내용"
    fact = {
        "fact_id": "fact-support-content",
        "field_name": "support_content",
        "value_raw": source_text,
        "status": "identified",
        "scope": "notice",
        "support_component_id": None,
        "applicability_component_ids": [],
        "modifies_fact_ids": [],
        "recipient_fact_ids": [],
        "basis_fact_ids": [],
        "value_source": {
            "source_block_id": "block-1",
            "start_char": 0,
            "end_char": len(source_text),
            "text_basis": verifier.TEXT_BASIS,
        },
        "evidence": [
            {
                "source_block_id": "block-1",
                "section_id": "main_notice",
                "source_occurrence_ids": ["occ-1"],
                "common_ir_document_id": document_id,
                "common_ir_block_id": "block-1",
                "common_ir_occurrence_ids": ["occ-1"],
            }
        ],
        "context_evidence": [],
    }
    profile = {
        "schema_version": verifier.PROFILE_SCHEMA,
        "notice_id": f"bizinfo:{pblanc_id}",
        "source_profile_id": document_id,
        "source_documents": [
            {
                "common_ir": {
                    "document_id": document_id,
                    "schema_version": verifier.COMMON_IR_SCHEMA,
                    "source_kind": "pdf",
                    "source_sha256": source_sha,
                    "source_location": "external/source.pdf",
                }
            }
        ],
        "comparison_profile": {"support_content": [fact]},
        "support_components": [],
        "derived_projections": [],
        "processing_metadata": {
            "candidate_pack": {
                "candidate_pack_id": "synthetic-pack",
                "common_ir_document_id": document_id,
                "common_ir_source_sha256": source_sha,
                "text_basis": verifier.TEXT_BASIS,
            }
        },
    }
    selection = {
        "selection_contract": verifier.SELECTION_CONTRACT,
        "selection": {
            "notice_id": pblanc_id,
            "candidate_pack_id": "synthetic-pack",
            "facts": [
                {
                    "fact_id": fact["fact_id"],
                    "field_name": fact["field_name"],
                    "status": fact["status"],
                    "subject_role": None,
                    "semantic_role": None,
                    "canonical_role": None,
                    "value_anchor": {
                        "source_block_id": "block-1",
                        "anchor_text": source_text,
                    },
                    "context_source_block_ids": [],
                    "primary_component_id": None,
                    "applicability_component_ids": [],
                    "modifies_fact_ids": [],
                    "recipient_fact_ids": [],
                    "basis_fact_ids": [],
                }
            ],
            "support_components": [],
        },
        "source_block_texts": {"block-1": source_text},
        "common_ir_identity": {"document_id": document_id, "source_kind": "pdf", "source_sha256": source_sha},
        "candidate_pack_lineage": {
            "candidate_pack_id": "synthetic-pack",
            "common_ir_document_id": document_id,
            "common_ir_source_sha256": source_sha,
            "text_basis": verifier.TEXT_BASIS,
        },
        "materialized_evidence": [
            {
                "fact_id": fact["fact_id"],
                "field_name": fact["field_name"],
                "status": fact["status"],
                "subject_role": None,
                "semantic_role": None,
                "canonical_role": None,
                "source_blocks": [
                    {
                        "source_block_id": "block-1",
                        "text": source_text,
                        "section_id": "main_notice",
                        "source_occurrence_ids": ["occ-1"],
                        "common_ir_document_id": document_id,
                        "common_ir_block_id": "block-1",
                        "common_ir_occurrence_ids": ["occ-1"],
                    }
                ],
                "value_source": fact["value_source"],
                "context_blocks": [],
                "primary_component_id": None,
                "applicability_component_ids": [],
                "modifies_fact_ids": [],
                "recipient_fact_ids": [],
                "basis_fact_ids": [],
            }
        ],
        "materialized_components": [],
    }
    common_ir = {
        "schema_version": verifier.COMMON_IR_SCHEMA,
        "document": {
            "document_id": document_id,
            "source_kind": "pdf",
            "provenance": {"source_sha256": source_sha, "source_location": "external/source.pdf"},
        },
        "blocks": [{
            "block_id": "block-1",
            "text": source_text,
            "kind": "paragraph",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": ["occ-1"],
            "occurrences": [{"occurrence_id": "occ-1", "text": source_text}],
        }],
    }
    notice = root / "notices" / pblanc_id
    _write_json(notice / "existing_profile.v0.2.json", profile)
    _write_json(notice / "source_selection.v0.2.json", selection)
    _write_json(notice / "common_ir_v1.json", common_ir)

    profile_manifest = {
        "schema_version": verifier.PROFILE_MANIFEST_SCHEMA,
        "profiles": [
            {
                "pblanc_id": pblanc_id,
                "notice_id": f"bizinfo:{pblanc_id}",
                "role": "answer",
                "title": "Synthetic Gold Notice",
                "support_field": "test",
                "source_kind": "synthetic",
                "source": {
                    kind: {"path": f"external/{name}", "sha256": _digest(notice / name)}
                    for kind, name in verifier._NOTICE_ARTIFACTS.items()
                },
                "frozen": {
                    kind: {"path": f"notices/{pblanc_id}/{name}", "sha256": _digest(notice / name)}
                    for kind, name in verifier._NOTICE_ARTIFACTS.items()
                },
            }
        ],
    }
    _write_json(root / "profile_manifest.json", profile_manifest)
    _write_csv(
        root / "sample_100.csv",
        ["pblanc_id", "notice_id", "role", "title", "support_field"],
        [{"pblanc_id": pblanc_id, "notice_id": f"bizinfo:{pblanc_id}", "role": "answer", "title": "Synthetic Gold Notice", "support_field": "test"}],
    )
    _write_json(
        root / "answer_set.json",
        {"schema_version": "answer_set.v1", "answers": [{"pblanc_id": pblanc_id, "title": "Synthetic Gold Notice", "support_field": "test"}]},
    )
    _write_csv(root / "competitor_mapping.csv", ["answer_pblanc_id", "competitor_pblanc_id"], [])
    _write_csv(root / "reserve_pool.csv", ["pblanc_id"], [])
    _write_csv(root / "replacement_log.csv", ["replacement_stage"], [])

    governance = root / "governance"
    _write_json(governance / "materialization_report.json", {"status": "passed", "notice_count": 1, "reports": [{"notice_id": pblanc_id, "status": "passed"}]})
    _write_json(governance / "semantic_regression_report.json", {"status": "passed", "notice_count": 1, "reports": [{"notice_id": pblanc_id, "status": "passed"}]})
    (governance / "decisions.jsonl").write_text(json.dumps({"notice_id": pblanc_id}) + "\n", encoding="utf-8")
    _write_json(governance / "v4_materialization_report.json", {"status": "passed", "notice_count": 1, "reports": [{"notice_id": pblanc_id, "status": "passed"}]})
    _write_json(governance / "v4_semantic_regression_report.json", {"status": "passed", "notice_count": 1, "reports": [{"notice_id": pblanc_id, "status": "passed"}]})
    (governance / "v4_decisions.jsonl").write_text(json.dumps({"notice_id": pblanc_id}) + "\n", encoding="utf-8")
    _write_json(
        governance / "v4_v3_manifest.json",
        {
            "all_contract_valid": True,
            "all_exact_span_valid": True,
            "all_common_ir_provenance_valid": True,
            "all_native_occurrence_valid": True,
            "all_component_relation_valid": True,
            "all_placeholder_and_duplicate_valid": True,
        },
    )
    _seal(root, pblanc_id)
    return pblanc_id


def _read_notice_triple(
    root: Path, pblanc_id: str
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    notice = root / "notices" / pblanc_id
    return tuple(
        json.loads((notice / name).read_text(encoding="utf-8"))
        for name in (
            "existing_profile.v0.2.json",
            "source_selection.v0.2.json",
            "common_ir_v1.json",
        )
    )  # type: ignore[return-value]


def _install_native_line_atom(
    profile: dict[str, object],
    selection: dict[str, object],
    common_ir: dict[str, object],
    *,
    producer_version: str = verifier.NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
) -> None:
    """Turn the synthetic atomic fact into one lossless native line atom."""

    base_pack = verifier._existing_a_base_candidate_pack(
        common_ir, {"block-1"}, label="synthetic native line"
    )
    transformed = verifier.replay_persisted_native_exact_transforms(
        base_pack,
        producer_version=producer_version,
        include_line_atoms=True,
        include_continuations=False,
    )
    line = next(block for block in transformed.blocks if block.native_parent_block_id is not None)
    derived_id = line.block_id
    value = line.text
    native_payload = verifier._native_block_payload(line)
    parent_span = native_payload["native_parent_span"]
    profile_fact = profile["comparison_profile"]["support_content"][0]  # type: ignore[index]
    profile_fact["value_source"] = {  # type: ignore[index]
        "source_block_id": derived_id,
        "start_char": 0,
        "end_char": len(value),
        "text_basis": verifier.TEXT_BASIS,
    }
    profile_fact["value_raw"] = value  # type: ignore[index]
    profile_fact["evidence"][0].update(native_payload)  # type: ignore[index]
    selected = selection["selection"]["facts"][0]  # type: ignore[index]
    selected["value_anchor"] = {"source_block_id": derived_id, "anchor_text": value}
    selection["source_block_texts"] = {  # type: ignore[index]
        block.block_id: block.text for block in transformed.blocks
    }
    materialized = selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["value_source"] = profile_fact["value_source"]  # type: ignore[index]
    materialized["source_blocks"][0].update(native_payload)  # type: ignore[index]
    materialized["source_blocks"][0]["text"] = value  # type: ignore[index]
    for lineage in (
        selection["candidate_pack_lineage"],  # type: ignore[index]
        profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
    ):
        lineage.update({
            "candidate_pack_id": transformed.pack_id,
            "candidate_pack_generator": transformed.generator,
            "candidate_pack_generator_version": transformed.generator_version,
            "parent_pack_id": transformed.parent_pack_id,
            "parent_generator": transformed.parent_generator,
            "parent_generator_version": transformed.parent_generator_version,
        })
    selection["selection"]["candidate_pack_id"] = transformed.pack_id  # type: ignore[index]


def _install_second_native_line_as_context(
    profile: dict[str, object],
    selection: dict[str, object],
    common_ir: dict[str, object],
) -> None:
    lineage = selection["candidate_pack_lineage"]  # type: ignore[index]
    transformed = verifier.regenerate_existing_native_candidate_pack(
        common_ir,
        selection["source_block_texts"],  # type: ignore[index]
        lineage,
        label="synthetic native context",
    )
    assert transformed is not None
    primary_id = profile["comparison_profile"]["support_content"][0]["value_source"][  # type: ignore[index]
        "source_block_id"
    ]
    context_block = next(
        block
        for block in transformed.blocks
        if block.native_parent_block_id is not None and block.block_id != primary_id
    )
    context = {
        **verifier._native_block_payload(context_block),
        "common_ir_document_id": transformed.common_ir_document_id,
        "text": context_block.text,
    }
    selection["selection"]["facts"][0]["context_source_block_ids"] = [  # type: ignore[index]
        context_block.block_id
    ]
    selection["materialized_evidence"][0]["context_blocks"] = [deepcopy(context)]  # type: ignore[index]
    profile["comparison_profile"]["support_content"][0]["context_evidence"] = [  # type: ignore[index]
        deepcopy(context)
    ]


def _install_named_component(
    profile: dict[str, object],
    selection: dict[str, object],
) -> None:
    profile["support_components"] = [
        {
            "support_component_id": "component-1",
            "component_kind": "support_package",
            "name_raw": "명시적 지원 내용",
            "name_status": "identified",
            "name_source_block_id": "block-1",
            "source_block_ids": ["block-1"],
            "table_block_ids": [],
            "facts": [],
        }
    ]
    selection["selection"]["support_components"] = [  # type: ignore[index]
        {
            "support_component_id": "component-1",
            "component_kind": "support_package",
            "source_block_ids": ["block-1"],
            "table_block_ids": [],
            "name_anchor": {
                "source_block_id": "block-1",
                "anchor_text": "명시적 지원 내용",
            },
        }
    ]
    selection["materialized_components"] = [
        {
            "support_component_id": "component-1",
            "name_raw": "명시적 지원 내용",
            "name_source_block_id": "block-1",
        }
    ]


def test_verifies_a_self_contained_synthetic_gold_freeze(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "gold"
    _write_gold(root)

    report = verifier.verify_gold_root(root, expected_notice_count=1)

    assert report.status == "valid"
    assert report.notice_count == 1
    assert report.fact_count == 1
    assert verifier.main(["--gold-root", str(root), "--expected-notice-count", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"


def test_rejects_a_manifest_pin_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    (root / "sample_100.csv").write_text("pblanc_id\nchanged\n", encoding="utf-8")

    with pytest.raises(verifier.GoldVerificationError, match="freeze manifest SHA-256 pin mismatch: sample_100.csv"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_rejects_provenance_tampering_even_when_artifacts_are_resealed(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    selection_path = root / "notices" / pblanc_id / "source_selection.v0.2.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["common_ir_identity"]["source_sha256"] = "b" * 64
    _write_json(selection_path, selection)
    profile_manifest_path = root / "profile_manifest.json"
    profile_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
    replacement_digest = _digest(selection_path)
    profile_manifest["profiles"][0]["source"]["selection"]["sha256"] = replacement_digest
    profile_manifest["profiles"][0]["frozen"]["selection"]["sha256"] = replacement_digest
    _write_json(profile_manifest_path, profile_manifest)
    _seal(root, pblanc_id)

    with pytest.raises(verifier.GoldVerificationError, match="common_ir_identity.source_sha256 does not match"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_rejects_selection_materialization_mismatch_even_when_resealed(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    selection_path = root / "notices" / pblanc_id / "source_selection.v0.2.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["materialized_evidence"][0]["semantic_role"] = "forged-role"
    _write_json(selection_path, selection)

    profile_manifest_path = root / "profile_manifest.json"
    profile_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
    replacement_digest = _digest(selection_path)
    profile_manifest["profiles"][0]["source"]["selection"]["sha256"] = replacement_digest
    profile_manifest["profiles"][0]["frozen"]["selection"]["sha256"] = replacement_digest
    _write_json(profile_manifest_path, profile_manifest)
    _seal(root, pblanc_id)

    with pytest.raises(verifier.GoldVerificationError, match="selection/materialized evidence"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_rejects_delivery_organization_anchor_text_drift(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    fact = profile["comparison_profile"].pop("support_content")[0]  # type: ignore[index,union-attr]
    fact["field_name"] = "delivery_roles"
    fact["organization_names"] = [
        {"value_raw": fact["value_raw"], "value_source": fact["value_source"]}
    ]
    fact.update(
        {
            "role_raw": fact["value_raw"],
            "role_source_block_id": "block-1",
            "role_source": fact["value_source"],
            "canonical_role": None,
        }
    )
    profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    selected = selection["selection"]["facts"][0]  # type: ignore[index]
    selected["field_name"] = "delivery_roles"
    selected["organization_anchors"] = [
        {"source_block_id": "block-1", "anchor_text": "변조된 기관명"}
    ]
    selected["role_anchor"] = {
        "source_block_id": "block-1",
        "anchor_text": fact["value_raw"],
    }
    materialized = selection["materialized_evidence"][0]  # type: ignore[index]
    materialized["field_name"] = "delivery_roles"
    materialized.update(
        {
            "organization_names": [fact["value_raw"]],
            "organization_sources": [fact["value_source"]],
            "role_raw": fact["value_raw"],
            "role_source_block_id": "block-1",
            "role_source": fact["value_source"],
            "canonical_role": None,
        }
    )

    with pytest.raises(
        verifier.GoldVerificationError,
        match="selection/materialized organization anchors differ",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_delivery_role_anchor_text_drift(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    fact = profile["comparison_profile"].pop("support_content")[0]  # type: ignore[index,union-attr]
    fact["field_name"] = "delivery_roles"
    fact.update(
        {
            "organization_names": [
                {"value_raw": fact["value_raw"], "value_source": fact["value_source"]}
            ],
            "role_raw": fact["value_raw"],
            "role_source_block_id": "block-1",
            "role_source": fact["value_source"],
            "canonical_role": "operating_agency",
        }
    )
    profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    selected = selection["selection"]["facts"][0]  # type: ignore[index]
    selected.update(
        {
            "field_name": "delivery_roles",
            "organization_anchors": [
                {"source_block_id": "block-1", "anchor_text": fact["value_raw"]}
            ],
            "role_anchor": {
                "source_block_id": "block-1",
                "anchor_text": "변조된 역할",
            },
            "canonical_role": "operating_agency",
        }
    )
    materialized = selection["materialized_evidence"][0]  # type: ignore[index]
    materialized.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [fact["value_raw"]],
            "organization_sources": [fact["value_source"]],
            "role_raw": fact["value_raw"],
            "role_source_block_id": "block-1",
            "role_source": fact["value_source"],
            "canonical_role": "operating_agency",
        }
    )

    with pytest.raises(
        verifier.GoldVerificationError,
        match="selection/materialized role anchor differs",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_delivery_role_without_explicit_role_or_organization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    fact = profile["comparison_profile"].pop("support_content")[0]  # type: ignore[index,union-attr]
    fact.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [],
            "role_raw": None,
            "role_source_block_id": None,
            "role_source": None,
            "canonical_role": "operating_agency",
        }
    )
    profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    selection["selection"]["facts"][0].update(  # type: ignore[index]
        {
            "field_name": "delivery_roles",
            "organization_anchors": [],
            "role_anchor": None,
            "canonical_role": "operating_agency",
        }
    )
    selection["materialized_evidence"][0].update(  # type: ignore[index]
        {
            "field_name": "delivery_roles",
            "organization_names": [],
            "organization_sources": [],
            "role_raw": None,
            "role_source_block_id": None,
            "role_source": None,
            "canonical_role": "operating_agency",
        }
    )

    with pytest.raises(
        verifier.GoldVerificationError,
        match="delivery role requires at least one explicit organization",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_delivery_role_canonicalization_without_raw_role(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    fact = profile["comparison_profile"].pop("support_content")[0]  # type: ignore[index,union-attr]
    organization = {
        "value_raw": fact["value_raw"],
        "value_source": fact["value_source"],
    }
    fact.update(
        {
            "field_name": "delivery_roles",
            "organization_names": [organization],
            "role_raw": None,
            "role_source_block_id": None,
            "role_source": None,
            "canonical_role": "operating_agency",
        }
    )
    profile["comparison_profile"]["delivery_roles"] = [fact]  # type: ignore[index]
    selection["selection"]["facts"][0].update(  # type: ignore[index]
        {
            "field_name": "delivery_roles",
            "organization_anchors": [
                {"source_block_id": "block-1", "anchor_text": fact["value_raw"]}
            ],
            "role_anchor": None,
            "canonical_role": "operating_agency",
        }
    )
    selection["materialized_evidence"][0].update(  # type: ignore[index]
        {
            "field_name": "delivery_roles",
            "organization_names": [fact["value_raw"]],
            "organization_sources": [fact["value_source"]],
            "role_raw": None,
            "role_source_block_id": None,
            "role_source": None,
            "canonical_role": "operating_agency",
        }
    )

    with pytest.raises(
        verifier.GoldVerificationError,
        match="delivery role requires an explicit role anchor",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_component_name_anchor_text_drift(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_named_component(profile, selection)
    selection["selection"]["support_components"][0]["name_anchor"][  # type: ignore[index]
        "anchor_text"
    ] = "변조된 구성요소명"

    with pytest.raises(
        verifier.GoldVerificationError,
        match="selection/materialized name anchor differs",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_component_name_anchor_outside_component_sources(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_named_component(profile, selection)
    profile["support_components"][0]["source_block_ids"] = ["other-block"]  # type: ignore[index]
    selection["selection"]["support_components"][0]["source_block_ids"] = [  # type: ignore[index]
        "other-block"
    ]
    selection["source_block_texts"]["other-block"] = "다른 구성요소"  # type: ignore[index]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="name anchor is outside its component sources",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_ambiguous_component_name_anchor(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_named_component(profile, selection)
    selection["source_block_texts"]["block-1"] = (  # type: ignore[index]
        "명시적 지원\n내용 / 명시적 지원\n내용"
    )

    with pytest.raises(
        verifier.GoldVerificationError,
        match="name anchor must occur exactly once",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_missing_selection_candidate_pack_id(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    selection["selection"].pop("candidate_pack_id")  # type: ignore[index]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="selection.candidate_pack_id must be a non-empty string",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_rejects_selection_candidate_pack_id_that_differs_from_lineage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    selection["selection"]["candidate_pack_id"] = "forged-pack"  # type: ignore[index]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="selection.candidate_pack_id does not match CandidatePack lineage",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_requires_the_explicit_expected_notice_count(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    _write_gold(root)

    with pytest.raises(verifier.GoldVerificationError, match="expected notice count"):
        verifier.verify_gold_root(root)


def test_accepts_lossless_native_line_final_evidence(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)

    verifier.verify_profile_artifact_triple(profile, selection, common_ir, pblanc_id=pblanc_id)


def test_accepts_lossless_native_line_context_evidence(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    _install_second_native_line_as_context(profile, selection, common_ir)

    verifier.verify_profile_artifact_triple(
        profile, selection, common_ir, pblanc_id=pblanc_id
    )


def test_rejects_matching_tampered_native_context_in_profile_and_materialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    _install_second_native_line_as_context(profile, selection, common_ir)
    profile_context = profile["comparison_profile"]["support_content"][0]["context_evidence"][0]  # type: ignore[index]
    materialized_context = selection["materialized_evidence"][0]["context_blocks"][0]  # type: ignore[index]
    profile_context["native_parent_span"]["end_char"] = 1
    materialized_context["native_parent_span"]["end_char"] = 1

    with pytest.raises(
        verifier.GoldVerificationError,
        match="differs from the regenerated native CandidatePack block",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


@pytest.mark.parametrize("location", ["value", "context"])
def test_rejects_forged_cell_locator_on_non_cell_native_source(
    tmp_path: Path,
    location: str,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    if location == "context":
        _install_second_native_line_as_context(profile, selection, common_ir)
        profile_source = profile["comparison_profile"]["support_content"][0]["context_evidence"][0]  # type: ignore[index]
        materialized_source = selection["materialized_evidence"][0]["context_blocks"][0]  # type: ignore[index]
    else:
        profile_source = profile["comparison_profile"]["support_content"][0]["evidence"][0]  # type: ignore[index]
        materialized_source = selection["materialized_evidence"][0]["source_blocks"][0]  # type: ignore[index]
    profile_source["common_ir_cell_id"] = "forged-cell"
    materialized_source["common_ir_cell_id"] = "forged-cell"

    with pytest.raises(
        verifier.GoldVerificationError,
        match="locator fields differ from the regenerated native CandidatePack block",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


@pytest.mark.parametrize(
    ("target", "mutation", "match"),
    [
        (
            "profile",
            lambda profile, selection: profile["comparison_profile"]["support_content"][0]["evidence"][0].pop("native_parent_span"),  # type: ignore[index]
            "removed native provenance",
        ),
        (
            "both",
            lambda profile, selection: (
                profile["comparison_profile"]["support_content"][0]["evidence"][0]["native_parent_span"].update({"end_char": 1}),  # type: ignore[index]
                selection["materialized_evidence"][0]["source_blocks"][0]["native_parent_span"].update({"end_char": 1}),  # type: ignore[index]
            ),
            "differs from the regenerated native CandidatePack block",
        ),
    ],
)
def test_rejects_tampered_or_missing_native_line_final_evidence(
    tmp_path: Path,
    target: str,
    mutation,
    match: str,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    mutation(profile, selection)

    with pytest.raises(verifier.GoldVerificationError, match=match):
        verifier.verify_profile_artifact_triple(profile, selection, common_ir, pblanc_id=pblanc_id)


def test_rejects_native_line_with_invalid_transform_lineage(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    selection["candidate_pack_lineage"]["candidate_pack_generator_version"] = "1:off"  # type: ignore[index]
    profile["processing_metadata"]["candidate_pack"]["candidate_pack_generator_version"] = "1:off"  # type: ignore[index]

    with pytest.raises(verifier.GoldVerificationError, match="unsupported transform version"):
        verifier.verify_profile_artifact_triple(profile, selection, common_ir, pblanc_id=pblanc_id)


def test_verifier_replays_persisted_v1_native_lineage(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(
        profile,
        selection,
        common_ir,
        producer_version=verifier.LEGACY_NATIVE_EXACT_TRANSFORM_GENERATOR_VERSION,
    )

    verifier.verify_profile_artifact_triple(
        profile, selection, common_ir, pblanc_id=pblanc_id
    )


@pytest.mark.parametrize("mutation", ["invented", "self_parent", "forged_occurrence"])
def test_native_line_replay_rejects_invented_ids_lineage_and_occurrences(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)
    if mutation == "invented":
        selection["source_block_texts"]["line:invented"] = "위조"  # type: ignore[index]
    elif mutation == "self_parent":
        for lineage in (selection["candidate_pack_lineage"], profile["processing_metadata"]["candidate_pack"]):  # type: ignore[index]
            lineage["parent_pack_id"] = lineage["candidate_pack_id"]
    else:
        for evidence in (
            profile["comparison_profile"]["support_content"][0]["evidence"][0],  # type: ignore[index]
            selection["materialized_evidence"][0]["source_blocks"][0],  # type: ignore[index]
        ):
            evidence["common_ir_occurrence_ids"] = ["forged-occurrence"]
            evidence["source_occurrence_ids"] = ["forged-occurrence"]
    with pytest.raises(verifier.GoldVerificationError):
        verifier.verify_profile_artifact_triple(profile, selection, common_ir, pblanc_id=pblanc_id)


def test_rejects_native_source_id_when_provenance_and_parent_lineage_are_stripped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    profile, selection, common_ir = _read_notice_triple(root, pblanc_id)
    _install_native_line_atom(profile, selection, common_ir)

    profile["comparison_profile"]["support_content"][0]["evidence"][0].pop(  # type: ignore[index]
        "native_parent_span"
    )
    selection["materialized_evidence"][0]["source_blocks"][0].pop(  # type: ignore[index]
        "native_parent_span"
    )
    base = verifier._existing_a_base_candidate_pack(
        common_ir, {"block-1"}, label="synthetic stripped native line"
    )
    for lineage in (
        selection["candidate_pack_lineage"],  # type: ignore[index]
        profile["processing_metadata"]["candidate_pack"],  # type: ignore[index]
    ):
        lineage.update({
            "candidate_pack_id": base.pack_id,
            "candidate_pack_generator": base.generator,
            "candidate_pack_generator_version": base.generator_version,
        })
        for key in ("parent_pack_id", "parent_generator", "parent_generator_version"):
            lineage.pop(key)
    selection["selection"]["candidate_pack_id"] = base.pack_id  # type: ignore[index]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="native evidence requires complete CandidatePack parent lineage",
    ):
        verifier.verify_profile_artifact_triple(
            profile, selection, common_ir, pblanc_id=pblanc_id
        )


def test_native_replay_rejects_attachment_only_atomic_source(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    pblanc_id = _write_gold(root)
    _profile, _selection, common_ir = _read_notice_triple(root, pblanc_id)
    common_ir["blocks"][0]["boundary_markers"] = [  # type: ignore[index]
        {"marker": "attachment_bracket", "matched_text": "[서식 1]"}
    ]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="no trusted atomic A-pack source ids",
    ):
        verifier._existing_a_base_candidate_pack(
            common_ir, {"block-1"}, label="synthetic attachment-only native source"
        )


def test_native_replay_rejects_attachment_table_cells() -> None:
    common_ir = _explicit_table_common_ir(1)
    common_ir["blocks"][0]["boundary_markers"] = [  # type: ignore[index]
        {"marker": "attachment_bracket", "matched_text": "[서식 1]"}
    ]

    with pytest.raises(
        verifier.GoldVerificationError,
        match="no trusted atomic A-pack source ids",
    ):
        verifier._existing_a_base_candidate_pack(
            common_ir,
            {"table-1#r0c0p0"},
            label="synthetic attachment-table native source",
        )


def _explicit_table_common_ir(cell_count: int) -> dict[str, object]:
    occurrences = [
        {"occurrence_id": f"occ-{index}", "text": f"셀 {index}"}
        for index in range(cell_count)
    ]
    cells = [
        {
            "cell_id": f"cell-{index}",
            "row_index": index,
            "col_index": 0,
            "row_span": 1,
            "col_span": 1,
            "text_occurrence_ids": [f"occ-{index}"],
        }
        for index in range(cell_count)
    ]
    return {
        "schema_version": verifier.COMMON_IR_SCHEMA,
        "document": {
            "document_id": "pdf:PBLN_TABLE_REPLAY",
            "source_kind": "pdf",
            "provenance": {"source_sha256": "b" * 64},
        },
        "blocks": [{
            "block_id": "table-1",
            "text": "\n".join(row["text"] for row in occurrences),
            "kind": "table",
            "reading_order": 0,
            "structure_status": "explicit",
            "text_occurrence_ids": [row["occurrence_id"] for row in occurrences],
            "occurrences": occurrences,
            "cells": cells,
        }],
    }


def test_native_replay_rejects_partial_table_cell_group() -> None:
    common_ir = _explicit_table_common_ir(2)

    with pytest.raises(
        verifier.GoldVerificationError,
        match="partial production A-table cell group",
    ):
        verifier._existing_a_base_candidate_pack(
            common_ir,
            {"table-1#r0c0p0"},
            label="synthetic partial table replay",
        )


def test_native_replay_rejects_table_cell_total_over_production_cap() -> None:
    cell_count = verifier.MAX_A_TABLE_CELL_CANDIDATES + 1
    common_ir = _explicit_table_common_ir(cell_count)
    source_ids = {
        f"table-1#r{index}c0p0" for index in range(cell_count)
    }

    with pytest.raises(
        verifier.GoldVerificationError,
        match="exceeds the production A-table cell cap",
    ):
        verifier._existing_a_base_candidate_pack(
            common_ir,
            source_ids,
            label="synthetic oversized table replay",
        )


def test_rejects_an_oversized_json_before_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    monkeypatch.setattr(verifier, "MAX_GOLD_FILE_BYTES", 1024 * 1024)
    (root / "freeze_manifest.json").write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

    with pytest.raises(verifier.GoldVerificationError, match="per-file byte budget"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_rejects_an_oversized_jsonl_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    monkeypatch.setattr(verifier, "MAX_GOLD_FILE_BYTES", 1024 * 1024)
    (root / "governance" / "decisions.jsonl").write_bytes(
        b'{"notice_id":"PBLN_SYNTHETIC_001"}\n' + b"x" * (1024 * 1024)
    )

    with pytest.raises(verifier.GoldVerificationError, match="per-file byte budget"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_rejects_an_oversized_csv_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    monkeypatch.setattr(verifier, "MAX_GOLD_FILE_BYTES", 1024 * 1024)
    (root / "competitor_mapping.csv").write_bytes(
        b"answer_pblanc_id,competitor_pblanc_id\n" + b"x" * (1024 * 1024)
    )

    with pytest.raises(verifier.GoldVerificationError, match="per-file byte budget"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_unindexed_readme_still_obeys_the_corpus_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    monkeypatch.setattr(verifier, "MAX_GOLD_FILE_BYTES", 1024 * 1024)
    (root / "README.md").write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(verifier.GoldVerificationError, match="per-file byte budget"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_aggregate_budget_counts_each_file_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    # Verification first reads the freeze manifest and its artifact-index and
    # profile-manifest pins.  Permit precisely those three unique files; the
    # subsequent sample CSV must trigger the aggregate guard.
    permitted = sum(
        (root / filename).stat().st_size
        for filename in ("freeze_manifest.json", "artifact_index.json", "profile_manifest.json")
    )
    monkeypatch.setattr(verifier, "MAX_GOLD_TOTAL_UNIQUE_FILE_BYTES", permitted)

    with pytest.raises(verifier.GoldVerificationError, match="aggregate unique-file byte budget"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_deep_json_is_a_validation_error_without_a_recursion_traceback(tmp_path: Path) -> None:
    root = tmp_path / "gold"
    _write_gold(root)
    depth = 10_000
    (root / "freeze_manifest.json").write_bytes(b'{"nested":' * depth + b"0" + b"}" * depth)

    with pytest.raises(verifier.GoldVerificationError, match="freeze_manifest.json is not valid UTF-8 JSON|nesting depth"):
        verifier.verify_gold_root(root, expected_notice_count=1)


def test_read_budget_binds_a_path_to_the_first_verified_inode(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"version": 1}', encoding="utf-8")
    budget = verifier._GoldReadBudget()
    verifier._load_json(target, "artifact", budget=budget)
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"version": 2}', encoding="utf-8")
    replacement.replace(target)

    with pytest.raises(verifier.GoldVerificationError, match="changed while"):
        verifier._load_json(target, "artifact", budget=budget)


def test_read_budget_detects_in_place_rewrite_when_mtime_is_restored(tmp_path: Path) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"version": 1}', encoding="utf-8")
    budget = verifier._GoldReadBudget()
    assert verifier._load_json(target, "artifact", budget=budget) == {"version": 1}

    before = target.stat()
    # Keep the inode and byte size stable, then restore mtime.  A verifier that
    # snapshots only (inode, size, mtime) would accept this replacement.
    target.write_text('{"version": 2}', encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    with pytest.raises(verifier.GoldVerificationError, match="changed while"):
        verifier._load_json(target, "artifact", budget=budget)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_checked_path_replaced_by_fifo_cannot_block_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"version": 1}', encoding="utf-8")
    budget = verifier._GoldReadBudget()
    real_open = verifier.os.open
    swapped = False

    def replace_then_open(path, flags):
        nonlocal swapped
        if not swapped:
            swapped = True
            target.unlink()
            os.mkfifo(target)
        return real_open(path, flags)

    monkeypatch.setattr(verifier.os, "open", replace_then_open)

    with pytest.raises(verifier.GoldVerificationError, match="changed while"):
        verifier._load_json(target, "artifact", budget=budget)
