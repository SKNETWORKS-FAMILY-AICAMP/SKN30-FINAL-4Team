from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_model1_runtime.py"
SPEC = importlib.util.spec_from_file_location("prepare_model1_runtime", SCRIPT)
assert SPEC and SPEC.loader
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _fake_checkout(tmp_path: Path) -> tuple[Path, Path, bytes]:
    root = tmp_path / "checkout"
    source = root / "ml" / "serving" / "model1"
    weight = b"verified model one bytes"
    _write(source / "inference.py", b"# inference\n")
    _write(source / "label_mapping.json", b"{}\n")
    _write(source / "model" / "config.json", b"{}\n")
    _write(source / "tokenizer" / "tokenizer.json", b"{}\n")
    _write(source / "tokenizer" / "tokenizer_config.json", b"{}\n")
    # This local weight must never be used; archive bytes are authoritative.
    _write(source / "model" / "model.safetensors", b"untrusted local weight")
    _write(root / "ml" / "pipelines" / "model1" / "dl07_m1_apply.py", b"# clean\n")
    _write(root / "ml" / "serving" / "requirements.txt", b"torch==test\n")
    for _, (area, *relative) in prepare.MANIFEST_FILES.items():
        if area != "backend":
            continue
        _write(root / "backend" / Path(*relative), f"# {'/'.join(relative)}\n".encode())

    archive = tmp_path / "serving.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for member in prepare.ARCHIVE_MODEL1_MEMBERS:
            relative = Path(*Path(member).parts[1:])
            if member == prepare.WEIGHT_MEMBER:
                contents = weight
            elif member == "model1/inference.py":
                contents = b"# archive inference\n"
            else:
                source_file = source / relative
                contents = (
                    source_file.read_bytes() if source_file.is_file() else b"data\n"
                )
            bundle.writestr(member, contents)
    return root, archive, weight


def _patch_expected_hashes(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    archive: Path,
    weight: bytes,
) -> None:
    monkeypatch.setattr(prepare, "EXPECTED_ARCHIVE_SHA256", prepare.sha256_file(archive))
    monkeypatch.setattr(prepare, "EXPECTED_WEIGHT_SHA256", hashlib.sha256(weight).hexdigest())
    staged = root / ".runtime" / "expected" / "model1"
    # Calculate the expected identity from the exact logical map under test.
    for logical_name, (area, *relative) in prepare.MANIFEST_FILES.items():
        if area != "serving":
            continue
        source = root / "ml" / "serving" / "model1" / Path(*relative)
        target = staged / Path(*relative)
        contents = weight if logical_name.endswith("model.safetensors") else source.read_bytes()
        if logical_name == "serving/inference.py":
            contents = b"# archive inference\n"
        _write(target, contents)
    monkeypatch.setattr(
        prepare,
        "EXPECTED_RUNTIME_MANIFEST_SHA256",
        prepare._manifest_sha256(staged, root),
    )


def test_prepares_new_runtime_from_archive_and_checks_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archive, weight = _fake_checkout(tmp_path)
    _patch_expected_hashes(monkeypatch, root, archive, weight)

    destination = root / ".runtime" / "model1-serving" / "model1"
    result = prepare.prepare_runtime(archive, destination, repository_root=root)

    assert result == destination
    assert (result / "model" / "model.safetensors").read_bytes() == weight
    assert (result / "inference.py").read_bytes() == b"# archive inference\n"
    assert prepare._manifest_sha256(result, root) == prepare.EXPECTED_RUNTIME_MANIFEST_SHA256
    assert not (result / "model" / "model.safetensors").stat().st_mode & 0o077


def test_refuses_existing_or_non_runtime_destination(tmp_path: Path) -> None:
    root, archive, _ = _fake_checkout(tmp_path)
    existing = root / ".runtime" / "model1"
    existing.mkdir(parents=True)

    with pytest.raises(prepare.PreparationError, match="will not be overwritten"):
        prepare.prepare_runtime(archive, existing, repository_root=root)
    with pytest.raises(prepare.PreparationError, match="below the checkout .runtime"):
        prepare.prepare_runtime(archive, root / "ml" / "serving" / "model1", repository_root=root)
    with pytest.raises(prepare.PreparationError, match="name must be model1"):
        prepare.prepare_runtime(
            archive,
            root / ".runtime" / "model1-serving",
            repository_root=root,
        )


def test_rejects_wrong_archive_before_creating_destination(tmp_path: Path) -> None:
    root, archive, weight = _fake_checkout(tmp_path)
    destination = root / ".runtime" / "model1"
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_expected_hashes(monkeypatch, root, archive, weight)
        archive.write_bytes(b"changed")
        with pytest.raises(prepare.PreparationError, match="serving.zip SHA-256"):
            prepare.prepare_runtime(archive, destination, repository_root=root)
    assert not destination.exists()


def test_does_not_replace_destination_created_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archive, weight = _fake_checkout(tmp_path)
    _patch_expected_hashes(monkeypatch, root, archive, weight)
    destination = root / ".runtime" / "model1-serving" / "model1"
    original_restrict = prepare._restrict_permissions

    def create_racing_destination(stage: Path) -> None:
        original_restrict(stage)
        destination.mkdir(parents=True)
        (destination / "belongs-to-other-process").write_text(
            "preserve", encoding="utf-8"
        )

    monkeypatch.setattr(prepare, "_restrict_permissions", create_racing_destination)

    with pytest.raises(prepare.PreparationError, match="will not be overwritten"):
        prepare.prepare_runtime(archive, destination, repository_root=root)

    assert (destination / "belongs-to-other-process").read_text(encoding="utf-8") == "preserve"
