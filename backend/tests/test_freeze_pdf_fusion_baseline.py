from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "freeze_pdf_fusion_baseline.py"
BACKEND_ROOT = SCRIPT.parents[1]
SPEC = importlib.util.spec_from_file_location("freeze_pdf_fusion_baseline", SCRIPT)
assert SPEC and SPEC.loader
baseline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = baseline
SPEC.loader.exec_module(baseline)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_run(root: Path, logical_id: str, extension: str, source: bytes) -> dict[str, str]:
    run = root / logical_id
    source_path = run / "attachments" / f"source.{extension}"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)
    common_ir = run / "pipeline" / "common_ir_v1" / f"{logical_id}.{extension}.json"
    profile = run / "pipeline" / "structured_profile.v0.2.json"
    candidate = run / "pipeline" / "source_selection.json"
    _write_json(common_ir, {"blocks": [{"block_id": "b1"}], "relations": []})
    _write_json(profile, {"comparison_profile": {"purpose_goal": [{"fact_id": "f1"}]}, "support_components": []})
    _write_json(candidate, {"source_block_texts": {"b1": "source body"}})
    return {
        "source": source_path.relative_to(root).as_posix(),
        "common_ir": common_ir.relative_to(root).as_posix(),
        "structured_profile": profile.relative_to(root).as_posix(),
        "candidate_pack": candidate.relative_to(root).as_posix(),
    }


def _inventory(runs: list[tuple[str, dict[str, str]]]) -> dict[str, object]:
    return {
        "schema_version": baseline.INVENTORY_SCHEMA_VERSION,
        "corpus_id": "synthetic-pdf-fusion",
        "expected": {"all_runs": len(runs), "pdf_runs": 1},
        "runs": [
            {
                "logical_id": logical_id,
                "source_path": paths["source"],
                "artifacts": {key: value for key, value in paths.items() if key != "source"},
            }
            for logical_id, paths in runs
        ],
    }


def test_creates_deterministic_manifest_and_checks_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    corpus = tmp_path / "corpus"
    pdf = _write_run(corpus, "PBLN_0002", "pdf", b"%PDF fixture\n")
    hwpx = _write_run(corpus, "PBLN_0001", "hwpx", b"hwpx fixture\n")
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, _inventory([("PBLN_0002", pdf), ("PBLN_0001", hwpx)]))
    output = tmp_path / "baseline.json"

    assert baseline.main(["--corpus-root", str(corpus), "--inventory", str(inventory_path), "--output", str(output)]) == 0
    first_bytes = output.read_bytes()
    created_stdout = capsys.readouterr().out
    result = json.loads(created_stdout)
    assert result["status"] == "created"
    assert result["manifest_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert "source body" not in created_stdout
    assert "%PDF fixture" not in created_stdout

    document = json.loads(first_bytes)
    assert document["counts"] == {
        "all_runs": 2,
        "pdf_runs": 1,
        "extensions": {"hwpx": 1, "pdf": 1},
        "artifacts": {"candidate_pack": 2, "common_ir": 2, "structured_profile": 2},
    }
    assert [item["logical_id"] for item in document["all_runs"]] == ["PBLN_0001", "PBLN_0002"]
    assert [item["logical_id"] for item in document["pdf_subset"]["runs"]] == ["PBLN_0002"]
    assert document["all_runs"][0]["artifacts"]["common_ir"]["counts"] == {"blocks": 1, "relations": 0}

    assert baseline.main(["--corpus-root", str(corpus), "--inventory", str(inventory_path), "--output", str(output), "--check"]) == 0
    assert output.read_bytes() == first_bytes
    assert json.loads(capsys.readouterr().out)["status"] == "valid"


def test_discovery_mode_finds_one_source_and_required_standard_artifacts(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _write_run(corpus, "PBLN_001", "pdf", b"one")

    document = baseline.build_baseline(corpus)

    assert document["corpus_id"] == "corpus"
    assert document["counts"]["all_runs"] == 1
    assert document["all_runs"][0]["source"]["extension"] == "pdf"
    assert set(document["all_runs"][0]["artifacts"]) == {
        "candidate_pack",
        "common_ir",
        "structured_profile",
    }


def test_discovery_and_inventory_require_all_baseline_artifacts(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    paths = _write_run(corpus, "PBLN_001", "pdf", b"one")
    (corpus / paths["candidate_pack"]).unlink()

    with pytest.raises(baseline.BaselineError, match="required artifacts are missing: candidate_pack"):
        baseline.build_baseline(corpus)

    inventory = _inventory([("PBLN_001", paths)])
    inventory["runs"][0]["artifacts"].pop("candidate_pack")  # type: ignore[index]
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)
    with pytest.raises(baseline.BaselineError, match="required artifacts are missing: candidate_pack"):
        baseline.build_baseline(corpus, inventory_path=inventory_path)


def test_inventory_rejects_duplicate_logical_id_with_different_source_hash(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    first = _write_run(corpus, "PBLN_001", "pdf", b"one")
    second = _write_run(corpus, "PBLN_002", "pdf", b"two")
    inventory = _inventory([("PBLN_DUPLICATE", first), ("PBLN_DUPLICATE", second)])
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)

    with pytest.raises(baseline.BaselineError, match="duplicate logical_id with different source SHA-256"):
        baseline.build_baseline(corpus, inventory_path=inventory_path)


@pytest.mark.parametrize("kind", ["source_path", "source_hash", "artifact_path", "cross_run_artifact"])
def test_inventory_rejects_cross_run_source_and_artifact_aliases(tmp_path: Path, kind: str) -> None:
    corpus = tmp_path / "corpus"
    first = _write_run(corpus, "PBLN_001", "pdf", b"same" if kind == "source_hash" else b"one")
    second = _write_run(corpus, "PBLN_002", "pdf", b"same" if kind == "source_hash" else b"two")
    inventory = _inventory([("PBLN_001", first), ("PBLN_002", second)])
    second_run = inventory["runs"][1]  # type: ignore[index]
    if kind == "source_path":
        second_run["source_path"] = first["source"]
        message = "duplicate source path"
    elif kind == "source_hash":
        message = "duplicate source SHA-256"
    elif kind == "artifact_path":
        second_run["artifacts"]["candidate_pack"] = first["candidate_pack"]
        message = "must stay under its logical_id tree"
    else:
        second_run["artifacts"]["candidate_pack"] = "PBLN_001/pipeline/other.json"
        message = "must stay under its logical_id tree"
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)
    with pytest.raises(baseline.BaselineError, match=message):
        baseline.build_baseline(corpus, inventory_path=inventory_path)


def test_freezer_rejects_non_finite_canonical_value() -> None:
    with pytest.raises(baseline.BaselineError, match="canonically serializable"):
        baseline._canonical_json({"bad": float("nan")})


def test_inventory_rejects_duplicate_artifact_path_within_run(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    paths = _write_run(corpus, "PBLN_001", "pdf", b"one")
    inventory = _inventory([("PBLN_001", paths)])
    inventory["runs"][0]["artifacts"]["candidate_pack"] = paths["structured_profile"]  # type: ignore[index]
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)
    with pytest.raises(baseline.BaselineError, match="duplicate artifact path"):
        baseline.build_baseline(corpus, inventory_path=inventory_path)


@pytest.mark.parametrize("source_path", ["../outside.pdf", "PBLN_001/attachments/missing.pdf"])
def test_inventory_fails_closed_for_traversal_and_missing_source(tmp_path: Path, source_path: str) -> None:
    corpus = tmp_path / "corpus"
    paths = _write_run(corpus, "PBLN_001", "pdf", b"source")
    inventory = _inventory([("PBLN_001", paths)])
    inventory["runs"][0]["source_path"] = source_path  # type: ignore[index]
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)

    with pytest.raises(baseline.BaselineError):
        baseline.build_baseline(corpus, inventory_path=inventory_path)


