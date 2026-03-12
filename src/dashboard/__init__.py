"""FastAPI + HTMX dashboard for Telegram Digest."""

from .app import create_app, run_dashboard

__all__ = ["create_app", "run_dashboard"]
