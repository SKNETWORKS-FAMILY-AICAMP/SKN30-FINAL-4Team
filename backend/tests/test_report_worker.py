from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from worker.report_main import (
    REPORT_CLEANUP_INTERVAL_SECONDS,
    ReportWorkerConfigurationError,
    ReportWorkerSettings,
    _next_cleanup_deadline,
)
from worker.reporting.contracts import ReportPayloadV1
from worker.reporting.handler import ReportJobHandler
from worker.reporting.renderer import PersistentChromiumRenderer, ReportRenderError
from worker.runtime import ClaimedJob


FIXTURE = Path(__file__).parent / "fixtures" / "reporting" / "report_payload_v1_case01.json"
DETAIL_SCRIPT = (
    Path(__file__).parents[1]
    / "worker"
    / "reporting"
    / "templates"
    / "result_pdf_detail_preview.js"
)


class _Renderer:
    payload: ReportPayloadV1 | None = None

    def render(self, payload: ReportPayloadV1) -> bytes:
        self.payload = payload
        return b"%PDF-1.7\nfixture"


class _Storage:
    def __init__(self) -> None:
        self.puts: list[dict[str, object]] = []

    def put_if_absent(self, **kwargs: object) -> bool:
        self.puts.append(kwargs)
        return True


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _projection(fixture: dict[str, object]) -> dict[str, object]:
    return {
        key: fixture[key]
        for key in ("case", "cpl", "fit", "sim", "ml", "evidences")
    } | {
        "report": {
            "status": "generating",
            "can_download": False,
            "can_regenerate": False,
            "retry_count": 1,
        },
        "session": {
            "analysis_session_id": None,
            "is_active": False,
            "can_chat": False,
            "expires_at": None,
        },
    }


def test_report_payload_v1_accepts_real_analysis_fixture() -> None:
    payload = ReportPayloadV1.model_validate(_fixture())
    assert payload.schema_version == "report_payload_v1"
    assert len(payload.sim_details) == len(payload.sim.candidates) == 5


def test_production_template_has_no_mock_fallback_and_allows_only_http_links() -> None:
    script = DETAIL_SCRIPT.read_text(encoding="utf-8")
    assert "example.com/notices/sample" not in script
    assert "Invalid report payload" in script
    assert "url.protocol === 'http:'" in script
    assert "url.protocol === 'https:'" in script


def test_production_template_maps_internal_cpl_fields_to_korean_labels() -> None:
    from worker.result_payload import _CPL_FIELD_LABELS

    script = DETAIL_SCRIPT.read_text(encoding="utf-8")
    for field_name, korean_label in _CPL_FIELD_LABELS.items():
        assert field_name in script
        assert korean_label in script
    assert "? '확인 항목' : raw || '항목'" in script


def test_handler_renders_and_uploads_content_addressed_pdf() -> None:
    fixture = _fixture()
    processing_run_id = uuid4()
    owner_id = uuid4()
    renderer = _Renderer()
    storage = _Storage()
    result = ReportJobHandler(renderer=renderer, storage=storage).handle(
        ClaimedJob(
            job_pk=uuid4(),
            processing_run_pk=processing_run_id,
            payload={
                "result_payload": _projection(fixture),
                "sim_details": fixture["sim_details"],
                "owner_id": owner_id,
                "analysis_case_id": fixture["case"]["analysis_case_id"],  # type: ignore[index]
                "storage_object_key": f"{owner_id}/{fixture['case']['analysis_case_id']}/pdf/{processing_run_id}.pdf",  # type: ignore[index]
                "source_analysis_run_id": fixture["report_metadata"]["source_analysis_run_id"],  # type: ignore[index]
            },
        )
    )
    assert renderer.payload is not None
    assert result["mime_type"] == "application/pdf"
    assert str(result["storage_object_key"]).startswith(f"{owner_id}/{fixture['case']['analysis_case_id']}/pdf/")  # type: ignore[index]
    assert storage.puts[0]["content"] == b"%PDF-1.7\nfixture"


def test_handler_rejects_a_pdf_larger_than_the_download_limit() -> None:
    fixture = _fixture()
    renderer = _Renderer()
    storage = _Storage()
    with pytest.raises(ValueError, match="byte limit"):
        ReportJobHandler(
            renderer=renderer, storage=storage, max_pdf_bytes=8
        ).handle(
            ClaimedJob(
                job_pk=uuid4(),
                processing_run_pk=uuid4(),
                payload={
                    "owner_id": uuid4(),
                    "analysis_case_id": fixture["case"]["analysis_case_id"],
                    "result_payload": _projection(fixture),
                    "sim_details": fixture["sim_details"],
                    "source_analysis_run_id": fixture["report_metadata"]["source_analysis_run_id"],
                },
            )
        )
    assert storage.puts == []


