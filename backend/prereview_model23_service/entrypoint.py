"""Uvicorn entry point for the combined resident Model 2/3 service."""

from .app import create_app_from_environment

app = create_app_from_environment()
