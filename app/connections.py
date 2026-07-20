"""Build connection URLs and run bounded, read-only connection tests for
ExternalConnection rows.

Security posture (see the architecture checkpoint on issue #5 and ADR #24):
- Connection tests are the *only* SQL this module ever runs — a fixed
  `SELECT 1` liveness probe, nothing else. There is no arbitrary-query
  path here or anywhere else in this feature.
- Every connection uses a strict timeout so a misconfigured/unreachable
  host can't hang a request.
- Failure messages are sanitized: never the resolved password, never a
  raw driver conninfo string, never the full connection URL.
"""

from __future__ import annotations

import dataclasses
import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import ExternalConnection
from app.secrets import resolve_secret

_DRIVERS = {
    "postgres": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
}


class ConnectionTestError(RuntimeError):
    """Raised for a connection configuration that can't be turned into a URL here."""


@dataclasses.dataclass(frozen=True)
class ConnectionTestResult:
    status: str  # "success" | "failure"
    message: str


def build_connection_url(conn: ExternalConnection, *, password: str | None) -> URL:
    if conn.db_type in ("postgres", "mysql"):
        return URL.create(
            drivername=_DRIVERS[conn.db_type],
            username=conn.username,
            password=password,
            host=conn.host,
            port=conn.port,
            database=conn.database_name,
        )
    if conn.db_type == "sqlite":
        return URL.create(drivername="sqlite", database=conn.sqlite_path)
    raise ConnectionTestError(
        f"db_type '{conn.db_type}' has no host/port/username shape — resolve its secret "
        "as a full URL directly instead of calling build_connection_url."
    )


def _sanitized_error(exc: Exception) -> str:
    # Truncate and never include the exception's full str() verbatim —
    # some drivers embed the DSN (without password, but be conservative)
    # in connection-refused messages.
    return f"{type(exc).__name__}: connection failed"


def run_connection_test(
    session: Session, conn: ExternalConnection, *, key: str, actor: str, timeout_seconds: int = 5
) -> ConnectionTestResult:
    try:
        if conn.db_type == "generic":
            if conn.secret is None:
                raise ConnectionTestError("generic connections require a secret holding the full URL")
            url = resolve_secret(conn.secret, key=key)
            connect_args: dict = {}
        else:
            password = resolve_secret(conn.secret, key=key) if conn.secret is not None else None
            url = build_connection_url(conn, password=password)
            connect_args = {"connect_timeout": timeout_seconds} if conn.db_type != "sqlite" else {}

        engine = create_engine(url, connect_args=connect_args, pool_pre_ping=False)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            result = ConnectionTestResult(status="success", message="Connection succeeded.")
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any driver/network error is a "failure" result
        result = ConnectionTestResult(status="failure", message=_sanitized_error(exc))

    conn.last_tested_at = datetime.datetime.now(datetime.UTC)
    conn.last_test_status = result.status
    conn.last_test_message = result.message
    record_audit_event(
        session,
        entity_type="external_connection",
        entity_id=conn.id,
        action="test",
        detail=f"Connection test for '{conn.name}': {result.status} — {result.message}",
        actor=actor,
    )
    return result
