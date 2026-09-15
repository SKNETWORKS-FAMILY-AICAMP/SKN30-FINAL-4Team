from __future__ import annotations

import csv
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from zipfile import ZIP_BZIP2, ZipFile, ZipInfo

import pytest

from scripts import compare_existing_profile_candidates as cli
from scripts import verify_existing_gold100 as gold_verifier
from worker.evaluation import existing_profile_diff as comparison


REAL_BASELINE = Path("/srv/pre-review/imports/bizinfo-existing/structured-profiles-100.zip")
REAL_GOLD = Path(
    "/home/paim/Project/llm-prompting-test/imports/prereview-vectordb-poc/"
    "dataset_prep/frozen_existing_profile_gold_100_20260909_v5"
)
REAL_CHANGED_IDS = {
    "PBLN_000000000103645",
    "PBLN_000000000112425",
    "PBLN_000000000117175",
    "PBLN_000000000121019",
    "PBLN_000000000121309",
    "PBLN_000000000122023",
}
REAL_BASELINE_SHA256 = "6649f1a5aab36f659d688634103950d3b73f8a5903a453aabdbbd9f5bc0f7f0d"
REAL_GOLD_MANIFEST_SHA256 = "a2c35fb4c98c92c23ff34faa045a16ec4e8ed1ea4bae5397caab547cb3db6987"


def _profile(notice_id: str, *, value: str = "원문", nested_notice_id: str | None = None) -> dict[str, object]:
    profile: dict[str, object] = {
        "schema_version": "existing_program_profile/v0.2",
        "notice_id": notice_id,
        "comparison_profile": {
            "support_content": [
                {
                    "fact_id": "fact-1",
                    "field_name": "support_content",
                    "value_raw": value,
                }
            ]
        },
        "support_components": [],
    }
    if nested_notice_id is not None:
        profile["processing_metadata"] = {"notice_id": nested_notice_id}
    return profile


def _write_baseline(path: Path, profiles: dict[str, dict[str, object]]) -> None:
    with ZipFile(path, "w") as archive:
        for notice_id, profile in profiles.items():
            archive.writestr(
                f"{notice_id}/pipeline/structured_profile.v0.2.json",
                json.dumps(profile, ensure_ascii=False),
            )


def _write_boundary_gold(root: Path, profiles: dict[str, dict[str, object]]) -> None:
    for notice_id, profile in profiles.items():
        path = root / "notices" / notice_id / "existing_profile.v0.2.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    (root / "freeze_manifest.json").write_text(
        json.dumps(
            {
                "freeze_status": "FROZEN",
                "dataset_version": "synthetic-reviewed-v5",
                "notice_count": len(profiles),
            }
        ),
        encoding="utf-8",
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _seal_verified_gold(root: Path, notice_id: str) -> None:
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {"freeze_manifest.json", "artifact_index.json", "README.md"}
    )
    artifact_rows = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        artifact_rows.append(
            {
                "notice_id": notice_id if relative.startswith(f"notices/{notice_id}/") else None,
                "artifact": (
                    path.stem
                    if path.parent.name != "governance"
                    else f"governance:{path.name}"
                ),
                "path": relative,
                "sha256": _digest(path),
                "bytes": path.stat().st_size,
            }
        )
    _write_json(
        root / "artifact_index.json",
        {
            "schema_version": gold_verifier.ARTIFACT_INDEX_SCHEMA,
            "artifacts": artifact_rows,
        },
    )
    _write_json(
        root / "freeze_manifest.json",
        {
            "contract": gold_verifier.FREEZE_CONTRACT,
            "freeze_status": "FROZEN",
            "dataset_version": "synthetic-reviewed-existing-profile-v5",
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
                "notice_ids": [notice_id],
                "decision_ledger_sha256": _digest(root / "governance" / "decisions.jsonl"),
                "semantic_regression_report_sha256": _digest(
                    root / "governance" / "semantic_regression_report.json"
                ),
            },
            "artifact_index_sha256": _digest(root / "artifact_index.json"),
            "profile_manifest_sha256": _digest(root / "profile_manifest.json"),
            "sample_100_sha256": _digest(root / "sample_100.csv"),
        },
    )


