"""Fail-closed integrity gate for the dedicated Docker ML worker.

The analysis worker executes Model 1/2/3 only from local, immutable inputs.
Checking those inputs once before polling prevents a deployment typo or a
partially copied Model 1 mount from quietly turning an analysis result into an
``unavailable`` response.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path


MODEL1_WEIGHT_SHA256 = "8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779"
MODEL1_RUNTIME_MANIFEST_SHA256 = (
    "2903d0e90e71cd121af3185eeab3fefe3e1407175d14476e8d60f611b6861a60"
)
MODEL2_BUNDLE_SHA256 = "0c5b93e2a04c778ace8e07d7551b1fc7c3cc2b91cde80d94b2b1b1cf38cbabff"
MODEL2_COHORT_SHA256 = "551c4b2018d567419cc39344e87646e70f2d0a2ce58b6748d36573254ba0439d"
MODEL2_TAXONOMY_SHA256 = "78ebba0d5264fc2b9d8092baba4f790d4fea147e0ad98e4ede3c87f248c22239"
MODEL3_POOL_SHA256 = "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf"

# This is the same registered identity used by prepare_model1_runtime.py.
# Keep the logical-name mapping explicit here: this runtime check must not
# depend on an operator script or any host checkout state.
MODEL1_MANIFEST_FILES: dict[str, tuple[str, ...]] = {
    "serving/inference.py": ("model1", "inference.py"),
    "serving/label_mapping.json": ("model1", "label_mapping.json"),
    "serving/model/config.json": ("model1", "model", "config.json"),
    "serving/model/model.safetensors": ("model1", "model", "model.safetensors"),
    "serving/tokenizer/tokenizer.json": ("model1", "tokenizer", "tokenizer.json"),
    "serving/tokenizer/tokenizer_config.json": (
        "model1",
        "tokenizer",
        "tokenizer_config.json",
    ),
    "pipeline/dl07_m1_apply.py": ("ml", "pipelines", "model1", "dl07_m1_apply.py"),
    "runtime/requirements.txt": ("ml", "serving", "requirements.txt"),
    "backend/scripts/classify_existing_model1.py": (
        "backend",
        "scripts",
        "classify_existing_model1.py",
    ),
    "backend/worker/existing_model1.py": ("backend", "worker", "existing_model1.py"),
    "backend/worker/adapters/ml_child.py": (
        "backend",
        "worker",
        "adapters",
        "ml_child.py",
    ),
    "backend/worker/adapters/ml_subprocess.py": (
        "backend",
        "worker",
        "adapters",
        "ml_subprocess.py",
    ),
    "backend/worker/ml_reference.py": ("backend", "worker", "ml_reference.py"),
    "backend/worker/contracts/ml_result.py": (
        "backend",
        "worker",
        "contracts",
        "ml_result.py",
    ),
}

MODEL1_LAYOUT = (
    ("external.parquet",),
    ("inference.py",),
    ("label_mapping.json",),
    ("model", "config.json"),
    ("model", "model.safetensors"),
    ("predict.py",),
    ("tokenizer", "tokenizer.json"),
    ("tokenizer", "tokenizer_config.json"),
    ("train.parquet",),
)


def _model1_allowed_directories() -> frozenset[Path]:
    directories: set[Path] = set()
    for relative in MODEL1_LAYOUT:
        current = Path(*relative).parent
        while current != Path("."):
            directories.add(current)
            current = current.parent
    return frozenset(directories)


MODEL1_ALLOWED_DIRECTORIES = _model1_allowed_directories()


class MlRuntimePreflightError(RuntimeError):
    """A mounted or image-baked ML runtime is not the registered release."""


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_file(path: Path, *, label: str) -> None:
    if not path.is_file():
        raise MlRuntimePreflightError(f"ML runtime required file is missing: {label}")


def _verify_digest(path: Path, expected: str, *, label: str) -> None:
    _required_file(path, label=label)
    if sha256_file(path) != expected:
        raise MlRuntimePreflightError(f"ML runtime SHA-256 mismatch: {label}")


def _verify_exact_model1_tree(model1_dir: Path) -> None:
    """Reject mutable or unregistered entries in the mounted release tree."""

    allowed_files = {Path(*relative) for relative in MODEL1_LAYOUT}
    for entry in model1_dir.rglob("*"):
        relative = entry.relative_to(model1_dir)
        if entry.is_symlink():
            raise MlRuntimePreflightError(
                f"Model 1 runtime contains a symlink: model1/{relative.as_posix()}"
            )
        if relative in allowed_files:
            if not entry.is_file():
                raise MlRuntimePreflightError(
                    f"Model 1 runtime file has the wrong type: "
                    f"model1/{relative.as_posix()}"
                )
            continue
        if relative in MODEL1_ALLOWED_DIRECTORIES:
            if not entry.is_dir():
                raise MlRuntimePreflightError(
                    f"Model 1 runtime directory has the wrong type: "
                    f"model1/{relative.as_posix()}"
                )
            continue
        raise MlRuntimePreflightError(
            f"Model 1 runtime contains an unregistered entry: "
            f"model1/{relative.as_posix()}"
        )


def _manifest_path(
    parts: tuple[str, ...], *, model1_dir: Path, ml_root: Path, backend_root: Path
) -> Path:
    area, *relative = parts
    if area == "model1":
        return model1_dir.joinpath(*relative)
    if area == "ml":
        return ml_root.joinpath(*relative)
    if area == "backend":
        return backend_root.joinpath(*relative)
    raise AssertionError(f"unsupported Model 1 manifest area: {area}")


def model1_manifest_sha256(*, model1_dir: Path, ml_root: Path, backend_root: Path) -> str:
    digests: dict[str, str] = {}
    for logical_name, parts in MODEL1_MANIFEST_FILES.items():
        path = _manifest_path(
            parts, model1_dir=model1_dir, ml_root=ml_root, backend_root=backend_root
        )
        _required_file(path, label=logical_name)
        digests[logical_name] = sha256_file(path)
    canonical = json.dumps(digests, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def verify_ml_runtime(
    *,
    ml_root: Path,
    model1_serving_dir: Path | None,
    backend_root: Path,
    verify_model1: bool = True,
    verify_model23: bool = True,
) -> None:
    """Verify every artifact used by the Docker analysis worker.

    This intentionally returns no diagnostic hashes: startup logs should tell
    an operator which immutable input failed without publishing local paths or
    artifact contents.
    """

    if verify_model1:
        if model1_serving_dir is None:
            raise MlRuntimePreflightError("Model 1 runtime directory is missing")
        model1_dir = model1_serving_dir
        if not (model1_dir / "inference.py").is_file() and (
            model1_dir / "model1" / "inference.py"
        ).is_file():
            model1_dir = model1_dir / "model1"
        _verify_exact_model1_tree(model1_dir)
        for relative in MODEL1_LAYOUT:
            _required_file(
                model1_dir.joinpath(*relative), label="model1/" + "/".join(relative)
            )
        _verify_digest(
            model1_dir / "model" / "model.safetensors",
            MODEL1_WEIGHT_SHA256,
            label="model1/model.safetensors",
        )
        if (
            model1_manifest_sha256(
                model1_dir=model1_dir, ml_root=ml_root, backend_root=backend_root
            )
            != MODEL1_RUNTIME_MANIFEST_SHA256
        ):
            raise MlRuntimePreflightError("Model 1 runtime manifest SHA-256 mismatch")

    if verify_model23:
        _verify_digest(
            ml_root / "models" / "model2_canonical" / "model2_p3_bundle.joblib",
            MODEL2_BUNDLE_SHA256,
            label="model2/model2_p3_bundle.joblib",
        )
        _verify_digest(
            ml_root / "serving" / "model2" / "cohort_reference.parquet",
            MODEL2_COHORT_SHA256,
            label="model2/cohort_reference.parquet",
        )
        _verify_digest(
            ml_root / "data" / "processed" / "business_taxonomy.parquet",
            MODEL2_TAXONOMY_SHA256,
            label="model2/business_taxonomy.parquet",
        )
        _verify_digest(
            ml_root / "serving" / "model3" / "design_features_v3.parquet",
            MODEL3_POOL_SHA256,
            label="model3/design_features_v3.parquet",
        )
