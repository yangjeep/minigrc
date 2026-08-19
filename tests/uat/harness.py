"""Headless UAT harness primitives (issue #38).

Drives the real FastAPI app over a real TCP socket (uvicorn in a
background thread + httpx.Client), not an in-process ASGI transport and
not a browser — see
docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md §2 for
why. Everything here is test-only code: nothing in `app/` imports this
module (enforced by tests/test_uat_harness_safety.py, which runs in the
normal, always-on suite).
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db import build_engine
from app.main import create_app
from app.models import User
from app.security import hash_password

UAT_PASSWORD = "uat-harness-password-not-a-secret"  # noqa: S105 (fixture, not a real credential)

_CSRF_FIELD_RE = re.compile(r'name="csrf_token" value="([^"]+)"')

_SERVER_START_TIMEOUT_SECONDS = 10
_SERVER_STOP_TIMEOUT_SECONDS = 10


def assert_uat_mode_enabled() -> None:
    """Refuse to build/run anything unless the caller explicitly opted in.

    Defense-in-depth on top of the structural guarantees (no UAT code is
    importable from `app/`, no UAT route exists, the harness never reads
    GRC_DATA_DIR/DATABASE_URL from the ambient environment) — see the
    design doc §4. This check alone is not what makes the harness safe;
    it exists so an accidental `pytest -m uat` in a shell that forgot the
    env var fails immediately with a clear message instead of silently
    doing nothing useful.
    """
    if os.environ.get("GRC_UAT_MODE") != "1":
        raise RuntimeError(
            "Headless UAT harness refused to start: GRC_UAT_MODE=1 is required. "
            "Run via `python -m app.cli uat` or "
            "`GRC_UAT_MODE=1 pytest -m uat tests/uat`."
        )


def reset_postgres_schema(database_url: str) -> None:
    """Drop and recreate the `public` schema so a Postgres UAT run starts
    from a deterministic empty database, matching
    tests/test_postgres_compat.py's own teardown shape.

    Only ever called with the caller-supplied TEST_DATABASE_URL (see
    build_uat_app) — never the ambient DATABASE_URL — but this is still a
    schema-destroying operation, so require the target database name to
    look test-scoped as cheap defense-in-depth against a misconfigured
    TEST_DATABASE_URL pointed at a real database.
    """
    database_name = (make_url(database_url).database or "").lower()
    if "test" not in database_name and "uat" not in database_name:
        raise RuntimeError(
            f"refusing to reset Postgres schema on database {database_name!r}: "
            "TEST_DATABASE_URL must name a test/uat-scoped database"
        )
    engine = build_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def build_uat_app(*, tmp_path: Path, database_url: str | None = None) -> FastAPI:
    """Build an isolated app instance for one UAT run.

    SQLite (default): always an explicit tmp-file `database_path` — never
    reads GRC_DATA_DIR/GRC_DATABASE_PATH from the ambient environment, so
    there is no code path by which this could point at a real deployment's
    data directory.

    Postgres: caller must pass an explicit `database_url` (sourced from
    TEST_DATABASE_URL by the caller, e.g. tests/uat/conftest.py) — this
    function never falls back to the ambient DATABASE_URL variable.
    """
    assert_uat_mode_enabled()
    if database_url is not None:
        reset_postgres_schema(database_url)
        return create_app(database_url=database_url)
    return create_app(database_path=str(tmp_path / "uat.db"))


class LiveServer:
    """Runs a FastAPI app on a real, OS-assigned localhost port in a
    background thread, for the duration of a `with` block."""

    def __init__(self, app: FastAPI, host: str = "127.0.0.1") -> None:
        self._config = uvicorn.Config(
            app,
            host=host,
            port=0,
            log_level="warning",
            log_config=None,  # never let uvicorn re-init app's own logging config
            lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> LiveServer:
        self._config.install_signal_handlers = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("UAT live server did not start within the startup timeout")
            time.sleep(0.01)

        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def _run(self) -> None:
        asyncio.run(self._server.serve())

    def __exit__(self, *exc_info: object) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=_SERVER_STOP_TIMEOUT_SECONDS)


@dataclass
class _Exchange:
    method: str
    url: str
    status_code: int | None = None
    body_snippet: str = ""


@dataclass
class RequestRecorder:
    """Records the last few HTTP exchanges on a real httpx.Client, for
    dumping into a failure artifact — this harness's equivalent of a
    browser screenshot (see tests/uat/conftest.py's
    pytest_runtest_makereport hook)."""

    max_entries: int = 20
    max_body_chars: int = 2000
    entries: list[_Exchange] = field(default_factory=list)

    def event_hooks(self) -> dict[str, list]:
        return {"request": [self._on_request], "response": [self._on_response]}

    def _on_request(self, request) -> None:
        self.entries.append(_Exchange(method=request.method, url=str(request.url)))
        overflow = len(self.entries) - self.max_entries
        if overflow > 0:
            del self.entries[:overflow]

    def _on_response(self, response) -> None:
        response.read()
        if not self.entries:
            return
        last = self.entries[-1]
        last.status_code = response.status_code
        last.body_snippet = response.text[: self.max_body_chars]

    def render(self) -> str:
        blocks = [f"{e.method} {e.url} -> {e.status_code}\n{e.body_snippet}" for e in self.entries]
        return "\n\n---\n\n".join(blocks)


def redirect_path(response) -> str:
    """The path component of a redirect's Location header, with the
    ?flash=...&flash_kind=... query string (app/flash.py) stripped —
    every state-changing route in this app redirects through
    redirect_with_flash, so a bare `.rsplit("/", 1)` on the raw header
    would otherwise fold the query string into the trailing id segment."""
    location = response.headers["location"]
    return urlsplit(location).path


def extract_csrf_field(html: str) -> str:
    """Extract the hidden csrf_token form field value — same convention
    as tests/conftest.py's extract_csrf_token, for real HTML pages."""
    match = _CSRF_FIELD_RE.search(html)
    assert match is not None, "no csrf_token field found in response HTML"
    return match.group(1)