def _write_verified_gold(root: Path) -> str:
    """Build one strict 15-digit, fully sealed fixture accepted by the real gate."""

    notice_id = "PBLN_000000000000001"
    document_id = f"pdf:{notice_id}"
    source_sha = "a" * 64
    source_text = "명시적 지원 내용"
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
            "text_basis": gold_verifier.TEXT_BASIS,
        },
        "evidence": [
            {
                "source_block_id": "block-1",
                "common_ir_document_id": document_id,
                "common_ir_block_id": "block-1",
                "common_ir_occurrence_ids": ["occ-1"],
            }
        ],
        "context_evidence": [],
    }
    profile = {
        "schema_version": gold_verifier.PROFILE_SCHEMA,
        "notice_id": f"bizinfo:{notice_id}",
        "source_profile_id": document_id,
        "source_documents": [
            {
                "common_ir": {
                    "document_id": document_id,
                    "schema_version": gold_verifier.COMMON_IR_SCHEMA,
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
                "text_basis": gold_verifier.TEXT_BASIS,
            }
        },
    }
    selection = {
        "selection_contract": gold_verifier.SELECTION_CONTRACT,
        "selection": {"notice_id": notice_id},
        "source_block_texts": {"block-1": source_text},
        "common_ir_identity": {
            "document_id": document_id,
            "source_kind": "pdf",
            "source_sha256": source_sha,
        },
        "candidate_pack_lineage": {
            "candidate_pack_id": "synthetic-pack",
            "common_ir_document_id": document_id,
            "common_ir_source_sha256": source_sha,
            "text_basis": gold_verifier.TEXT_BASIS,
        },
        "materialized_evidence": [
            {
                "fact_id": fact["fact_id"],
                "field_name": fact["field_name"],
                "status": fact["status"],
                "value_source": fact["value_source"],
            }
        ],
        "materialized_components": [],
    }
    common_ir = {
        "schema_version": gold_verifier.COMMON_IR_SCHEMA,
        "document": {
            "document_id": document_id,
            "source_kind": "pdf",
            "provenance": {
                "source_sha256": source_sha,
                "source_location": "external/source.pdf",
            },
        },
        "blocks": [
            {
                "block_id": "block-1",
                "text": source_text,
                "text_occurrence_ids": ["occ-1"],
            }
        ],
    }
    notice = root / "notices" / notice_id
    _write_json(notice / "existing_profile.v0.2.json", profile)
    _write_json(notice / "source_selection.v0.2.json", selection)
    _write_json(notice / "common_ir_v1.json", common_ir)

    profile_manifest = {
        "schema_version": gold_verifier.PROFILE_MANIFEST_SCHEMA,
        "profiles": [
            {
                "pblanc_id": notice_id,
                "notice_id": f"bizinfo:{notice_id}",
                "role": "answer",
                "title": "Synthetic Gold Notice",
                "support_field": "test",
                "source_kind": "synthetic",
                "source": {
                    kind: {
                        "path": f"external/{name}",
                        "sha256": _digest(notice / name),
                    }
                    for kind, name in gold_verifier._NOTICE_ARTIFACTS.items()
                },
                "frozen": {
                    kind: {
                        "path": f"notices/{notice_id}/{name}",
                        "sha256": _digest(notice / name),
                    }
                    for kind, name in gold_verifier._NOTICE_ARTIFACTS.items()
                },
            }
        ],
    }
    _write_json(root / "profile_manifest.json", profile_manifest)
    _write_csv(
        root / "sample_100.csv",
        ["pblanc_id", "notice_id", "role", "title", "support_field"],
        [
            {
                "pblanc_id": notice_id,
                "notice_id": f"bizinfo:{notice_id}",
                "role": "answer",
                "title": "Synthetic Gold Notice",
                "support_field": "test",
            }
        ],
    )
    _write_json(
        root / "answer_set.json",
        {
            "schema_version": "answer_set.v1",
            "answers": [
                {
                    "pblanc_id": notice_id,
                    "title": "Synthetic Gold Notice",
                    "support_field": "test",
                }
            ],
        },
    )
    _write_csv(root / "competitor_mapping.csv", ["answer_pblanc_id", "competitor_pblanc_id"], [])
    _write_csv(root / "reserve_pool.csv", ["pblanc_id"], [])
    _write_csv(root / "replacement_log.csv", ["replacement_stage"], [])

    governance = root / "governance"
    for name in (
        "materialization_report.json",
        "semantic_regression_report.json",
        "v4_materialization_report.json",
        "v4_semantic_regression_report.json",
    ):
        _write_json(
            governance / name,
            {
                "status": "passed",
                "notice_count": 1,
                "reports": [{"notice_id": notice_id, "status": "passed"}],
            },
        )
    (governance / "decisions.jsonl").write_text(
        json.dumps({"notice_id": notice_id}) + "\n",
        encoding="utf-8",
    )
    (governance / "v4_decisions.jsonl").write_text(
        json.dumps({"notice_id": notice_id}) + "\n",
        encoding="utf-8",
    )
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
    _seal_verified_gold(root, notice_id)
    return notice_id


def _loaded(
    profiles: dict[str, dict[str, object]],
    *,
    role: str,
) -> comparison.LoadedProfiles:
    return comparison.LoadedProfiles(
        profiles=profiles,
        identity={"role": role, "profile_count": len(profiles)},
    )


def _verified_fixture_profile(root: Path) -> tuple[str, dict[str, object]]:
    notice_id = _write_verified_gold(root)
    profile_path = root / "notices" / notice_id / comparison.GOLD_PROFILE_NAME
    return notice_id, json.loads(profile_path.read_text(encoding="utf-8"))


def _strict_profile(notice_id: str) -> dict[str, object]:
    return {
        "schema_version": "existing_program_profile/v0.2",
        "notice_id": notice_id,
        "comparison_profile": {
            "support_content": [
                {
                    "fact_id": "fact-1",
                    "field_name": "support_content",
                    "value_raw": "지원 내용 A",
                    "value_source": {
                        "source_block_id": "block-1",
                        "start_char": 0,
                        "end_char": 7,
                    },
                    "evidence": [
                        {
                            "common_ir_occurrence_ids": ["occ-1"],
                            "source_block_id": "block-1",
                        }
                    ],
                },
                {
                    "fact_id": "fact-2",
                    "field_name": "support_content",
                    "value_raw": "지원 내용 B",
                    "value_source": {
                        "source_block_id": "block-1",
                        "start_char": 8,
                        "end_char": 15,
                    },
                    "evidence": [
                        {
                            "common_ir_occurrence_ids": ["occ-2"],
                            "source_block_id": "block-1",
                        }
                    ],
                },
            ]
        },
        "support_components": [],
        "processing_metadata": {"attempt": 1},
    }


def test_only_root_notice_namespace_and_object_key_order_are_normalized() -> None:
    first = "PBLN_000000000000001"
    second = "PBLN_000000000000002"
    third = "PBLN_000000000000003"
    baseline_profiles = {
        first: _profile(first),
        second: _profile(second, value="자동 결과"),
        third: _profile(third, nested_notice_id=third),
    }
    gold_profiles = {
        first: dict(reversed(list(_profile(f"bizinfo:{first}").items()))),
        second: _profile(f"bizinfo:{second}", value="사람이 교정한 결과"),
        third: _profile(f"bizinfo:{third}", nested_notice_id=f"bizinfo:{third}"),
    }
    report = comparison.compare_loaded_profiles(
        _loaded(baseline_profiles, role="unreviewed_automatic_baseline"),
        _loaded(gold_profiles, role="human_reviewed_corrected_gold"),
    )

    assert report["counts"] == {
        "baseline": 3,
        "gold": 3,
        "shared": 3,
        "unchanged": 1,
        "changed": 2,
        "baseline_only": 0,
        "gold_only": 0,
    }
    assert report["unchanged_notice_ids"] == [first]
    changed = {row["notice_id"]: row for row in report["changed"]}
    assert changed[second]["changed_top_level_fields"] == ["comparison_profile"]
    assert changed[third]["changed_top_level_fields"] == ["processing_metadata"]
    assert report["normalization"]["fuzzy_matching"] == "forbidden"


@pytest.mark.parametrize(
    "mutation",
    ["value_raw_whitespace", "array_order", "fact_id", "evidence_id", "span", "numeric_type"],
)
def test_every_non_namespace_semantic_or_provenance_change_remains_changed(
    mutation: str,
) -> None:
    notice_id = "PBLN_000000000000001"
    baseline_profile = _strict_profile(notice_id)
    gold_profile = deepcopy(baseline_profile)
    gold_profile["notice_id"] = f"bizinfo:{notice_id}"
    facts = gold_profile["comparison_profile"]["support_content"]  # type: ignore[index]
    if mutation == "value_raw_whitespace":
        facts[0]["value_raw"] = "지원 내용 A "
    elif mutation == "array_order":
        facts.reverse()
    elif mutation == "fact_id":
        facts[0]["fact_id"] = "fact-changed"
    elif mutation == "evidence_id":
        facts[0]["evidence"][0]["common_ir_occurrence_ids"] = ["occ-changed"]
    elif mutation == "span":
        facts[0]["value_source"]["end_char"] = 8
    else:
        gold_profile["processing_metadata"]["attempt"] = 1.0  # type: ignore[index]

    report = comparison.compare_loaded_profiles(
        _loaded({notice_id: baseline_profile}, role="automatic"),
        _loaded({notice_id: gold_profile}, role="gold"),
    )

    assert report["counts"]["unchanged"] == 0
    assert report["counts"]["changed"] == 1


def test_distinct_high_precision_json_number_tokens_remain_changed() -> None:
    notice_id = "PBLN_000000000000001"
    baseline_profile = comparison._json_object(
        (
            '{"schema_version":"existing_program_profile/v0.2",'
            f'"notice_id":"{notice_id}","score":0.10000000000000001}}'
        ).encode(),
        label="baseline",
    )
    gold_profile = comparison._json_object(
        (
            '{"schema_version":"existing_program_profile/v0.2",'
            f'"notice_id":"bizinfo:{notice_id}","score":0.1}}'
        ).encode(),
        label="Gold",
    )

    report = comparison.compare_loaded_profiles(
        _loaded({notice_id: baseline_profile}, role="automatic"),
        _loaded({notice_id: gold_profile}, role="gold"),
    )

    assert report["counts"]["unchanged"] == 0
    assert report["counts"]["changed"] == 1


def test_excessive_json_nesting_is_reported_as_a_comparison_error() -> None:
    raw = b'{"nested":' + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}"

    with pytest.raises(comparison.ExistingProfileComparisonError, match="cannot read UTF-8 JSON"):
        comparison._json_object(raw, label="deep profile")


