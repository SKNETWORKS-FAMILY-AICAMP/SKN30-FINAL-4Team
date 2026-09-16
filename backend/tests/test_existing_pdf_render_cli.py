from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "render_existing_pdf_pages.py"
SPEC = importlib.util.spec_from_file_location("render_existing_pdf_pages", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _manifest(source: bytes) -> dict:
    image = b"\x89PNG\r\n\x1a\n"
    return {
        "schema_version": "pdf_render_manifest/v1",
        "source_pdf_relative_path": "source.pdf",
        "source_pdf_sha256": __import__("hashlib").sha256(source).hexdigest(),
        "source_pdf_size_bytes": len(source),
        "page_count": 1,
        "renderer": "pypdfium2",
        "renderer_version": "5.13.0",
        "renderer_config_sha256": "0" * 64,
        "pages": [{
            "page": 1,
            "image_relative_path": "rendered/page-0001.png",
            "image_mime_type": "image/png",
            "image_sha256": __import__("hashlib").sha256(image).hexdigest(),
            "image_size_bytes": len(image),
            "coordinate_manifest": {
                "rendered_width_px": 1,
                "rendered_height_px": 1,
            },
            "coordinate_manifest_sha256": "2" * 64,
        }],
    }


def _install_fake_renderer(monkeypatch: pytest.MonkeyPatch, *, mode: str = "normal") -> list[tuple[list[str], Path, dict[str, str]]]:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def validate(payload, *, artifact_root):
        assert artifact_root.name.startswith(".existing-pdf-render-")
        assert payload["source_pdf_relative_path"] == "source.pdf"
        return payload

    api = SimpleNamespace(
        DEFAULT_LIMITS=SimpleNamespace(
            max_source_bytes=1024 * 1024,
            max_pages=4,
            max_png_file_bytes=1024 * 1024,
            max_png_dimension_px=100,
            max_png_total_pixels=10_000,
            max_document_total_pixels=20_000,
        ),
        PdfRenderManifestError=ValueError,
        validate_render_manifest_files=validate,
    )

    def run(command, *, cwd, timeout_seconds, environment=None):
        command = list(command)
        calls.append((command, cwd, dict(environment or {})))
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        source = (cwd / "source.pdf").read_bytes()
        rendered = cwd / "rendered"
        rendered.mkdir()
        (rendered / "page-0001.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        payload = _manifest(source)
        if mode == "duplicate":
            (cwd / "render_manifest.json").write_bytes(b'{"pages":[],"pages":[]}')
        elif mode == "symlink":
            external = cwd.parent / "external.json"
            external.write_text(json.dumps(payload))
            (cwd / "render_manifest.json").symlink_to(external)
        elif mode == "mutate-source":
            (cwd / "source.pdf").write_bytes(b"%PDF-1.7\nchanged\n")
            (cwd / "render_manifest.json").write_text(json.dumps(payload))
        else:
            (cwd / "render_manifest.json").write_text(json.dumps(payload))
        return "", ""

    monkeypatch.setattr(MODULE, "_load_renderer_api", lambda: api)
    monkeypatch.setattr(MODULE, "_run_command", run)
    return calls


def test_render_stages_and_publishes_terminal_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "notice.pdf"
    source_bytes = b"%PDF-1.7\nfixture\n"
    source.write_bytes(source_bytes)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("DATABASE_URL", "must-not-cross")
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")
    calls = _install_fake_renderer(monkeypatch)

    output = tmp_path / "output"
    returned = MODULE.render_existing_pdf_pages(source_pdf=source, output_directory=output)

    assert returned["source_pdf_relative_path"] == "source.pdf"
    assert (output / "source.pdf").read_bytes() == source_bytes
    assert (output / "rendered" / "page-0001.png").read_bytes().startswith(b"\x89PNG")
    assert json.loads((output / "render_manifest.json").read_text()) == returned
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "rendered").stat().st_mode) == 0o700
    for artifact in (output / "source.pdf", output / "rendered" / "page-0001.png", output / "render_manifest.json"):
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    command, cwd, environment = calls[0]
    assert command == [os.sys.executable, "-m", MODULE.RENDERER_MODULE, "--artifact-root", str(cwd), "--pdf", "source.pdf"]
    assert "OPENAI_API_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert "PYTHONPATH" not in environment


@pytest.mark.parametrize("mode, expected", [("duplicate", "duplicate JSON key"), ("symlink", "cannot read render_manifest"), ("mutate-source", "changed after rendering")])
def test_renderer_output_is_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, expected: str) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    _install_fake_renderer(monkeypatch, mode=mode)
    output = tmp_path / "output"

    with pytest.raises(MODULE.ExistingPdfRenderError, match=expected):
        MODULE.render_existing_pdf_pages(source_pdf=source, output_directory=output)
    assert not output.exists()


def test_input_symlink_fifo_and_existing_output_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_renderer(monkeypatch)
    actual = tmp_path / "actual.pdf"
    actual.write_bytes(b"%PDF-1.7\nfixture\n")
    link = tmp_path / "link.pdf"
    link.symlink_to(actual)
    with pytest.raises(MODULE.ExistingPdfRenderError, match="non-symlink"):
        MODULE.render_existing_pdf_pages(source_pdf=link, output_directory=tmp_path / "from-link")

    fifo = tmp_path / "pipe.pdf"
    os.mkfifo(fifo)
    with pytest.raises(MODULE.ExistingPdfRenderError, match="regular non-symlink"):
        MODULE.render_existing_pdf_pages(source_pdf=fifo, output_directory=tmp_path / "from-fifo")

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "operator-data"
    marker.write_text("keep")
    with pytest.raises(MODULE.ExistingPdfRenderError, match="never overwrites"):
        MODULE.render_existing_pdf_pages(source_pdf=actual, output_directory=existing)
    assert marker.read_text() == "keep"


