"""Shell-level safety checks for the persistent RunPod API foreground launcher."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prereview_runpod_worker" / "scripts" / "start_persistent_api.sh"
TOKEN = "a" * 64


def _run_shell(
    program: str,
    *arguments: str,
    environment: dict[str, str] | None = None,
    xtrace: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PREREVIEW_RUNPOD_LAUNCHER_TEST_MODE"] = "1"
    if environment:
        env.update(environment)
    bash_arguments = ["bash"]
    if xtrace:
        bash_arguments.append("-x")
    return subprocess.run(
        [*bash_arguments, "-c", program, "--", str(SCRIPT), *arguments],
        check=False,
        text=True,
        capture_output=True,
        env=env,
    )


def _token_file(tmp_path: Path, *, content: str = TOKEN, mode: int = 0o600) -> Path:
    path = tmp_path / "api-bearer-token"
    path.write_text(content, encoding="ascii")
    path.chmod(mode)
    return path


def test_launcher_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], check=False, text=True, capture_output=True
    )

    assert result.returncode == 0, result.stderr


def test_runtime_bearer_file_is_read_without_exporting_it_from_launcher_shell(
    tmp_path: Path,
) -> None:
    bearer_file = _token_file(tmp_path, content=f"{TOKEN}\n")
    result = _run_shell(
        'source "$1"; read_persistent_api_bearer_file "$2"; '
        '[[ "$PERSISTENT_API_BEARER_TOKEN" == "' + TOKEN + '" ]]; '
        '! export -p | grep -F PREREVIEW_SURYA_API_BEARER_TOKEN',
        str(bearer_file),
    )

    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


def test_inherited_xtrace_is_disabled_before_runtime_bearer_is_read(
    tmp_path: Path,
) -> None:
    bearer_file = _token_file(tmp_path, content=f"{TOKEN}\n")
    result = _run_shell(
        'source "$1"; set -x; read_persistent_api_bearer_file "$2"; '
        '[[ "$PERSISTENT_API_BEARER_TOKEN" == "' + TOKEN + '" ]]',
        str(bearer_file),
        xtrace=True,
    )

    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


@pytest.mark.parametrize(
    "prepare",
    [
        lambda path: path.chmod(0o644),
        lambda path: path.write_text("short\n", encoding="ascii"),
        lambda path: path.write_text(f"{TOKEN}\nextra\n", encoding="ascii"),
        lambda path: path.write_text(("$" * 64) + "\n", encoding="ascii"),
    ],
)
def test_invalid_bearer_file_fails_closed_without_echoing_its_contents(
    tmp_path: Path,
    prepare: object,
) -> None:
    bearer_file = _token_file(tmp_path, content=f"{TOKEN}\n")
    assert callable(prepare)
    prepare(bearer_file)
    secret = bearer_file.read_text(encoding="ascii").strip()

    result = _run_shell(
        'source "$1"; read_persistent_api_bearer_file "$2"', str(bearer_file)
    )

    assert result.returncode != 0
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert "persistent API bearer file is invalid" in result.stderr


def test_symlinked_bearer_file_is_rejected(tmp_path: Path) -> None:
    target = _token_file(tmp_path, content=f"{TOKEN}\n")
    link = tmp_path / "api-bearer-link"
    link.symlink_to(target)

    result = _run_shell('source "$1"; read_persistent_api_bearer_file "$2"', str(link))

    assert result.returncode != 0
    assert TOKEN not in result.stderr
    assert "persistent API bearer file is invalid" in result.stderr


def test_main_rejects_a_shell_exported_bearer_before_any_deployment_work() -> None:
    result = _run_shell(
        'source "$1"; main',
        environment={"PREREVIEW_SURYA_API_BEARER_TOKEN": TOKEN},
    )

    assert result.returncode != 0
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr
    assert "persistent API bearer must be supplied by its runtime file" in result.stderr


def test_journal_directory_requires_mountpoint_then_creates_exact_non_symlink_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_directory = workspace / "persistent" / "prereview" / "surya-jobs"
    result = _run_shell(
        'mountpoint() { [[ "$1" == "-q" && "$2" == "--" && "$3" == "$TEST_WORKSPACE" ]]; }; '
        'source "$1"; ensure_persistent_journal_directory "$2" "$3"; '
        '[[ "$PREREVIEW_SURYA_API_STATE_DIRECTORY" == "$3" ]]',
        str(workspace),
        str(state_directory),
        environment={"TEST_WORKSPACE": str(workspace)},
    )

    assert result.returncode == 0, result.stderr
    assert state_directory.is_dir()
    assert state_directory.resolve() == state_directory
    assert all(
        not path.is_symlink()
        for path in (
            workspace,
            workspace / "persistent",
            workspace / "persistent" / "prereview",
            state_directory,
        )
    )


def test_journal_directory_rejects_non_mount_before_creating_components(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_directory = workspace / "persistent" / "prereview" / "surya-jobs"
    result = _run_shell(
        'mountpoint() { return 1; }; source "$1"; '
        'ensure_persistent_journal_directory "$2" "$3"',
        str(workspace),
        str(state_directory),
    )

    assert result.returncode != 0
    assert not (workspace / "persistent").exists()
    assert "separate mountpoint" in result.stderr


def test_journal_directory_rejects_run_override_before_creating_components(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_directory = workspace / "persistent" / "prereview" / "surya-jobs"
    result = _run_shell(
        'mountpoint() { return 0; }; source "$1"; '
        'ensure_persistent_journal_directory "$2" "$3"',
        str(workspace),
        str(state_directory),
        environment={
            "PREREVIEW_SURYA_API_STATE_DIRECTORY": "/run/prereview-surya/surya-jobs"
        },
    )

    assert result.returncode != 0
    assert not (workspace / "persistent").exists()
    assert "override is invalid" in result.stderr


@pytest.mark.parametrize(
    "symlink_component", ("workspace", "persistent", "prereview", "surya-jobs")
)
def test_journal_directory_rejects_symlinked_path_components(
    tmp_path: Path,
    symlink_component: str,
) -> None:
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    if symlink_component == "workspace":
        workspace = tmp_path / "workspace"
        workspace.symlink_to(real_workspace, target_is_directory=True)
    else:
        workspace = real_workspace
        target = tmp_path / f"{symlink_component}-target"
        target.mkdir()
        if symlink_component == "persistent":
            (workspace / "persistent").symlink_to(target, target_is_directory=True)
        elif symlink_component == "prereview":
            (workspace / "persistent").mkdir()
            (workspace / "persistent" / "prereview").symlink_to(
                target,
                target_is_directory=True,
            )
        else:
            (workspace / "persistent" / "prereview").mkdir(parents=True)
            (workspace / "persistent" / "prereview" / "surya-jobs").symlink_to(
                target,
                target_is_directory=True,
            )
    state_directory = workspace / "persistent" / "prereview" / "surya-jobs"
    result = _run_shell(
        'mountpoint() { return 0; }; source "$1"; '
        'ensure_persistent_journal_directory "$2" "$3"',
        str(workspace),
        str(state_directory),
    )

    assert result.returncode != 0
    assert "non-symlink" in result.stderr
