"""Offline canary tests for a frozen Existing Composite corpus."""

from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_existing_composite_shadow.py"
SPEC = importlib.util.spec_from_file_location("evaluate_existing_composite_shadow", SCRIPT)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


def _table_document(notice_id: str) -> dict:
    texts = ["구분", "지원 내용", "청년기업", "컨설팅"]
    occurrences = [
        {"occurrence_id": f"hwpx:t0:occ:{index}", "text": text}
        for index, text in enumerate(texts)
    ]
    flattened = "\n".join(texts)
    return {
        "schema_version": "common_ir_v1",
        "document": {
            "document_id": f"hwpx:{notice_id}",
            "source_kind": "hwpx",
            "provenance": {"source_sha256": "a" * 64},
        },
        "blocks": [
            {
                "block_id": "hwpx:t0",
                "kind": "table",
                "structure_status": "explicit",
                "text": flattened,
                "text_occurrence_ids": ["hwpx:t0:occ:table"],
                "reading_order": 0,
                "page": None,
                "section_path": "",
                "occurrences": [
                    {"occurrence_id": "hwpx:t0:occ:table", "text": flattened},
                    *occurrences,
                ],
                "cells": [
                    {
                        "cell_id": f"hwpx:t0:c:{index}",
                        "row_index": index // 2,
                        "col_index": index % 2,
                        "row_span": 1,
                        "col_span": 1,
                        "text_occurrence_ids": [f"hwpx:t0:occ:{index}"],
                    }
                    for index in range(4)
                ],
                "boundary_markers": [],
                "provenance": {},
            }
        ],
        "relations": [],
        "conflicts": [],
    }


