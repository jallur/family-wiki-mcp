"""Uvicorn-ingång: app.main:app."""
from __future__ import annotations

from .server import create_app_from_env

app = create_app_from_env()