def test_rejects_symlink_anywhere_in_corpus_tree(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _write_run(corpus, "PBLN_001", "pdf", b"source")
    target = corpus / "PBLN_001" / "attachments" / "source.pdf"
    link = corpus / "PBLN_001" / "unrelated-link"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable on test filesystem: {error}")

    with pytest.raises(baseline.BaselineError, match="symlink is not allowed"):
        baseline.build_baseline(corpus)


def test_refuses_overwrite_and_check_detects_corpus_change(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    paths = _write_run(corpus, "PBLN_001", "pdf", b"source")
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, _inventory([("PBLN_001", paths)]))
    output = tmp_path / "baseline.json"

    assert baseline.main(["--corpus-root", str(corpus), "--inventory", str(inventory_path), "--output", str(output)]) == 0
    assert baseline.main(["--corpus-root", str(corpus), "--inventory", str(inventory_path), "--output", str(output)]) == 1
    (corpus / paths["source"]).write_bytes(b"changed")
    assert baseline.main(["--corpus-root", str(corpus), "--inventory", str(inventory_path), "--output", str(output), "--check"]) == 1


def test_tracked_existing_100_baseline_is_canonical_and_matches_schema() -> None:
    baseline_path = BACKEND_ROOT / "baselines" / "pdf_fusion" / "bizinfo_existing_100.v1.json"
    schema_path = BACKEND_ROOT / "baselines" / "pdf_fusion" / "pdf_fusion_corpus_baseline_v1.schema.json"
    payload = baseline_path.read_bytes()
    document = json.loads(payload)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    assert baseline._canonical_json(document) == payload
    assert hashlib.sha256(payload).hexdigest() == "78c828fc0de4e32ccfe681cece53dfbf1a3b106c48e762484be5e8a1cd46358f"
    assert document["counts"] == {
        "all_runs": 100,
        "pdf_runs": 47,
        "extensions": {"hwp": 48, "hwpx": 5, "pdf": 47},
        "artifacts": {"candidate_pack": 100, "common_ir": 100, "structured_profile": 100},
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"typo": True}), "inventory keys are invalid"),
        (lambda value: value["runs"][0].update({"typo": True}), "inventory run keys are invalid"),
        (lambda value: value["expected"].update({"pdf_run": 1}), "expected keys are invalid"),
        (lambda value: value["expected"].update({"pdf_runs": True}), "invalid expected.pdf_runs"),
    ],
)
def test_inventory_schema_rejects_unknown_keys_and_boolean_counts(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    corpus = tmp_path / "corpus"
    paths = _write_run(corpus, "PBLN_001", "pdf", b"source")
    inventory = _inventory([("PBLN_001", paths)])
    mutation(inventory)
    inventory_path = tmp_path / "inventory.json"
    _write_json(inventory_path, inventory)

    with pytest.raises(baseline.BaselineError, match=message):
        baseline.build_baseline(corpus, inventory_path=inventory_path)
