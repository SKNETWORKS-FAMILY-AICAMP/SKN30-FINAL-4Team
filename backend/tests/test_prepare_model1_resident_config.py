from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_model1_resident_config.py"
SPEC = importlib.util.spec_from_file_location("prepare_model1_resident_config", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _resolver(_runtime: Path, _preprocessor: Path) -> str:
    return "b" * 64


def _create_configuration(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    assert MODULE.prepare_configuration(
        runtime=runtime,
        token_file=token,
        env_file=identity,
        manifest_resolver=_resolver,
    ) is True
    return runtime, token, identity


def test_create_and_idempotently_verify_private_configuration(tmp_path: Path) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"

    assert MODULE.prepare_configuration(
        runtime=runtime,
        token_file=token,
        env_file=identity,
        manifest_resolver=_resolver,
    ) is True
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    if os.name == "posix":
        expected_owner = (runtime.stat().st_uid, runtime.stat().st_gid)
        assert (token.stat().st_uid, token.stat().st_gid) == expected_owner
        assert (identity.stat().st_uid, identity.stat().st_gid) == expected_owner
    assert identity.read_text(encoding="ascii") == (
        "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256=" + "b" * 64 + "\n"
    )
    token_value = token.read_text(encoding="ascii")
    assert token_value.strip()

    assert MODULE.prepare_configuration(
        runtime=runtime,
        token_file=token,
        env_file=identity,
        manifest_resolver=_resolver,
    ) is False
    assert token.read_text(encoding="ascii") == token_value


@pytest.mark.skipif(os.name != "posix", reason="POSIX numeric owner contract")
@pytest.mark.parametrize("identity_part", ["uid", "gid"])
def test_owner_mismatch_fails_before_manifest_or_output_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_part: str,
) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    manifest_called = False

    def resolver_forbidden(_runtime: Path, _preprocessor: Path) -> str:
        nonlocal manifest_called
        manifest_called = True
        raise AssertionError("owner mismatch must fail before manifest resolution")

    if identity_part == "uid":
        monkeypatch.setattr(MODULE.os, "geteuid", lambda: runtime.stat().st_uid + 1)
    else:
        monkeypatch.setattr(MODULE.os, "getegid", lambda: runtime.stat().st_gid + 1)

    with pytest.raises(MODULE.ResidentConfigurationError, match="owner.*caller"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=resolver_forbidden,
        )

    assert manifest_called is False
    assert not token.exists()
    assert not identity.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX numeric owner contract")
def test_exclusive_write_removes_output_when_filesystem_owner_is_unexpected(
    tmp_path: Path,
) -> None:
    output = tmp_path / "model1-service.token"
    unexpected_owner = (os.geteuid() + 1, os.getegid())

    with pytest.raises(MODULE.ResidentConfigurationError, match="private"):
        MODULE._write_exclusive(
            output,
            "a" * 48 + "\n",
            expected_owner=unexpected_owner,
        )

    assert not output.exists()


def test_partial_or_stale_configuration_fails_closed(tmp_path: Path) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    token.write_text("a" * 48 + "\n", encoding="ascii")
    token.chmod(0o600)

    with pytest.raises(MODULE.ResidentConfigurationError, match="partially"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
        )

    identity.write_text(
        "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256=" + "c" * 64 + "\n",
        encoding="ascii",
    )
    identity.chmod(0o600)
    with pytest.raises(MODULE.ResidentConfigurationError, match="changed"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
        )


def test_shared_or_linked_output_is_rejected(tmp_path: Path) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    token.write_text("a" * 48 + "\n", encoding="ascii")
    token.chmod(0o640)
    identity.write_text(
        "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256=" + "b" * 64 + "\n",
        encoding="ascii",
    )
    identity.chmod(0o600)

    with pytest.raises(MODULE.ResidentConfigurationError, match="private"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
        )

    token.unlink()
    target = tmp_path / "token-target"
    target.write_text("a" * 48 + "\n", encoding="ascii")
    target.chmod(0o600)
    token.symlink_to(target)
    with pytest.raises(MODULE.ResidentConfigurationError):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
        )


def test_refresh_identity_atomically_without_rotating_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, token, identity = _create_configuration(tmp_path)
    token_value = token.read_bytes()
    token_inode = token.stat().st_ino
    old_identity_inode = identity.stat().st_ino
    replacements: list[tuple[Path, Path, int]] = []
    fsynced_directory = False
    real_replace = os.replace
    real_fsync = os.fsync

    def replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append(
            (source_path, destination_path, stat.S_IMODE(source_path.stat().st_mode))
        )
        real_replace(source, destination)

    def fsync(descriptor: int) -> None:
        nonlocal fsynced_directory
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            fsynced_directory = True
        real_fsync(descriptor)

    def token_generation_forbidden(_: int) -> str:
        raise AssertionError("refresh must not generate or rotate the resident token")

    monkeypatch.setattr(MODULE.os, "replace", replace)
    monkeypatch.setattr(MODULE.os, "fsync", fsync)
    monkeypatch.setattr(MODULE.secrets, "token_urlsafe", token_generation_forbidden)

    assert MODULE.prepare_configuration(
        runtime=runtime,
        token_file=token,
        env_file=identity,
        manifest_resolver=lambda _runtime, _preprocessor: "c" * 64,
        refresh_identity=True,
    ) is True

    assert token.read_bytes() == token_value
    assert token.stat().st_ino == token_inode
    assert identity.read_text(encoding="ascii") == (
        "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256=" + "c" * 64 + "\n"
    )
    assert identity.stat().st_ino != old_identity_inode
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    assert len(replacements) == 1
    temporary, destination, mode = replacements[0]
    assert temporary.parent == identity.parent
    assert destination == identity
    assert mode == 0o600
    assert fsynced_directory is True


