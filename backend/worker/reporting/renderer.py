"""Resident Chromium renderer for deterministic, offline report PDFs."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ReportPayloadV1


class ReportRenderError(RuntimeError):
    """The HTML report could not be rendered safely."""


class PersistentChromiumRenderer:
    """Launch Chromium once and create an isolated page for each report."""

    def __init__(
        self,
        *,
        template_dir: Path,
        executable_path: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._template_dir = template_dir.resolve()
        self._executable_path = executable_path
        self._timeout_ms = int(timeout_seconds * 1000)
        self._playwright: Any = None
        self._browser: Any = None

    def start(self) -> None:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return
            except Exception:
                pass
            self.close()
        if not (self._template_dir / "report.html").is_file():
            raise ReportRenderError("report template is missing")
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if self._executable_path:
                launch_options["executable_path"] = self._executable_path
            self._browser = self._playwright.chromium.launch(**launch_options)
        except Exception as exc:
            self.close()
            raise ReportRenderError("Chromium failed to start") from exc

    def close(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        finally:
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass

    def render(self, payload: ReportPayloadV1) -> bytes:
        self.start()
        assert self._browser is not None
        with tempfile.TemporaryDirectory(prefix="prereview-report-") as temp:
            workdir = Path(temp)
            shutil.copytree(self._template_dir, workdir, dirs_exist_ok=True)
            data = payload.model_dump_json(exclude_none=False)
            (workdir / "report_payload.js").write_text(
                f"window.REPORT_PREVIEW_DATA = {data};\n", encoding="utf-8"
            )
            context: Any = None
            page: Any = None
            try:
                context = self._browser.new_context(locale="ko-KR", offline=True)
                page = context.new_page()
                page.route(
                    "**/*",
                    lambda route: route.continue_()
                    if route.request.url.startswith(("file:", "data:", "blob:"))
                    else route.abort(),
                )
                page.set_default_timeout(self._timeout_ms)
                page.goto((workdir / "report.html").as_uri(), wait_until="load")
                page.wait_for_function(
                    "document.querySelectorAll('.report-detail-section').length >= 4"
                )
                pdf = page.pdf(
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )
            except Exception as exc:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                self.close()
                raise ReportRenderError("HTML report rendering failed") from exc
            try:
                context.close()
            except Exception:
                self.close()
            return pdf

    def __enter__(self) -> "PersistentChromiumRenderer":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
