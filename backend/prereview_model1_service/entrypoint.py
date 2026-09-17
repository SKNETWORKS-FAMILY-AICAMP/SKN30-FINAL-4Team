"""Uvicorn factory entry point; the launcher owns host/port/process identity."""

from .app import create_app_from_environment

app = create_app_from_environment()