def test_refresh_identity_is_noop_when_manifest_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, token, identity = _create_configuration(tmp_path)
    token_value = token.read_bytes()
    identity_inode = identity.stat().st_ino

    def replace_forbidden(*_args: object) -> None:
        raise AssertionError("matching identity must not be replaced")

    monkeypatch.setattr(MODULE.os, "replace", replace_forbidden)
    assert MODULE.prepare_configuration(
        runtime=runtime,
        token_file=token,
        env_file=identity,
        manifest_resolver=_resolver,
        refresh_identity=True,
    ) is False
    assert token.read_bytes() == token_value
    assert identity.stat().st_ino == identity_inode


@pytest.mark.parametrize("unsafe_kind", ["mode", "hardlink", "symlink"])
def test_refresh_rejects_unsafe_existing_identity_before_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    runtime, token, identity = _create_configuration(tmp_path)
    token_value = token.read_bytes()
    if unsafe_kind == "mode":
        identity.chmod(0o640)
    elif unsafe_kind == "hardlink":
        os.link(identity, tmp_path / "identity-hardlink")
    else:
        target = tmp_path / "identity-target"
        identity.rename(target)
        identity.symlink_to(target)

    def replace_forbidden(*_args: object) -> None:
        raise AssertionError("unsafe identity must be rejected before replacement")

    monkeypatch.setattr(MODULE.os, "replace", replace_forbidden)
    with pytest.raises(MODULE.ResidentConfigurationError, match="private|paths"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=lambda _runtime, _preprocessor: "c" * 64,
            refresh_identity=True,
        )
    assert token.read_bytes() == token_value


def test_refresh_replace_failure_preserves_identity_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, token, identity = _create_configuration(tmp_path)
    token_value = token.read_bytes()
    identity_value = identity.read_bytes()

    def replace_failure(*_args: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(MODULE.os, "replace", replace_failure)
    with pytest.raises(MODULE.ResidentConfigurationError, match="refresh failed"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=lambda _runtime, _preprocessor: "c" * 64,
            refresh_identity=True,
        )
    assert token.read_bytes() == token_value
    assert identity.read_bytes() == identity_value
    assert list(tmp_path.glob(".model1-resident.env.refresh-*")) == []


def test_refresh_does_not_recover_partial_configuration(tmp_path: Path) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    token.write_text("a" * 48 + "\n", encoding="ascii")
    token.chmod(0o600)

    with pytest.raises(MODULE.ResidentConfigurationError, match="partially"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
            refresh_identity=True,
        )

    token.unlink()
    identity.write_text(
        "PREREVIEW_MODEL1_REMOTE_RUNTIME_MANIFEST_SHA256=" + "b" * 64 + "\n",
        encoding="ascii",
    )
    identity.chmod(0o600)
    with pytest.raises(MODULE.ResidentConfigurationError, match="partially"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=_resolver,
            refresh_identity=True,
        )
    assert not token.exists()


def test_refresh_rejects_malformed_private_identity(tmp_path: Path) -> None:
    runtime, token, identity = _create_configuration(tmp_path)
    token_value = token.read_bytes()
    identity.write_text("unexpected=value\n", encoding="ascii")
    identity.chmod(0o600)

    with pytest.raises(MODULE.ResidentConfigurationError, match="identity file is invalid"):
        MODULE.prepare_configuration(
            runtime=runtime,
            token_file=token,
            env_file=identity,
            manifest_resolver=lambda _runtime, _preprocessor: "c" * 64,
            refresh_identity=True,
        )
    assert token.read_bytes() == token_value
    assert identity.read_text(encoding="ascii") == "unexpected=value\n"


def test_cli_forwards_refresh_without_printing_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "model1"
    runtime.mkdir()
    token = tmp_path / "model1-service.token"
    identity = tmp_path / "model1-resident.env"
    captured: dict[str, object] = {}

    def prepare(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(MODULE, "prepare_configuration", prepare)
    assert MODULE.main(
        [
            "--runtime",
            str(runtime),
            "--token-file",
            str(token),
            "--env-file",
            str(identity),
            "--refresh-identity",
        ]
    ) == 0
    output = capsys.readouterr()
    assert captured["refresh_identity"] is True
    assert "a" * 48 not in output.out
    assert "b" * 64 not in output.out
    assert output.err == ""
