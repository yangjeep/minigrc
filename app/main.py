"""Application factory and ASGI entrypoint.

`create_app` takes an optional `database_path` override so tests can point
each run at an isolated SQLite file without touching environment variables
or module-level globals (see tests/conftest.py).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import build_engine, init_db, make_session_factory, session_scope
from app.logging_config import configure_logging
from app.routers import audit_log, controls, dashboard, frameworks, placeholders, risks
from app.seed import seed_if_empty

logger = logging.getLogger(__name__)

NAV_ITEMS = [
    ("Dashboard", "/"),
    ("Frameworks", "/frameworks"),
    ("Controls", "/controls"),
    ("Policies", "/policies"),
    ("Evidence", "/evidence"),
    ("Risks", "/risks"),
    ("Actions", "/actions"),
    ("Connectors", "/connectors"),
    ("Trust Center", "/trust-center"),
    ("Audit Log", "/audit-log"),
]


def create_app(database_path: str | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    resolved_path = database_path if database_path is not None else settings.database_path
    engine = build_engine(resolved_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        if seed_if_empty(session):
            logger.info("seeded example dataset")

    app = FastAPI(title="playground-grc", version="0.1.0")
    app.state.session_factory = session_factory
    app.state.settings = settings

    templates = Jinja2Templates(directory="app/templates")
    templates.env.globals["nav_items"] = NAV_ITEMS
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(dashboard.router)
    app.include_router(frameworks.router)
    app.include_router(controls.router)
    app.include_router(risks.router)
    app.include_router(audit_log.router)
    # placeholders.router registers a catch-all "/{slug}" — it must be
    # included last so it never shadows a more specific route above.
    app.include_router(placeholders.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "error.html", {"status_code": 404, "message": "Page not found"}, status_code=404
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc: Exception) -> HTMLResponse:
        logger.exception("unhandled server error")
        return templates.TemplateResponse(
            request, "error.html", {"status_code": 500, "message": "Something went wrong"}, status_code=500
        )

    return app


app = create_app()