def test_handler_rejects_a_projection_for_another_claimed_case() -> None:
    fixture = _fixture()
    renderer = _Renderer()
    with pytest.raises(ValueError, match="does not match"):
        ReportJobHandler(renderer=renderer, storage=_Storage()).handle(
            ClaimedJob(
                job_pk=uuid4(),
                processing_run_pk=uuid4(),
                payload={
                    "owner_id": uuid4(),
                    "analysis_case_id": str(uuid4()),
                    "result_payload": _projection(fixture),
                    "sim_details": fixture["sim_details"],
                    "source_analysis_run_id": fixture["report_metadata"]["source_analysis_run_id"],
                },
            )
        )
    assert renderer.payload is None


def test_payload_rejects_missing_sim_detail() -> None:
    fixture = _fixture()
    with pytest.raises(ValueError, match="SIM detail set"):
        ReportPayloadV1.from_projection(
            result_payload=_projection(fixture),
            sim_details=fixture["sim_details"][:-1],  # type: ignore[index]
        )


def test_renderer_discards_a_crashed_browser_before_retry(tmp_path, monkeypatch) -> None:
    class CrashedBrowser:
        closed = False

        def new_context(self, **_kwargs):
            raise RuntimeError("browser process exited")

        def close(self) -> None:
            self.closed = True

    class Playwright:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    renderer = PersistentChromiumRenderer(template_dir=DETAIL_SCRIPT.parent)
    browser = CrashedBrowser()
    playwright = Playwright()
    renderer._browser = browser
    renderer._playwright = playwright
    monkeypatch.setattr(renderer, "start", lambda: None)

    with pytest.raises(ReportRenderError, match="rendering failed"):
        renderer.render(ReportPayloadV1.model_validate(_fixture()))

    assert browser.closed is True
    assert playwright.stopped is True
    assert renderer._browser is None
    assert renderer._playwright is None


def test_report_worker_settings_require_a_shorter_heartbeat() -> None:
    env = {
        "DATABASE_URL": "postgresql://example.invalid/db",
        "SUPABASE_URL": "http://supabase.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
        "PREREVIEW_REPORT_WORKER_LEASE_SECONDS": "30",
        "PREREVIEW_REPORT_WORKER_HEARTBEAT_SECONDS": "30",
    }
    with pytest.raises(ReportWorkerConfigurationError, match="shorter"):
        ReportWorkerSettings.from_env(env)


def test_report_worker_rejects_a_limit_above_the_storage_contract() -> None:
    env = {
        "DATABASE_URL": "postgresql://example.invalid/db",
        "SUPABASE_URL": "http://supabase.invalid",
        "SUPABASE_SERVICE_ROLE_KEY": "secret",
        "PREREVIEW_REPORT_MAX_BYTES": str(25 * 1024 * 1024 + 1),
    }
    with pytest.raises(ReportWorkerConfigurationError, match="must not exceed"):
        ReportWorkerSettings.from_env(env)


def test_report_worker_deployment_is_hardened_and_cleanup_is_throttled() -> None:
    backend_root = Path(__file__).parents[1]
    compose = (backend_root / "compose.yaml").read_text(encoding="utf-8")
    report_section = compose.split("\n  report-worker:\n", 1)[1]
    dockerfile = (backend_root / "Dockerfile.report-worker").read_text(encoding="utf-8")

    assert 'command: ["python", "-m", "worker.report_main"]' in report_section
    assert "ports:" not in report_section
    assert "read_only: true" in report_section
    assert "no-new-privileges:true" in report_section
    assert "cap_drop:\n      - ALL" in report_section
    assert "mem_limit:" in report_section
    assert "USER 10001:10001" in dockerfile


def test_report_cleanup_drains_backlog_and_backs_off_after_empty_sweep() -> None:
    now = 1234.5
    assert _next_cleanup_deadline(now=now, cleaned=True) == now
    assert _next_cleanup_deadline(now=now, cleaned=False) == (
        now + REPORT_CLEANUP_INTERVAL_SECONDS
    )
