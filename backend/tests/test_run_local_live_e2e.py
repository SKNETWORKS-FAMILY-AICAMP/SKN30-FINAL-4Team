from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_local_live_e2e.py"
SPEC = importlib.util.spec_from_file_location("run_local_live_e2e", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_trace_writes_actual_stage_layout(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    result = {"cpl": {"items": []}, "fit": {"items": []}, "sim": {}, "ml": {}}

    MODULE._write_trace(
        trace_dir,
        upload={"analysis_run_id": "run-1"},
        common_ir={"schema_version": "common_ir_v1"},
        structured_profile={"profile_id": "request:run-1"},
        result=result,
    )

    assert [path.name for path in sorted(trace_dir.iterdir())] == [
        "00_upload.json",
        "01_common_ir.json",
        "02_structured_profile.json",
        "03_cpl.json",
        "04_fit.json",
        "05_sim.json",
        "06_ml.json",
        "07_result.json",
    ]
    assert json.loads((trace_dir / "02_structured_profile.json").read_text("utf-8"))[
        "profile_id"
    ] == "request:run-1"
    assert json.loads((trace_dir / "03_cpl.json").read_text("utf-8")) == {"items": []}


def test_trace_never_overwrites_existing_directory(tmp_path: Path) -> None:
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()

    with pytest.raises(MODULE.E2EFailure, match="must not already exist"):
        MODULE._write_trace(
            trace_dir,
            upload={},
            common_ir={},
            structured_profile={},
            result={},
        )


@pytest.mark.parametrize(
    ("filename", "content", "mime_type"),
    [
        ("request.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fixture", "application/x-hwp"),
        ("request.hwpx", b"PK\x03\x04fixture", "application/vnd.hancom.hwpx"),
    ],
)
def test_validated_source_accepts_hwp_and_hwpx(
    tmp_path: Path, filename: str, content: bytes, mime_type: str
) -> None:
    source = tmp_path / filename
    source.write_bytes(content)

    uploaded, detected_mime_type = MODULE._validated_source(source)

    assert uploaded == content
    assert detected_mime_type == mime_type


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("request.hwp", b"PK\x03\x04not-ole"),
        ("request.hwpx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1not-zip"),
        ("request.pdf", b"%PDF-1.7"),
    ],
)
def test_validated_source_rejects_wrong_magic_or_extension(
    tmp_path: Path, filename: str, content: bytes
) -> None:
    source = tmp_path / filename
    source.write_bytes(content)

    with pytest.raises(MODULE.E2EFailure, match="format is invalid"):
        MODULE._validated_source(source)


@pytest.mark.parametrize("top_k", [0, -1, 101])
def test_configure_environment_rejects_out_of_range_top_k(
    tmp_path: Path, top_k: int
) -> None:
    with pytest.raises(MODULE.E2EFailure, match="between 1 and 100"):
        MODULE._configure_environment(
            tmp_path / "backend.env", tmp_path / "supabase.env", top_k=top_k
        )


def test_top_k_defaults_to_the_stored_trace_baseline() -> None:
    assert MODULE.DEFAULT_TOP_K == 5


def test_windows_uses_the_direct_postgres_listener() -> None:
    assert MODULE._database_endpoint("acme", "win32") == ("postgres", 55432)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_keep_the_supabase_pooler(platform: str) -> None:
    assert MODULE._database_endpoint("acme", platform) == ("postgres.acme", 5432)


def test_pooler_username_is_url_quoted() -> None:
    username, port = MODULE._database_endpoint("a/c me", "linux")
    assert username == "postgres.a%2Fc%20me"
    assert port == 5432


@pytest.fixture
def isolated_environ(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """``_configure_environment`` writes straight into ``os.environ``.

    Hand it a copy so a test's fake API key never leaks into the rest of the
    session.
    """

    copy = dict(os.environ)
    monkeypatch.setattr(os, "environ", copy)
    return copy


def _env_files(tmp_path: Path, model1_line: str) -> tuple[Path, Path]:
    backend_env = tmp_path / "backend.env"
    backend_env.write_text(
        "OPENAI_API_KEY=sk-test-not-a-real-key\n" + model1_line, encoding="utf-8"
    )
    supabase_env = tmp_path / "supabase.env"
    supabase_env.write_text(
        "POSTGRES_PASSWORD=pw\nPOOLER_TENANT_ID=acme\n"
        "JWT_SECRET=s\nANON_KEY=a\nSERVICE_ROLE_KEY=r\n",
        encoding="utf-8",
    )
    return backend_env, supabase_env


def test_model1_serving_dir_reaches_the_worker_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = r"C:\models\model1\serving"
    backend_env, supabase_env = _env_files(
        tmp_path, f"PREREVIEW_MODEL1_SERVING_DIR={served}\n"
    )
    monkeypatch.delenv("PREREVIEW_MODEL1_SERVING_DIR", raising=False)

    MODULE._configure_environment(backend_env, supabase_env)

    # The Windows path must survive verbatim: no separator or case rewriting.
    assert os.environ["PREREVIEW_MODEL1_SERVING_DIR"] == served


@pytest.mark.parametrize("line", ["", "PREREVIEW_MODEL1_SERVING_DIR=   \n"])
def test_a_blank_model1_serving_dir_never_overwrites_the_caller(
    tmp_path: Path, isolated_environ: dict[str, str], line: str
) -> None:
    backend_env, supabase_env = _env_files(tmp_path, line)
    isolated_environ["PREREVIEW_MODEL1_SERVING_DIR"] = "/already/exported"

    MODULE._configure_environment(backend_env, supabase_env)

    assert isolated_environ["PREREVIEW_MODEL1_SERVING_DIR"] == "/already/exported"


def test_configure_environment_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    isolated_environ: dict[str, str],
) -> None:
    served = r"C:\models\model1\serving"
    backend_env, supabase_env = _env_files(
        tmp_path, f"PREREVIEW_MODEL1_SERVING_DIR={served}\n"
    )

    MODULE._configure_environment(backend_env, supabase_env)

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