def _gold_root(tmp_path: Path, *, relative_path: str = "notices/PBLN_1/common_ir_v1.json") -> Path:
    root = tmp_path / "gold"
    common_ir_path = root / relative_path
    common_ir_path.parent.mkdir(parents=True)
    common_ir_path.write_text(
        json.dumps(_table_document("PBLN_1"), ensure_ascii=False), encoding="utf-8"
    )
    common_ir_sha256 = sha256(common_ir_path.read_bytes()).hexdigest()
    profile_manifest_path = root / "profile_manifest.json"
    profile_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "test",
                "profiles": [
                    {
                        "pblanc_id": "PBLN_1",
                        "frozen": {
                            "common_ir": {
                                "path": relative_path,
                                "sha256": common_ir_sha256,
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    profile_manifest_sha256 = sha256(profile_manifest_path.read_bytes()).hexdigest()
    (root / "freeze_manifest.json").write_text(
        json.dumps(
            {
                "dataset_version": "synthetic-gold-v1",
                "notice_count": 1,
                "profile_manifest_sha256": profile_manifest_sha256,
            }
        ),
        encoding="utf-8",
    )
    return root


def _refresh_manifest_hashes(root: Path) -> None:
    common_ir_path = root / "notices" / "PBLN_1" / "common_ir_v1.json"
    profile_manifest_path = root / "profile_manifest.json"
    profile_manifest = json.loads(profile_manifest_path.read_text(encoding="utf-8"))
    profile_manifest["profiles"][0]["frozen"]["common_ir"]["sha256"] = sha256(
        common_ir_path.read_bytes()
    ).hexdigest()
    profile_manifest_path.write_text(json.dumps(profile_manifest), encoding="utf-8")
    freeze_manifest_path = root / "freeze_manifest.json"
    freeze_manifest = json.loads(freeze_manifest_path.read_text(encoding="utf-8"))
    freeze_manifest["profile_manifest_sha256"] = sha256(
        profile_manifest_path.read_bytes()
    ).hexdigest()
    freeze_manifest_path.write_text(json.dumps(freeze_manifest), encoding="utf-8")


def test_evaluator_reports_aggregate_only_native_structural_canary(tmp_path: Path) -> None:
    root = _gold_root(tmp_path)
    report = evaluator.evaluate_gold_corpus(
        root, expected_notice_count=1
    )

    assert report["status"] == "valid"
    assert report["dataset_version"] == "synthetic-gold-v1"
    assert report["notice_count"] == 1
    assert report["candidate_kind_counts"] == {"table_axis_context": 1}
    assert report["invariants"] == {
        "cross_common_ir_block_candidate_count": 0,
        "fatal_diagnostic_notice_count": 0,
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert "청년기업" not in serialized
    assert "컨설팅" not in serialized
    assert "candidate_id" not in serialized
    assert "notices" not in report

    detailed = evaluator.evaluate_gold_corpus(
        root, expected_notice_count=1, include_notices=True
    )
    assert detailed["notices"][0]["notice_id"] == "PBLN_1"


def test_evaluator_rejects_manifest_path_escape(tmp_path: Path) -> None:
    root = _gold_root(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = json.loads((root / "profile_manifest.json").read_text(encoding="utf-8"))
    manifest["profiles"][0]["frozen"]["common_ir"]["path"] = "../../outside.json"
    (root / "profile_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    freeze_manifest_path = root / "freeze_manifest.json"
    freeze_manifest = json.loads(freeze_manifest_path.read_text(encoding="utf-8"))
    freeze_manifest["profile_manifest_sha256"] = sha256(
        (root / "profile_manifest.json").read_bytes()
    ).hexdigest()
    freeze_manifest_path.write_text(json.dumps(freeze_manifest), encoding="utf-8")

    with pytest.raises(
        evaluator.CompositeShadowEvaluationError,
        match="escapes the Gold root",
    ):
        evaluator.evaluate_gold_corpus(root)


def test_evaluator_rejects_missing_requested_notice(tmp_path: Path) -> None:
    with pytest.raises(
        evaluator.CompositeShadowEvaluationError,
        match="requested notices are absent",
    ):
        evaluator.evaluate_gold_corpus(
            _gold_root(tmp_path), notice_ids={"PBLN_missing"}
        )


def test_evaluator_marks_fatal_generator_rejection_invalid(tmp_path: Path) -> None:
    root = _gold_root(tmp_path)
    common_ir_path = root / "notices" / "PBLN_1" / "common_ir_v1.json"
    document = json.loads(common_ir_path.read_text(encoding="utf-8"))
    document["document"]["provenance"]["source_sha256"] = "invalid"
    common_ir_path.write_text(json.dumps(document), encoding="utf-8")
    _refresh_manifest_hashes(root)

    report = evaluator.evaluate_gold_corpus(root)

    assert report["status"] == "invalid"
    assert report["candidate_count"] == 0
    assert report["invariants"]["fatal_diagnostic_notice_count"] == 1
    detailed = evaluator.evaluate_gold_corpus(root, include_notices=True)
    assert detailed["notices"][0]["fatal_diagnostic_codes"] == [
        "COMMON_IR_V1_REQUIRED"
    ]


def test_evaluator_rejects_common_ir_changed_after_freeze(tmp_path: Path) -> None:
    root = _gold_root(tmp_path)
    common_ir_path = root / "notices" / "PBLN_1" / "common_ir_v1.json"
    document = json.loads(common_ir_path.read_text(encoding="utf-8"))
    document["relations"].append({"kind": "tampered"})
    common_ir_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        evaluator.CompositeShadowEvaluationError,
        match="Common IR SHA-256 mismatch",
    ):
        evaluator.evaluate_gold_corpus(root)


@pytest.mark.parametrize("missing", ["profile_manifest", "common_ir"])
def test_evaluator_requires_frozen_artifact_checksums(
    tmp_path: Path, missing: str
) -> None:
    root = _gold_root(tmp_path)
    if missing == "profile_manifest":
        freeze_path = root / "freeze_manifest.json"
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        del freeze["profile_manifest_sha256"]
        freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
        expected = "profile manifest manifest SHA-256 is invalid"
    else:
        profile_path = root / "profile_manifest.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        del profile["profiles"][0]["frozen"]["common_ir"]["sha256"]
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        freeze_path = root / "freeze_manifest.json"
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
        freeze["profile_manifest_sha256"] = sha256(profile_path.read_bytes()).hexdigest()
        freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
        expected = "Common IR manifest SHA-256 is invalid"

    with pytest.raises(evaluator.CompositeShadowEvaluationError, match=expected):
        evaluator.evaluate_gold_corpus(root)


def test_json_read_cap_is_checked_before_loading_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "oversized.json"
    artifact.write_text("{}", encoding="utf-8")
    original_stat = Path.stat
    read_attempted = False

    def reported_size(path: Path, *args: object, **kwargs: object):
        if path == artifact:
            return SimpleNamespace(st_size=evaluator.MAX_JSON_ARTIFACT_BYTES + 1)
        return original_stat(path, *args, **kwargs)

    def reject_read(path: Path) -> bytes:
        nonlocal read_attempted
        read_attempted = True
        raise AssertionError(f"oversized artifact was read: {path}")

    monkeypatch.setattr(Path, "stat", reported_size)
    monkeypatch.setattr(Path, "read_bytes", reject_read)

    with pytest.raises(
        evaluator.CompositeShadowEvaluationError,
        match="exceeds the read cap",
    ):
        evaluator._load_object(artifact, label="test artifact")
    assert not read_attempted
