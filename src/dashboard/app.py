"""FastAPI application for the dashboard."""

import asyncio
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

import structlog
import uvicorn
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import get_config
from ..store import Store
from ..vectors import VectorStore
from ..scheduler import Scheduler

log = structlog.get_logger()

# Template directory
TEMPLATES_DIR = Path(__file__).parent / "templates"

# HTTP Basic auth
_security = HTTPBasic(auto_error=False)


def _get_auth_token() -> Optional[str]:
    """Get the dashboard auth token from environment."""
    return os.environ.get("DASHBOARD_TOKEN")


async def _verify_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_security)) -> None:
    """Verify dashboard authentication. Skipped if DASHBOARD_TOKEN is not set."""
    token = _get_auth_token()
    if not token:
        # No token configured — allow access (localhost-only trust model)
        return

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Username is ignored; password must match the token
    if not secrets.compare_digest(credentials.password.encode(), token.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def create_app(
    store: Store,
    vector_store: VectorStore,
    scheduler: Scheduler,
    start_time: Optional[datetime] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Telegram Digest Dashboard",
        description="Stats and history for your Telegram Digest",
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # Store references for routes
    app.state.store = store
    app.state.vector_store = vector_store
    app.state.scheduler = scheduler
    app.state.start_time = start_time or datetime.now()

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def index(request: Request):
        """Main dashboard page."""
        stats = await store.get_stats()
        jobs = scheduler.get_jobs()
        is_paused = scheduler.is_paused()
        is_quiet = scheduler.is_quiet_hours()

        # Calculate uptime
        uptime = datetime.now() - app.state.start_time
        uptime_str = str(uptime).split(".")[0]  # Remove microseconds

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "stats": stats,
                "jobs": jobs,
                "is_paused": is_paused,
                "is_quiet_hours": is_quiet,
                "uptime": uptime_str,
                "qa_count": vector_store.count(),
            },
        )

    @app.get("/api/stats", dependencies=[Depends(_verify_auth)])
    async def api_stats():
        """API endpoint for stats."""
        stats = await store.get_stats()
        stats["qa_pairs_vectorized"] = vector_store.count()
        stats["is_paused"] = scheduler.is_paused()
        stats["is_quiet_hours"] = scheduler.is_quiet_hours()

        uptime = datetime.now() - app.state.start_time
        stats["uptime_seconds"] = int(uptime.total_seconds())

        return stats

    @app.get("/stats", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def stats_partial(request: Request):
        """HTMX partial for stats update."""
        stats = await store.get_stats()
        uptime = datetime.now() - app.state.start_time
        uptime_str = str(uptime).split(".")[0]

        return templates.TemplateResponse(
            "partials/stats.html",
            {
                "request": request,
                "stats": stats,
                "uptime": uptime_str,
                "qa_count": vector_store.count(),
                "is_paused": scheduler.is_paused(),
                "is_quiet_hours": scheduler.is_quiet_hours(),
            },
        )

    @app.get("/digests", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def digests_page(request: Request):
        """Digests history page."""
        digests = await store.get_recent_digests(limit=20)
        return templates.TemplateResponse(
            "digests.html",
            {"request": request, "digests": digests},
        )

    @app.get("/digests/list", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def digests_list_partial(request: Request):
        """HTMX partial for digests list."""
        digests = await store.get_recent_digests(limit=20)
        return templates.TemplateResponse(
            "partials/digests_list.html",
            {"request": request, "digests": digests},
        )

    @app.get("/qa", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def qa_page(request: Request):
        """Q&A knowledge base page."""
        pairs = vector_store.get_all()
        return templates.TemplateResponse(
            "qa.html",
            {"request": request, "pairs": pairs},
        )

    @app.get("/qa/list", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def qa_list_partial(request: Request):
        """HTMX partial for Q&A list."""
        pairs = vector_store.get_all()
        return templates.TemplateResponse(
            "partials/qa_list.html",
            {"request": request, "pairs": pairs},
        )

    @app.get("/suggestions", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def suggestions_page(request: Request):
        """Suggestions history page."""
        suggestions = await store.get_recent_suggestions(limit=50)
        return templates.TemplateResponse(
            "suggestions.html",
            {"request": request, "suggestions": suggestions},
        )

    @app.get("/suggestions/list", response_class=HTMLResponse, dependencies=[Depends(_verify_auth)])
    async def suggestions_list_partial(request: Request):
        """HTMX partial for suggestions list."""
        suggestions = await store.get_recent_suggestions(limit=50)
        return templates.TemplateResponse(
            "partials/suggestions_list.html",
            {"request": request, "suggestions": suggestions},
        )

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "healthy", "timestamp": datetime.now().isoformat()}

    return app


async def run_dashboard(
    store: Store,
    vector_store: VectorStore,
    scheduler: Scheduler,
    start_time: Optional[datetime] = None,
) -> None:
    """Run the dashboard server."""
    config = get_config()

    if not config.dashboard.enabled:
        log.info("dashboard_disabled")
        return

    app = create_app(store, vector_store, scheduler, start_time)

    server_config = uvicorn.Config(
        app,
        host=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
        port=config.dashboard.port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    log.info("dashboard_started", port=config.dashboard.port)

    await server.serve()
