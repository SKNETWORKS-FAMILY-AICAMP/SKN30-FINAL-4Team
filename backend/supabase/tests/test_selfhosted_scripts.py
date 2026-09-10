from __future__ import annotations

import os
from pathlib import Path
import shlex
import stat
import subprocess

import pytest


SUPABASE_ROOT = Path(__file__).resolve().parents[1]
PREPARE = SUPABASE_ROOT / "prepare_selfhosted.sh"
INSTALL = SUPABASE_ROOT / "install_selfhosted_local.sh"


def _write_config(
    path: Path,
    *,
    compose_dir: Path,
    database_dir: Path,
    storage_dir: str = "",
) -> None:
    path.write_text(
        "\n".join(
            (
                f"SUPABASE_COMPOSE_DIR={shlex.quote(str(compose_dir))}",
                f"SUPABASE_DB_DATA_DIR={shlex.quote(str(database_dir))}",
                f"SUPABASE_STORAGE_DATA_DIR={shlex.quote(storage_dir)}",
                "",
            )
        ),
        encoding="utf-8",
    )


def _fake_compose(tmp_path: Path) -> Path:
    compose_dir = tmp_path / "supabase-compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return compose_dir


def _run_prepare(config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PREPARE), str(config)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_selfhosted_shell_scripts_have_valid_syntax() -> None:
    for script in (PREPARE, INSTALL):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_prepare_preserves_existing_data_directory_modes(tmp_path: Path) -> None:
    compose_dir = _fake_compose(tmp_path)
    database_dir = tmp_path / "persistent" / "postgres"
    storage_dir = tmp_path / "persistent" / "storage"
    database_dir.mkdir(parents=True)
    storage_dir.mkdir()
    (database_dir / "sentinel").write_text("db", encoding="utf-8")
    (storage_dir / "sentinel").write_text("storage", encoding="utf-8")
    database_dir.chmod(0o710)
    storage_dir.chmod(0o771)
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=database_dir,
        storage_dir=str(storage_dir),
    )

    result = _run_prepare(config)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(database_dir.stat().st_mode) == 0o710
    assert stat.S_IMODE(storage_dir.stat().st_mode) == 0o771
    linked = compose_dir / "volumes" / "db" / "data"
    assert linked.is_symlink()
    assert linked.resolve() == database_dir.resolve()


@pytest.mark.parametrize("unsafe_storage", ["/", "/tmp"])
def test_prepare_rejects_broad_storage_paths(
    tmp_path: Path, unsafe_storage: str
) -> None:
    compose_dir = _fake_compose(tmp_path)
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=tmp_path / "persistent" / "postgres",
        storage_dir=unsafe_storage,
    )

    result = _run_prepare(config)

    assert result.returncode != 0
    assert "refusing broad SUPABASE_STORAGE_DATA_DIR" in result.stderr


def test_prepare_rejects_compose_root_as_storage(tmp_path: Path) -> None:
    compose_dir = _fake_compose(tmp_path)
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=tmp_path / "persistent" / "postgres",
        storage_dir=str(compose_dir),
    )

    result = _run_prepare(config)

    assert result.returncode != 0
    assert "refusing a path overlapping the Supabase Compose tree" in result.stderr


def test_prepare_rejects_compose_local_db_path_as_external_data(
    tmp_path: Path,
) -> None:
    compose_dir = _fake_compose(tmp_path)
    local_db_path = compose_dir / "volumes" / "db" / "data"
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=local_db_path,
    )

    result = _run_prepare(config)

    assert result.returncode != 0
    assert "refusing a path overlapping the Supabase Compose tree" in result.stderr
    assert not local_db_path.is_symlink()


@pytest.mark.parametrize("storage_is_parent", [False, True])
def test_prepare_rejects_overlapping_db_and_storage_paths(
    tmp_path: Path, storage_is_parent: bool
) -> None:
    compose_dir = _fake_compose(tmp_path)
    persistent_root = tmp_path / "persistent"
    database_dir = (
        persistent_root / "postgres" if storage_is_parent else persistent_root
    )
    storage_dir = (
        persistent_root if storage_is_parent else persistent_root / "storage"
    )
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=database_dir,
        storage_dir=str(storage_dir),
    )

    result = _run_prepare(config)

    assert result.returncode != 0
    assert "DB and Storage data directories must not overlap" in result.stderr
    assert not database_dir.exists()
    assert not storage_dir.exists()


def test_prepare_rejects_repository_path_outside_runtime(tmp_path: Path) -> None:
    compose_dir = _fake_compose(tmp_path)
    repository_root = SUPABASE_ROOT.parents[1]
    unsafe_database_dir = repository_root / "backend" / ".unsafe-db-test-path"
    config = tmp_path / "paths.env"
    _write_config(
        config,
        compose_dir=compose_dir,
        database_dir=unsafe_database_dir,
    )

    result = _run_prepare(config)

    assert result.returncode != 0
    assert "repository data paths must be below .runtime" in result.stderr
    assert not unsafe_database_dir.exists()


def test_local_installer_is_pinned_and_prepare_only() -> None:
    source = INSTALL.read_text(encoding="utf-8")

    assert 'PINNED_SUPABASE_REF="self-hosted/v0.8.0"' in source
    assert (
        'PINNED_SUPABASE_COMMIT="e1af732589cd468edb49500ebc04e4367d4c56ad"'
        in source
    )
    assert 'OFFICIAL_REPOSITORY_URL="https://github.com/supabase/supabase.git"' in source
    assert "--filter=blob:none" in source
    assert "--sparse" in source
    assert "sparse-checkout set docker" in source
    commit_check = '[[ "$RESOLVED_COMMIT" == "$PINNED_SUPABASE_COMMIT" ]]'
    assert commit_check in source
    assert source.index(commit_check) < source.index("utils/generate-keys.sh")
    assert "utils/generate-keys.sh --update-env >/dev/null 2>&1" in source
    assert "utils/add-new-auth-keys.sh --update-env >/dev/null 2>&1" in source
    assert '"$STAGING_DIR/bundle/.env.old"' in source
    assert "docker-compose.pgvector.override.yml.example" in source
    assert "config --quiet >/dev/null 2>&1" in source
    assert "refusing a non-empty target" in source
    assert "repository targets must be below .runtime" in source
    assert 'mv -T -- "$STAGING_DIR/bundle" "$TARGET_DIR"' in source
    for forbidden in ("docker compose up", "docker compose down", "reset.sh", "down -v"):
        assert forbidden not in source


def test_local_installer_help_does_not_touch_external_state() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )

    assert result.returncode == 0
    assert "does not start containers" in result.stdout


@pytest.mark.parametrize(
    ("target", "expected_error"),
    [
        ("relative-target", "--target must be an absolute path"),
        ("/", "refusing a broad filesystem target"),
        ("/tmp", "refusing a broad filesystem target"),
    ],
)
def test_local_installer_rejects_unsafe_targets_before_external_work(
    target: str, expected_error: str
) -> None:
    result = subprocess.run(
        ["bash", str(INSTALL), "--target", target],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_local_installer_rejects_existing_nonempty_target(tmp_path: Path) -> None:
    target = tmp_path / "already-used"
    target.mkdir()
    (target / "sentinel").write_text("keep", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(INSTALL), "--target", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )

    assert result.returncode != 0
    assert "refusing a non-empty target" in result.stderr
    assert (target / "sentinel").read_text(encoding="utf-8") == "keep"