def csrf_header_value(client) -> str:
    """The CSRF cookie value, for JSON API calls (X-CSRF-Token header) —
    the real register-grid JS reads the same cookie (see
    app/deps.py::verify_csrf_header)."""
    value = client.cookies.get("csrf_token")
    assert value, "no csrf_token cookie set on the client yet — make a GET request first"
    return value


_POLL_TIMEOUT_SECONDS = 2.0
_POLL_INTERVAL_SECONDS = 0.05


def poll_for_visibility(session_factory, fetch):
    """Retry `fetch(session)` in fresh sessions until it returns a
    truthy value, or raise TimeoutError.

    Originally written as a stopgap for issue #55, a real read-after-
    write visibility gap. #55's SQLite-connection-freshness fix
    (`app/db.py::_invalidate_on_checkin`) shipped and measurably reduced
    it, but a rarer residual is a separate, backend-independent FastAPI
    response-vs-commit ordering behavior tracked as issue #57 — see
    `tests/test_sqlite_read_after_write.py` for the full account. Kept as
    cheap defense-in-depth for the rare side-channel check that has no
    rendered/API surface to read instead — every scenario in tests/uat/
    still prefers sourcing ids from real HTTP responses over a database
    peek, both for UAT faithfulness and because it avoids racing #57's
    residual far more often than a raw DB peek would."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while True:
        with session_factory() as session:
            result = fetch(session)
            session.commit()
        if result:
            return result
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"poll_for_visibility: fetch() never returned a truthy value within {_POLL_TIMEOUT_SECONDS}s"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def create_uat_user(session_factory, *, email: str, role: str = "user") -> User:
    """Deterministic fixture login user — mirrors tests/conftest.py's
    existing test_user/admin_user pattern. A login credential is not
    itself a material compliance fact, so a direct ORM insert here (as
    opposed to routing every other scenario step through real
    domain/HTTP paths) does not violate RULES.md §3."""
    with session_factory() as session:
        user = User(email=email, password_hash=hash_password(UAT_PASSWORD), role=role, status="active")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
