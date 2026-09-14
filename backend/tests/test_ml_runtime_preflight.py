from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import zipfile

import pytest

from worker import ml_runtime_preflight as preflight


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
PREPARE_SCRIPT = BACKEND_ROOT / "scripts" / "prepare_model1_runtime.py"
PREPARE_SPEC = importlib.util.spec_from_file_location(
    "prepare_model1_runtime_for_contract_test", PREPARE_SCRIPT
)
assert PREPARE_SPEC is not None and PREPARE_SPEC.loader is not None
PREPARE_MODULE = importlib.util.module_from_spec(PREPARE_SPEC)
PREPARE_SPEC.loader.exec_module(PREPARE_MODULE)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _runtime_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    model1 = tmp_path / "model1"
    ml_root = tmp_path / "ml"
    backend_root = tmp_path / "backend"
    for relative in preflight.MODEL1_LAYOUT:
        _write(model1.joinpath(*relative), "/".join(relative).encode())
    for logical_name, parts in preflight.MODEL1_MANIFEST_FILES.items():
        area, *relative = parts
        root = {"model1": model1, "ml": ml_root, "backend": backend_root}[area]
        _write(root.joinpath(*relative), logical_name.encode())

    bundle = ml_root / "models/model2_canonical/model2_p3_bundle.joblib"
    cohort = ml_root / "serving/model2/cohort_reference.parquet"
    taxonomy = ml_root / "data/processed/business_taxonomy.parquet"
    pool = ml_root / "serving/model3/design_features_v3.parquet"
    _write(bundle, b"model2 bundle")
    _write(cohort, b"model2 cohort")
    _write(taxonomy, b"model2 taxonomy")
    _write(pool, b"model3 pool")
    monkeypatch.setattr(
        preflight,
        "MODEL1_WEIGHT_SHA256",
        preflight.sha256_file(model1 / "model/model.safetensors"),
    )
    monkeypatch.setattr(
        preflight,
        "MODEL1_RUNTIME_MANIFEST_SHA256",
        preflight.model1_manifest_sha256(
            model1_dir=model1, ml_root=ml_root, backend_root=backend_root
        ),
    )
    monkeypatch.setattr(preflight, "MODEL2_BUNDLE_SHA256", preflight.sha256_file(bundle))
    monkeypatch.setattr(preflight, "MODEL2_COHORT_SHA256", preflight.sha256_file(cohort))
    monkeypatch.setattr(
        preflight, "MODEL2_TAXONOMY_SHA256", preflight.sha256_file(taxonomy)
    )
    monkeypatch.setattr(preflight, "MODEL3_POOL_SHA256", preflight.sha256_file(pool))
    return model1, ml_root, backend_root


def test_preflight_accepts_complete_registered_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model1, ml_root, backend_root = _runtime_tree(tmp_path, monkeypatch)

    preflight.verify_ml_runtime(
        model1_serving_dir=model1, ml_root=ml_root, backend_root=backend_root
    )


def test_preflight_rejects_unregistered_model1_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model1, ml_root, backend_root = _runtime_tree(tmp_path, monkeypatch)
    _write(model1 / "__pycache__/inference.cpython-313.pyc", b"untracked-code")

    with pytest.raises(preflight.MlRuntimePreflightError, match="unregistered entry"):
        preflight.verify_ml_runtime(
            model1_serving_dir=model1, ml_root=ml_root, backend_root=backend_root
        )


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_preflight_rejects_a_model1_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model1, ml_root, backend_root = _runtime_tree(tmp_path, monkeypatch)
    target = tmp_path / "replacement.py"
    target.write_text("# replacement\n", encoding="utf-8")
    (model1 / "inference.py").unlink()
    (model1 / "inference.py").symlink_to(target)

    with pytest.raises(preflight.MlRuntimePreflightError, match="symlink"):
        preflight.verify_ml_runtime(
            model1_serving_dir=model1, ml_root=ml_root, backend_root=backend_root
        )


@pytest.mark.parametrize(
    "relative",
    ("model/model.safetensors", "inference.py"),
)
def test_preflight_rejects_model1_weight_or_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    model1, ml_root, backend_root = _runtime_tree(tmp_path, monkeypatch)
    (model1 / relative).write_bytes(b"tampered")

    with pytest.raises(preflight.MlRuntimePreflightError, match="SHA-256"):
        preflight.verify_ml_runtime(
            model1_serving_dir=model1, ml_root=ml_root, backend_root=backend_root
        )


@pytest.mark.parametrize(
    ("relative", "label"),
    (
        ("models/model2_canonical/model2_p3_bundle.joblib", "model2"),
        ("serving/model2/cohort_reference.parquet", "model2"),
        ("data/processed/business_taxonomy.parquet", "model2"),
        ("serving/model3/design_features_v3.parquet", "model3"),
    ),
)
def test_preflight_fails_closed_for_each_model_artifact_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    label: str,
) -> None:
    model1, ml_root, backend_root = _runtime_tree(tmp_path, monkeypatch)
    (ml_root / relative).write_bytes(b"tampered")

    with pytest.raises(preflight.MlRuntimePreflightError, match=label):
        preflight.verify_ml_runtime(
            model1_serving_dir=model1, ml_root=ml_root, backend_root=backend_root
        )


