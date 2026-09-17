"""Loopback-only Uvicorn launcher for a persistent RunPod."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version as distribution_version
from typing import Any

from .app import create_persistent_app
from .settings import load_persistent_api_settings
from .store import DurableJobStore
from ..surya_layout_worker.settings import build_worker_composition, load_worker_settings

__all__ = ["main", "serve_persistent_app"]


def main() -> None:
    # Imports happen here so contract tests can import the package without an
    # installed Uvicorn.  Production setup pins both versions explicitly.
    if distribution_version("fastapi") != "0.115.14":
        raise RuntimeError("persistent Surya API runtime unavailable")
    if distribution_version("uvicorn") != "0.52.4":
        raise RuntimeError("persistent Surya API runtime unavailable")
    settings = load_persistent_api_settings()
    composition = build_worker_composition(load_worker_settings())
    store = DurableJobStore(
        settings.state_directory,
        max_records=settings.max_job_records,
        mac_key=settings.journal_mac_key,
    )
    app = create_persistent_app(
        handler=composition.handler,
        settings=settings,
        store=store,
    )
    serve_persistent_app(app, settings=settings)


def serve_persistent_app(
    app: object,
    *,
    settings: object,
    runner: Callable[..., Any] | None = None,
) -> None:
    """Launch exactly one loopback worker; Tailscale Serve owns ingress."""

    import uvicorn

    selected_runner = uvicorn.run if runner is None else runner
    selected_runner(
        app,
        host="127.0.0.1",
        port=settings.port,  # type: ignore[attr-defined]
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":  # pragma: no cover - operator boundary
    main()
