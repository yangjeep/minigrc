"""Application factory and ASGI entrypoint.

`create_app` takes optional `database_path`/`data_dir` overrides so tests can
point each run at an isolated SQLite file and storage directory without
touching environment variables, module-level globals, or the developer's
real ./data directory (see tests/conftest.py).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import build_engine, init_db, make_session_factory, session_scope
from app.logging_config import configure_logging
from app.routers import (
    audit_log,
    auth,
    controls,
    dashboard,
    frameworks,
    google_drive,
    google_oidc,
    people,
    placeholders,
    policies,
    risks,
    vendor_systems,
)
from app.security import CSRF_COOKIE_NAME, new_csrf_token
from app.seed import seed_if_empty

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent

NAV_ITEMS = [
    ("Dashboard", "/"),
    ("Frameworks", "/frameworks"),
    ("Controls", "/controls"),
    ("Policies", "/policies"),
    ("Evidence", "/evidence"),
    ("Risks", "/risks"),
    ("People", "/people"),
    ("Vendors", "/vendors"),
    ("Actions", "/actions"),
    ("Connectors", "/connectors"),
    ("Trust Center", "/trust-center"),
    ("Audit Log", "/audit-log"),
]

CSRF_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def create_app(database_path: str | None = None, data_dir: str | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    if database_path is not None or data_dir is not None:
        effective_data_dir = data_dir if data_dir is not None else (os.path.dirname(database_path) or ".")
        effective_database_path = (
            database_path if database_path is not None else f"{effective_data_dir}/grc.db"
        )
        settings = settings.model_copy(
            update={"data_dir": effective_data_dir, "database_path": effective_database_path}
        )

    resolved_path = settings.resolved_database_path
    engine = build_engine(resolved_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        if seed_if_empty(session):
            logger.info("seeded example dataset")

    app = FastAPI(title="minigrc", version="0.1.0")
    app.state.session_factory = session_factory
    app.state.settings = settings

    templates = Jinja2Templates(directory=APP_DIR / "templates")
    templates.env.globals["nav_items"] = NAV_ITEMS
    templates.env.globals["csrf_token"] = lambda request: request.state.csrf_token
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    @app.middleware("http")
    async def csrf_cookie_middleware(request: Request, call_next):
        existing = request.cookies.get(CSRF_COOKIE_NAME)
        request.state.csrf_token = existing or new_csrf_token()
        response = await call_next(request)
        if not existing:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                request.state.csrf_token,
                httponly=True,
                samesite="lax",
                secure=settings.session_cookie_secure,
                max_age=CSRF_COOKIE_MAX_AGE_SECONDS,
            )
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(google_oidc.router)
    app.include_router(dashboard.router)
    app.include_router(frameworks.router)
    app.include_router(controls.router)
    app.include_router(policies.router)
    app.include_router(risks.router)
    app.include_router(people.router)
    app.include_router(vendor_systems.router)
    app.include_router(google_drive.router)
    app.include_router(audit_log.router)
    # placeholders.router registers a catch-all "/{slug}" — it must be
    # included last so it never shadows a more specific route above.
    app.include_router(placeholders.router)

    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request: Request, exc: FastAPIHTTPException) -> HTMLResponse:
        if 300 <= exc.status_code < 400:
            return RedirectResponse(url=exc.headers.get("Location", "/"), status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "message": exc.detail or "Error"},
            status_code=exc.status_code,
        )

    @app.exception_handler(500)
    async def server_error(request: Request, exc: Exception) -> HTMLResponse:
        logger.exception("unhandled server error")
        return templates.TemplateResponse(
            request, "error.html", {"status_code": 500, "message": "Something went wrong"}, status_code=500
        )

    return app


def __getattr__(name: str) -> FastAPI:
    # Lazily build the default app only when something actually asks for
    # `app.main.app` (e.g. `uvicorn app.main:app`) — not merely on import.
    # Importing this module (as tests/conftest.py does, for `create_app`)
    # must never touch the real on-disk database.
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