def test_preflight_constants_pin_the_registered_model_2_and_3_artifacts() -> None:
    assert preflight.MODEL2_BUNDLE_SHA256 == (
        "0c5b93e2a04c778ace8e07d7551b1fc7c3cc2b91cde80d94b2b1b1cf38cbabff"
    )
    assert preflight.MODEL2_COHORT_SHA256 == (
        "551c4b2018d567419cc39344e87646e70f2d0a2ce58b6748d36573254ba0439d"
    )
    assert preflight.MODEL2_TAXONOMY_SHA256 == (
        "78ebba0d5264fc2b9d8092baba4f790d4fea147e0ad98e4ede3c87f248c22239"
    )
    assert preflight.MODEL3_POOL_SHA256 == (
        "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf"
    )


def test_registered_model_artifacts_match_source_files_and_dockerfile() -> None:
    registered = {
        "/app/ml/models/model2_canonical/model2_p3_bundle.joblib": (
            preflight.MODEL2_BUNDLE_SHA256,
            REPOSITORY_ROOT
            / "ml/models/model2_canonical/model2_p3_bundle.joblib",
        ),
        "/app/ml/serving/model2/cohort_reference.parquet": (
            preflight.MODEL2_COHORT_SHA256,
            REPOSITORY_ROOT / "ml/serving/model2/cohort_reference.parquet",
        ),
        "/app/ml/data/processed/business_taxonomy.parquet": (
            preflight.MODEL2_TAXONOMY_SHA256,
            REPOSITORY_ROOT / "ml/data/processed/business_taxonomy.parquet",
        ),
        "/app/ml/serving/model3/design_features_v3.parquet": (
            preflight.MODEL3_POOL_SHA256,
            REPOSITORY_ROOT / "ml/serving/model3/design_features_v3.parquet",
        ),
    }
    for expected_digest, source_path in registered.values():
        assert preflight.sha256_file(source_path) == expected_digest

    dockerfile = (BACKEND_ROOT / "Dockerfile.ml-worker").read_text(encoding="utf-8")
    dockerfile_pairs = {
        image_path: digest
        for digest, image_path in re.findall(
            r"'([0-9a-f]{64})' '(/app/ml/[^']+)'", dockerfile
        )
    }
    assert dockerfile_pairs == {
        image_path: digest for image_path, (digest, _path) in registered.items()
    }


def _normalized_preparation_manifest_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    area, *relative = parts
    if area == "serving":
        return ("model1", *relative)
    if area == "pipeline":
        return ("ml", "pipelines", "model1", *relative)
    if area == "runtime":
        return ("ml", "serving", *relative)
    if area == "backend":
        return ("backend", *relative)
    raise AssertionError(f"unexpected preparation manifest area: {area}")


def test_model1_preparation_and_startup_integrity_contracts_stay_in_sync() -> None:
    assert preflight.MODEL1_WEIGHT_SHA256 == PREPARE_MODULE.EXPECTED_WEIGHT_SHA256
    assert (
        preflight.MODEL1_RUNTIME_MANIFEST_SHA256
        == PREPARE_MODULE.EXPECTED_RUNTIME_MANIFEST_SHA256
    )
    assert set(preflight.MODEL1_MANIFEST_FILES) == set(PREPARE_MODULE.MANIFEST_FILES)
    for logical_name, preparation_parts in PREPARE_MODULE.MANIFEST_FILES.items():
        assert preflight.MODEL1_MANIFEST_FILES[logical_name] == (
            _normalized_preparation_manifest_parts(preparation_parts)
        )

    archive_layout = {
        tuple(member.removeprefix("model1/").split("/"))
        for member in PREPARE_MODULE.ARCHIVE_MODEL1_MEMBERS
    }
    assert set(preflight.MODEL1_LAYOUT) == archive_layout


@pytest.mark.skipif(
    not PREPARE_MODULE.DEFAULT_ARCHIVE.is_file(),
    reason="verified serving.zip is not available on this checkout host",
)
def test_registered_model1_manifest_matches_verified_serving_archive(
    tmp_path: Path,
) -> None:
    """Pin the real mounted-artifact plus checked-out-code release identity.

    ``serving.zip`` is intentionally Git-external, so ordinary CI skips this
    host-artifact regression.  A host that has the documented archive proves
    both the archive digest and the manifest made from its exact allowlisted
    Model 1 bytes and this checkout's Model 1 runtime code.
    """

    archive = PREPARE_MODULE.DEFAULT_ARCHIVE
    assert PREPARE_MODULE.sha256_file(archive) == PREPARE_MODULE.EXPECTED_ARCHIVE_SHA256

    model1 = tmp_path / "model1"
    with zipfile.ZipFile(archive) as bundle:
        for member_name in PREPARE_MODULE.ARCHIVE_MODEL1_MEMBERS:
            member = bundle.getinfo(member_name)
            assert not member.is_dir()
            target = model1.joinpath(*Path(member_name).parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)

    actual = preflight.model1_manifest_sha256(
        model1_dir=model1,
        ml_root=REPOSITORY_ROOT / "ml",
        backend_root=BACKEND_ROOT,
    )
    assert actual == preflight.MODEL1_RUNTIME_MANIFEST_SHA256
    assert actual == PREPARE_MODULE.EXPECTED_RUNTIME_MANIFEST_SHA256
