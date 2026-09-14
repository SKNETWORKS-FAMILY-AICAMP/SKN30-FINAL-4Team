import os
from pathlib import Path

from scripts.build_context_digest import (
    DOCKER_CONTEXT_EXCEPTIONS,
    DOCKER_CONTEXT_EXCLUSIONS,
    backend_build_context_digest,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_digest_rules_match_the_checked_in_dockerignore() -> None:
    rules = {
        line.strip()
        for line in (BACKEND_ROOT / ".dockerignore").read_text("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert rules == {
        *DOCKER_CONTEXT_EXCLUSIONS,
        *(f"!{item}" for item in DOCKER_CONTEXT_EXCEPTIONS),
    }


def test_digest_covers_copy_files_but_ignores_generated_identity_and_env(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (tmp_path / ".dockerignore").write_text(".env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".prereview-build-id").write_text("old identity\n", encoding="utf-8")
    generated_cache = tmp_path / "package" / "__pycache__"
    generated_cache.mkdir(parents=True)
    (generated_cache / "module.cpython-312.pyc").write_bytes(b"cache-v1")
    generated_build = tmp_path / "vendor" / "build"
    generated_build.mkdir(parents=True)
    (generated_build / "output.so").write_bytes(b"binary-v1")
    generated_metadata = tmp_path / "package.egg-info"
    generated_metadata.mkdir()
    (generated_metadata / "PKG-INFO").write_text("metadata-v1\n", encoding="utf-8")
    secret_directory = tmp_path / "nested" / "secrets"
    secret_directory.mkdir(parents=True)
    (secret_directory / "private.key").write_text("secret-v1\n", encoding="utf-8")
    (tmp_path / "nested" / "certificate.pem").write_text("pem-v1\n", encoding="utf-8")
    nested = tmp_path / "nested"
    (nested / ".env.example").write_text("safe example\n", encoding="utf-8")

    initial = backend_build_context_digest(tmp_path)
    (tmp_path / ".env").write_text("different secret\n", encoding="utf-8")
    (tmp_path / ".prereview-build-id").write_text("different old identity\n", encoding="utf-8")
    (generated_cache / "module.cpython-312.pyc").write_bytes(b"cache-v2")
    (generated_build / "output.so").write_bytes(b"binary-v2")
    (generated_metadata / "PKG-INFO").write_text("metadata-v2\n", encoding="utf-8")
    (secret_directory / "private.key").write_text("secret-v2\n", encoding="utf-8")
    (tmp_path / "nested" / "certificate.pem").write_text("pem-v2\n", encoding="utf-8")
    assert backend_build_context_digest(tmp_path) == initial

    (tmp_path / "app.py").write_text("print('two')\n", encoding="utf-8")
    assert backend_build_context_digest(tmp_path) != initial

    changed_file = backend_build_context_digest(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
    assert backend_build_context_digest(tmp_path) != changed_file


def test_digest_includes_empty_directory_path_and_mode(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    initial = backend_build_context_digest(tmp_path)

    empty_directory = tmp_path / "runtime-data"
    empty_directory.mkdir(mode=0o755)
    with_directory = backend_build_context_digest(tmp_path)
    assert with_directory != initial

    os.chmod(empty_directory, 0o700)
    assert backend_build_context_digest(tmp_path) != with_directory


def test_digest_includes_a_directory_symlink_once_as_its_own_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / "target-a").mkdir()
    (tmp_path / "target-b").mkdir()
    link = tmp_path / "directory-link"
    os.symlink("target-a", link, target_is_directory=True)
    initial = backend_build_context_digest(tmp_path)

    link.unlink()
    os.symlink("target-b", link, target_is_directory=True)
    assert backend_build_context_digest(tmp_path) != initial