def test_publish_rejects_staged_file_mutation_before_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    _install_fake_renderer(monkeypatch)
    original_copy = MODULE._copy_regular_verified
    calls = 0

    def race(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            source_fd = kwargs["source_fd"]
            replacement = os.open(
                ".replacement",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=source_fd,
            )
            os.write(replacement, b"replaced")
            os.close(replacement)
            os.rename(
                ".replacement",
                args[0],
                src_dir_fd=source_fd,
                dst_dir_fd=source_fd,
            )
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(MODULE, "_copy_regular_verified", race)
    output = tmp_path / "output"
    with pytest.raises(MODULE.ExistingPdfRenderError, match="changed (before|during) publication"):
        MODULE.render_existing_pdf_pages(source_pdf=source, output_directory=output)
    assert output.exists()
    assert not (output / "render_manifest.json").exists()


def test_publish_rejects_output_root_rename_before_terminal_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    _install_fake_renderer(monkeypatch)
    original_assert = MODULE._assert_directory_binding
    raced = False

    def race(name, *, parent_fd, directory_fd, label):
        nonlocal raced
        if not raced:
            raced = True
            os.rename(name, "moved-output", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        return original_assert(
            name,
            parent_fd=parent_fd,
            directory_fd=directory_fd,
            label=label,
        )

    monkeypatch.setattr(MODULE, "_assert_directory_binding", race)
    output = tmp_path / "output"
    with pytest.raises(MODULE.ExistingPdfRenderError, match="was replaced"):
        MODULE.render_existing_pdf_pages(source_pdf=source, output_directory=output)
    assert not (output / "render_manifest.json").exists()
    assert not (tmp_path / "moved-output" / "render_manifest.json").exists()


def test_publish_rejects_rendered_directory_rename_before_terminal_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "notice.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture\n")
    _install_fake_renderer(monkeypatch)
    original_assert = MODULE._assert_directory_binding
    raced = False

    def race(name, *, parent_fd, directory_fd, label):
        nonlocal raced
        if label == "published rendered directory" and not raced:
            raced = True
            os.rename(name, "moved-rendered", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        return original_assert(
            name,
            parent_fd=parent_fd,
            directory_fd=directory_fd,
            label=label,
        )

    monkeypatch.setattr(MODULE, "_assert_directory_binding", race)
    output = tmp_path / "output"
    with pytest.raises(MODULE.ExistingPdfRenderError, match="rendered directory was replaced"):
        MODULE.render_existing_pdf_pages(source_pdf=source, output_directory=output)
    assert not (output / "render_manifest.json").exists()
    assert not (output / "moved-rendered" / "render_manifest.json").exists()


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("pages", "page limit"),
        ("pixels", "document pixel limit"),
        ("bytes", "document image-byte limit"),
    ],
)
def test_parent_rejects_manifest_resource_caps_before_file_validation(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    source = b"%PDF-1.7\nfixture\n"
    payload = _manifest(source)
    page = payload["pages"][0]
    if mutation == "pages":
        payload["pages"] = [dict(page), dict(page)]
        payload["page_count"] = 2
    elif mutation == "pixels":
        page["coordinate_manifest"] = {
            "rendered_width_px": 100,
            "rendered_height_px": 100,
        }
    else:
        page["image_size_bytes"] = MODULE.MAX_DOCUMENT_RENDER_BYTES + 1
    called = False

    def validate(*_args, **_kwargs):
        nonlocal called
        called = True

    api = SimpleNamespace(
        DEFAULT_LIMITS=SimpleNamespace(
            max_pages=1,
            max_png_file_bytes=MODULE.MAX_DOCUMENT_RENDER_BYTES * 2,
            max_png_dimension_px=1_000,
            max_png_total_pixels=1_000_000,
            max_document_total_pixels=5_000,
        ),
        validate_render_manifest_files=validate,
    )
    with pytest.raises(MODULE.ExistingPdfRenderError, match=expected):
        MODULE._validate_manifest(
            payload,
            staging=tmp_path,
            source_sha256=payload["source_pdf_sha256"],
            source_size=payload["source_pdf_size_bytes"],
            renderer_api=api,
        )
    assert not called


def test_deep_manifest_json_is_normalized_to_contract_error(tmp_path: Path) -> None:
    path = tmp_path / "render_manifest.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(MODULE.ExistingPdfRenderError, match="cannot read render_manifest"):
        MODULE._json_object(path, max_bytes=MODULE.MAX_MANIFEST_BYTES)


def test_renderer_command_uses_external_prlimit_without_preexec() -> None:
    command = MODULE._resource_limited_command(
        ["/fixed/python", "-m", MODULE.RENDERER_MODULE],
        timeout_seconds=7,
    )
    assert command[0] == str(MODULE.PRLIMIT_PATH)
    for option in ("--as=", "--data=", "--fsize=", "--cpu=", "--nofile=", "--core=0:0"):
        assert any(argument.startswith(option) for argument in command)
    assert command[-4:] == ["--", "/fixed/python", "-m", MODULE.RENDERER_MODULE]


def test_timeout_is_bounded_and_reaps_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, timeout=None):
            if timeout is not None:
                raise __import__("subprocess").TimeoutExpired("fake", timeout)
            return "", ""

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(
        MODULE,
        "_resource_limited_command",
        lambda command, *, timeout_seconds: list(command),
    )
    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    killed: list[int] = []
    monkeypatch.setattr(MODULE.os, "killpg", lambda pid, _signal: killed.append(pid))
    with pytest.raises(MODULE.ExistingPdfRenderError, match="exceeded"):
        MODULE._run_command(["fake"], cwd=tmp_path, timeout_seconds=1)
    assert killed == [123]