def test_comparison_report_is_deterministic_across_mapping_insertion_order() -> None:
    first = "PBLN_000000000000001"
    second = "PBLN_000000000000002"
    baseline_profiles = {
        second: _strict_profile(second),
        first: _strict_profile(first),
    }
    gold_profiles = {
        first: _strict_profile(f"bizinfo:{first}"),
        second: _strict_profile(f"bizinfo:{second}"),
    }
    gold_profiles[second]["processing_metadata"]["attempt"] = 2  # type: ignore[index]

    first_report = comparison.compare_loaded_profiles(
        _loaded(baseline_profiles, role="automatic"),
        _loaded(gold_profiles, role="gold"),
    )
    second_report = comparison.compare_loaded_profiles(
        _loaded(dict(reversed(list(baseline_profiles.items()))), role="automatic"),
        _loaded(dict(reversed(list(gold_profiles.items()))), role="gold"),
    )

    assert first_report == second_report
    assert json.dumps(first_report, ensure_ascii=False, sort_keys=True) == json.dumps(
        second_report,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_cli_writes_only_one_exclusive_report_inside_explicit_output_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "baseline.zip"
    gold = tmp_path / "gold"
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    notice_id, gold_profile = _verified_fixture_profile(gold)
    baseline_profile = deepcopy(gold_profile)
    baseline_profile["notice_id"] = notice_id
    _write_baseline(baseline, {notice_id: baseline_profile})
    baseline_before = baseline.read_bytes()
    gold_before = (gold / "notices" / notice_id / comparison.GOLD_PROFILE_NAME).read_bytes()

    result = cli.main(
        [
            "--baseline-zip",
            str(baseline),
            "--gold-root",
            str(gold),
            "--output-dir",
            str(output),
            "--expected-profile-count",
            "1",
            "--expected-shared",
            "1",
            "--expected-unchanged",
            "1",
            "--expected-changed",
            "0",
            "--expected-baseline-sha256",
            comparison.load_automatic_baseline(
                baseline,
                expected_profile_count=1,
            ).identity["archive_sha256"],
            "--expected-gold-freeze-manifest-sha256",
            comparison.load_reviewed_gold(
                gold,
                expected_profile_count=1,
            ).identity["freeze_manifest_sha256"],
        ]
    )

    assert result == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout == {
        "status": "compared",
        "report_file": comparison.REPORT_FILE_NAME,
        "shared": 1,
        "unchanged": 1,
        "changed": 0,
        "baseline_only": 0,
        "gold_only": 0,
    }
    assert sorted(path.name for path in output.iterdir()) == [
        "existing-profile-comparison.v1.json",
        "keep.txt",
    ]
    assert stat.S_IMODE((output / comparison.REPORT_FILE_NAME).stat().st_mode) == 0o600
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert baseline.read_bytes() == baseline_before
    assert (gold / "notices" / notice_id / comparison.GOLD_PROFILE_NAME).read_bytes() == gold_before
    assert cli.main(
        [
            "--baseline-zip",
            str(baseline),
            "--gold-root",
            str(gold),
            "--output-dir",
            str(output),
            "--expected-profile-count",
            "1",
        ]
    ) == 1


def test_expectation_failure_does_not_write_report(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.zip"
    gold = tmp_path / "gold"
    output = tmp_path / "output"
    output.mkdir()
    notice_id, gold_profile = _verified_fixture_profile(gold)
    baseline_profile = deepcopy(gold_profile)
    baseline_profile["notice_id"] = notice_id
    _write_baseline(baseline, {notice_id: baseline_profile})

    assert cli.main(
        [
            "--baseline-zip",
            str(baseline),
            "--gold-root",
            str(gold),
            "--output-dir",
            str(output),
            "--expected-profile-count",
            "1",
            "--expected-changed",
            "1",
        ]
    ) == 1
    assert list(output.iterdir()) == []


def test_baseline_zip_rejects_path_traversal_even_without_extraction(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.zip"
    with ZipFile(baseline, "w") as archive:
        archive.writestr("../outside.json", "{}")

    with pytest.raises(comparison.ExistingProfileComparisonError, match="unsafe ZIP member path"):
        comparison.load_automatic_baseline(baseline)


def test_baseline_zip_rejects_symlink_members(tmp_path: Path) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    info = ZipInfo(f"{notice_id}/pipeline/structured_profile.v0.2.json")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(baseline, "w") as archive:
        archive.writestr(info, "target.json")

    with pytest.raises(comparison.ExistingProfileComparisonError, match="symlink ZIP member"):
        comparison.load_automatic_baseline(baseline)


def test_baseline_uses_one_open_file_identity_for_hash_and_profile_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    _write_baseline(baseline, {notice_id: _profile(notice_id)})
    original_open = os.open
    matching_opens = 0
    baseline_open_flags = 0

    def counting_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal baseline_open_flags, matching_opens
        if Path(path) == baseline:  # type: ignore[arg-type]
            matching_opens += 1
            baseline_open_flags = int(args[0])
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comparison.os, "open", counting_open)

    loaded = comparison.load_automatic_baseline(baseline, expected_profile_count=1)

    assert len(loaded.profiles) == 1
    assert matching_opens == 1
    assert baseline_open_flags & os.O_NONBLOCK


def test_baseline_enforces_exact_profile_count_and_physical_size_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    _write_baseline(baseline, {notice_id: _profile(notice_id)})

    with pytest.raises(comparison.ExistingProfileComparisonError, match="profile count"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=2)

    monkeypatch.setattr(comparison, "MAX_ARCHIVE_BYTES", baseline.stat().st_size - 1)
    with pytest.raises(comparison.ExistingProfileComparisonError, match="physical size cap"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=1)


def test_baseline_enforces_declared_and_retained_profile_byte_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    _write_baseline(baseline, {notice_id: _profile(notice_id)})
    with ZipFile(baseline) as archive:
        declared = archive.getinfo(
            f"{notice_id}/pipeline/structured_profile.v0.2.json"
        ).file_size

    monkeypatch.setattr(comparison, "MAX_TOTAL_PROFILE_BYTES", declared - 1)
    with pytest.raises(comparison.ExistingProfileComparisonError, match="declared Profile bytes"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=1)

    monkeypatch.setattr(comparison, "MAX_TOTAL_PROFILE_BYTES", declared)
    original_reader = comparison._read_zip_profile

    def oversized_retained(archive: ZipFile, info: ZipInfo) -> bytes:
        return original_reader(archive, info) + b" "

    monkeypatch.setattr(comparison, "_read_zip_profile", oversized_retained)
    with pytest.raises(comparison.ExistingProfileComparisonError, match="retained Profile bytes"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=1)


def test_baseline_rejects_unsupported_compression(tmp_path: Path) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    with ZipFile(baseline, "w", compression=ZIP_BZIP2) as archive:
        archive.writestr(
            f"{notice_id}/pipeline/structured_profile.v0.2.json",
            json.dumps(_profile(notice_id), ensure_ascii=False),
        )

    with pytest.raises(comparison.ExistingProfileComparisonError, match="unsupported ZIP compression"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=1)


def test_baseline_rejects_duplicate_local_header_offsets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_id = "PBLN_000000000000001"
    baseline = tmp_path / "baseline.zip"
    _write_baseline(baseline, {notice_id: _profile(notice_id)})
    original_infolist = comparison.ZipFile.infolist

    def duplicated_infolist(archive: ZipFile) -> list[ZipInfo]:
        infos = original_infolist(archive)
        duplicate = deepcopy(infos[0])
        duplicate.filename = f"{notice_id}/metadata-copy.json"
        duplicate.orig_filename = duplicate.filename
        return [infos[0], duplicate]

    monkeypatch.setattr(comparison.ZipFile, "infolist", duplicated_infolist)

    with pytest.raises(comparison.ExistingProfileComparisonError, match="duplicate ZIP local-header offset"):
        comparison.load_automatic_baseline(baseline, expected_profile_count=1)


def test_gold_tree_rejects_symlinks(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    notice_id, _ = _verified_fixture_profile(gold)
    target = gold / "notices" / notice_id / comparison.GOLD_PROFILE_NAME
    link = gold / "untrusted-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on test filesystem: {error}")

    with pytest.raises(comparison.ExistingProfileComparisonError, match="symlink is forbidden"):
        comparison.load_reviewed_gold(gold, expected_profile_count=1)


def test_output_cannot_be_written_inside_read_only_gold_tree(tmp_path: Path) -> None:
    notice_id = "PBLN_000000000000001"
    gold = tmp_path / "gold"
    _write_boundary_gold(gold, {notice_id: _profile(f"bizinfo:{notice_id}")})
    output = gold / "reports"
    output.mkdir()
    report = comparison.compare_loaded_profiles(
        _loaded({notice_id: _profile(notice_id)}, role="automatic"),
        _loaded({notice_id: _profile(f"bizinfo:{notice_id}")}, role="gold"),
    )

    with pytest.raises(comparison.ExistingProfileComparisonError, match="outside the read-only Gold root"):
        comparison.write_comparison_report(report, output_dir=output, gold_root=gold)
    assert list(output.iterdir()) == []


def test_output_directory_path_must_not_traverse_a_symlink(tmp_path: Path) -> None:
    notice_id = "PBLN_000000000000001"
    gold = tmp_path / "gold"
    gold.mkdir()
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    link = tmp_path / "output-link"
    try:
        link.symlink_to(real_output, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on test filesystem: {error}")
    report = comparison.compare_loaded_profiles(
        _loaded({notice_id: _profile(notice_id)}, role="automatic"),
        _loaded({notice_id: _profile(f"bizinfo:{notice_id}")}, role="gold"),
    )

    with pytest.raises(comparison.ExistingProfileComparisonError, match="must not traverse a symlink"):
        comparison.write_comparison_report(report, output_dir=link, gold_root=gold)
    assert list(real_output.iterdir()) == []


def test_output_ancestor_swap_to_gold_is_rejected_at_fd_chain_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_id = "PBLN_000000000000001"
    gold = tmp_path / "gold"
    gold_target = gold / "leaf"
    gold_target.mkdir(parents=True)
    output_parent = tmp_path / "output-parent"
    output = output_parent / "leaf"
    output.mkdir(parents=True)
    moved_parent = tmp_path / "moved-output-parent"
    report = comparison.compare_loaded_profiles(
        _loaded({notice_id: _profile(notice_id)}, role="automatic"),
        _loaded({notice_id: _profile(f"bizinfo:{notice_id}")}, role="gold"),
    )
    original_opener = comparison._open_absolute_directory_chain_no_follow
    swapped = False

    def swap_then_open(path: Path, *, label: str) -> list[int]:
        nonlocal swapped
        if label == "output directory" and not swapped:
            output_parent.rename(moved_parent)
            output_parent.symlink_to(gold, target_is_directory=True)
            swapped = True
        return original_opener(path, label=label)

    monkeypatch.setattr(
        comparison,
        "_open_absolute_directory_chain_no_follow",
        swap_then_open,
    )

    with pytest.raises(
        comparison.ExistingProfileComparisonError,
        match="without following ancestor symlinks",
    ):
        comparison.write_comparison_report(report, output_dir=output, gold_root=gold)

    assert swapped is True
    assert not (gold_target / comparison.REPORT_FILE_NAME).exists()
    assert not (moved_parent / "leaf" / comparison.REPORT_FILE_NAME).exists()


def test_identity_pin_mismatch_fails_closed() -> None:
    notice_id = "PBLN_000000000000001"
    report = comparison.compare_loaded_profiles(
        comparison.LoadedProfiles(
            profiles={notice_id: _profile(notice_id)},
            identity={"archive_sha256": "a" * 64},
        ),
        comparison.LoadedProfiles(
            profiles={notice_id: _profile(f"bizinfo:{notice_id}")},
            identity={"freeze_manifest_sha256": "b" * 64},
        ),
    )

    with pytest.raises(comparison.ExistingProfileExpectationError, match="archive_sha256"):
        comparison.assert_expected_comparison(
            report,
            baseline_archive_sha256="c" * 64,
        )


def test_gold_profile_mutation_is_rejected_by_existing_freeze_verifier(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    notice_id, profile = _verified_fixture_profile(gold)
    profile["comparison_profile"]["support_content"][0]["value_raw"] = "변조됨"  # type: ignore[index]
    profile_path = gold / "notices" / notice_id / comparison.GOLD_PROFILE_NAME
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(comparison.ExistingProfileComparisonError, match="Gold verification failed"):
        comparison.load_reviewed_gold(gold, expected_profile_count=1)


@pytest.mark.skipif(
    not (REAL_BASELINE.is_file() and REAL_GOLD.is_dir()),
    reason="external baseline/Gold corpora are not installed",
)
def test_real_reviewed_gold100_has_exactly_six_human_corrected_profiles() -> None:
    report = comparison.compare_existing_profile_corpora(REAL_BASELINE, REAL_GOLD)

    comparison.assert_expected_comparison(
        report,
        baseline=100,
        gold=100,
        shared=100,
        unchanged=94,
        changed=6,
        baseline_only=0,
        gold_only=0,
        changed_notice_ids=sorted(REAL_CHANGED_IDS),
        baseline_archive_sha256=REAL_BASELINE_SHA256,
        gold_freeze_manifest_sha256=REAL_GOLD_MANIFEST_SHA256,
    )
    assert report["counts"]["baseline_only"] == 0
    assert report["counts"]["gold_only"] == 0
    assert {row["notice_id"] for row in report["changed"]} == REAL_CHANGED_IDS
