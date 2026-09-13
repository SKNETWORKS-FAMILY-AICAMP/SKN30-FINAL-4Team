#!/usr/bin/env python3
"""Prepare an isolated, verified Model 1 serving directory.

The Model 1 serving package is deliberately not in Git.  This command writes
an explicit allowlist of its files from a SHA-pinned archive to an otherwise
empty directory under ``.runtime``.  It never extracts anything into the
checkout and refuses to replace a destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import zipfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE = Path.home() / "serving.zip"
EXPECTED_ARCHIVE_SHA256 = "0fca416dfe6910f2fc00764c94d8418dc67dc42c79569feadd036e0cdc0ede41"
EXPECTED_WEIGHT_SHA256 = "8fa1522ced99f69966aed797c94cbd841f9ee9ce7d94c84dbc55adbf28613779"
EXPECTED_RUNTIME_MANIFEST_SHA256 = (
    "d44007342e06d7f20039d53e140e04221e8029b4cd6735dd3fbacc6864eb7912"
)
WEIGHT_MEMBER = "model1/model/model.safetensors"
ARCHIVE_MODEL1_MEMBERS = (
    "model1/external.parquet",
    "model1/inference.py",
    "model1/label_mapping.json",
    "model1/model/config.json",
    WEIGHT_MEMBER,
    "model1/predict.py",
    "model1/tokenizer/tokenizer.json",
    "model1/tokenizer/tokenizer_config.json",
    "model1/train.parquet",
)

# These logical names are the registered Model 1 identity.  Do not add the
# operator-only requirements file here: migration 31/32 intentionally commits
# the digest of the base requirements file below.
MANIFEST_FILES = {
    "serving/inference.py": ("serving", "inference.py"),
    "serving/label_mapping.json": ("serving", "label_mapping.json"),
    "serving/model/config.json": ("serving", "model", "config.json"),
    "serving/model/model.safetensors": ("serving", "model", "model.safetensors"),
    "serving/tokenizer/tokenizer.json": ("serving", "tokenizer", "tokenizer.json"),
    "serving/tokenizer/tokenizer_config.json": (
        "serving",
        "tokenizer",
        "tokenizer_config.json",
    ),
    "pipeline/dl07_m1_apply.py": ("pipeline", "dl07_m1_apply.py"),
    "runtime/requirements.txt": ("runtime", "requirements.txt"),
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


class PreparationError(RuntimeError):
    """An input or integrity precondition was not met."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_runtime_destination(destination: Path, repository_root: Path) -> Path:
    resolved = destination.expanduser().resolve()
    runtime_root = (repository_root / ".runtime").resolve()
    try:
        resolved.relative_to(runtime_root)
    except ValueError as exc:
        raise PreparationError(
            f"destination must be below the checkout .runtime directory: {runtime_root}"
        ) from exc
    if resolved == runtime_root:
        raise PreparationError("destination must be a child of .runtime, not .runtime itself")
    if resolved.name.lower() != "model1":
        raise PreparationError("destination directory name must be model1")
    if resolved.exists() or resolved.is_symlink():
        raise PreparationError(
            f"destination already exists and will not be overwritten: {resolved}"
        )
    return resolved


def _manifest_sha256(destination_model1: Path, repository_root: Path) -> str:
    ml_root = repository_root / "ml"
    locations = {
        "serving": destination_model1,
        "pipeline": ml_root / "pipelines" / "model1",
        "runtime": ml_root / "serving",
        "backend": repository_root / "backend",
    }
    digests: dict[str, str] = {}
    for logical_name, (area, *relative) in MANIFEST_FILES.items():
        path = locations[area].joinpath(*relative)
        if not path.is_file():
            raise PreparationError(f"runtime manifest file is missing: {logical_name}")
        digests[logical_name] = sha256_file(path)
    canonical = json.dumps(digests, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _restrict_permissions(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & ~0o077)


def _install_verified_tree(stage: Path, destination: Path) -> None:
    """Reserve ``destination`` atomically, then install only verified entries.

    Reserving the directory with ``mkdir(exist_ok=False)`` closes the POSIX
    ``rename`` race where another process could create an empty destination
    after the initial preflight and have it silently replaced.  Consumers must
    only use the path after this command reports success.
    """

    try:
        destination.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        raise PreparationError(
            f"destination already exists and will not be overwritten: {destination}"
        ) from None

    try:
        for entry in sorted(stage.iterdir(), key=lambda path: path.name):
            entry.rename(destination / entry.name)
    except Exception:
        # This command created the reservation, so only its own partial tree is
        # removed.  A pre-existing destination never reaches this branch.
        shutil.rmtree(destination, ignore_errors=True)
        raise


def prepare_runtime(
    archive: Path,
    destination: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    """Create a new verified runtime tree and return its ``model1`` directory."""

    repository_root = repository_root.resolve()
    destination = _assert_runtime_destination(destination, repository_root)
    archive = archive.expanduser().resolve()
    if not archive.is_file():
        raise PreparationError(f"archive was not found: {archive}")
    if sha256_file(archive) != EXPECTED_ARCHIVE_SHA256:
        raise PreparationError("serving.zip SHA-256 mismatch")

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage_parent = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-",
            dir=destination.parent,
        )
    )
    stage = stage_parent / destination.name
    try:
        try:
            with zipfile.ZipFile(archive) as bundle:
                for member_name in ARCHIVE_MODEL1_MEMBERS:
                    member = bundle.getinfo(member_name)
                    if member.is_dir() or member.flag_bits & 0x1:
                        raise PreparationError(f"serving.zip entry is invalid: {member_name}")
                    target = stage.joinpath(*Path(member_name).parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as input_stream, target.open("xb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        except (KeyError, zipfile.BadZipFile) as exc:
            raise PreparationError(
                "serving.zip does not contain the complete expected Model 1 package"
            ) from exc
        weight_path = stage / "model" / "model.safetensors"
        if sha256_file(weight_path) != EXPECTED_WEIGHT_SHA256:
            raise PreparationError("model.safetensors SHA-256 mismatch")

        manifest = _manifest_sha256(stage, repository_root)
        if manifest != EXPECTED_RUNTIME_MANIFEST_SHA256:
            raise PreparationError(
                "Model 1 runtime manifest SHA-256 mismatch; checkout bytes do not match "
                "the registered runtime"
            )
        _restrict_permissions(stage)
        _install_verified_tree(stage, destination)
        stage.rmdir()
    except Exception:
        shutil.rmtree(stage_parent, ignore_errors=True)
        raise
    else:
        stage_parent.rmdir()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE,
        help="verified serving.zip",
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help=(
            "new model1 directory below this checkout's .runtime/ "
            "(for example .runtime/model1-serving/model1)"
        ),
    )
    args = parser.parse_args(argv)
    try:
        runtime = prepare_runtime(args.archive, args.destination)
    except PreparationError as exc:
        print(f"Model 1 runtime preparation failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "model1_serving_dir": str(runtime),
                "weight_sha256": EXPECTED_WEIGHT_SHA256,
                "runtime_manifest_sha256": EXPECTED_RUNTIME_MANIFEST_SHA256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
