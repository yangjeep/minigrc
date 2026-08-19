"""SQLAlchemy engine/session wiring.

Kept as plain module-level functions rather than a generic repository layer:
with one process and one SQLite file, an ORM session is already the right
level of abstraction. Schema creation/upgrades go through Alembic (see
migrations/) — `init_db` runs `alembic upgrade head` programmatically so
there is exactly one schema-initialization path for dev, tests, and Docker
alike (see docs/decisions/architectural-decisions.md).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Base(DeclarativeBase):
    pass


def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


def _invalidate_on_checkin(dbapi_connection, connection_record) -> None:
    """Force every checked-in SQLite connection closed so the next
    checkout always opens a fresh one (issue #55).

    `connection_record.invalidate()` (hard, the default) closes the
    physical connection immediately and sets its cached handle to
    `None` — the next `get_connection()` call sees that `None` and
    unconditionally reconnects. This is deterministic, unlike
    `pool_recycle`, which was tried first and rejected: it compares
    `time.time() - starttime > recycle`, and SQLAlchemy's own source
    comment on that check acknowledges `time.time()` isn't guaranteed
    sub-second precision — with `recycle=0` a connection created and
    reused within the same clock tick sails through the `> 0` check
    unrecycled, exactly the rare (~1-in-hundreds) flake measured in
    practice. Hard `invalidate()` has no such window.
    """
    connection_record.invalidate()


def build_engine(database_path_or_url: str) -> Engine:
    """Build the SQLAlchemy engine.

    Accepts either a bare filesystem path (the SQLite default) or a full
    SQLAlchemy URL (e.g. `postgresql+psycopg://...`) — a value containing
    `://` is treated as a URL as-is; anything else is assumed to be a
    SQLite file path. SQLite-only PRAGMAs are only attached for the
    sqlite dialect.

    The SQLite branch forces every connection closed on checkin (issue
    #55, `_invalidate_on_checkin`): this app's real request path (FastAPI
    dispatches each sync route handler to a worker thread) could
    otherwise check out a pooled connection that hadn't yet observed a
    write committed moments earlier on a *different* pooled connection —
    a real, reproducible read-after-write miss under WAL mode, confirmed
    against nothing but two ordinary sequential HTTP requests (no
    concurrency, no side-channel reads involved). Forcing a fresh
    connection on every checkout closes that window deterministically,
    while still going through the *default* `QueuePool`'s normal bounded
    concurrency (`pool_size`/`max_overflow`) — unlike `poolclass=NullPool`
    (unconditionally one physical connection per concurrent request,
    unbounded), tried first and rejected: it fixed the staleness but
    caused real `"database is locked"` errors under merely ~25 concurrent
    requests that the default pool's built-in throttling does not
    exhibit. Verified up to 100 concurrent login+write clients behaving
    the same as the pre-fix baseline (both degrade similarly past
    ~50-100 simultaneous clients — a pre-existing scalability ceiling of
    a single SQLite file, not something this fix changes). `:memory:` is
    explicitly rejected here because forcing a fresh connection on every
    checkout gives each one its own private, empty in-memory database —
    and nothing in this app currently requests it.
    """
    if "://" in database_path_or_url:
        url = database_path_or_url
    else:
        database_path = database_path_or_url
        if database_path == ":memory:":
            raise ValueError(
                "build_engine: ':memory:' is not supported — this app forces a fresh "
                "connection per checkout on the sqlite backend (see issue #55), and each "
                "one would get its own private, empty in-memory database. Use a real "
                "(even temporary) file path instead."
            )
        directory = os.path.dirname(database_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        url = f"sqlite:///{database_path}"

    is_sqlite = url.startswith("sqlite:")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _set_sqlite_pragmas)
        event.listen(engine, "checkin", _invalidate_on_checkin)
    return engine


def _escape_for_configparser(value: str) -> str:
    """Escape a value for Config.set_main_option.

    Config stores values through Python's configparser, whose default
    BasicInterpolation treats a bare "%" as the start of interpolation
    syntax (e.g. "%(foo)s") and raises ValueError on any other "%" — which
    a URL-encoded password (e.g. "%40" for "@") is guaranteed to contain,
    and which an ordinary filesystem path could coincidentally contain
    too. "%%" is configparser's own escape for a literal "%", and it
    correctly decodes back to a single "%" on get_main_option/get_section
    (verified against both read paths, including Alembic's own env.py,
    across a wide range of special-character values), so this round-trips
    exactly. Apply to every value passed to set_main_option, not only the
    ones you expect to contain "%".
    """
    return value.replace("%", "%%")


def init_db(engine: Engine) -> None:
    """Bring the database schema up to the latest Alembic revision."""
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", _escape_for_configparser(str(PROJECT_ROOT / "migrations")))
    # engine.url's default str()/repr() masks the password as "***" (issue
    # #40) — fine for logs/diagnostics, but Alembic reparses this exact
    # string into its own connection engine (migrations/env.py), so a
    # masked value here makes password-authenticated PostgreSQL migrations
    # fail. render_as_string(hide_password=False) round-trips the real
    # credential, including URL-encoded special characters, through
    # sqlalchemy.engine.url.make_url(); see app/cli.py for the
    # hide_password=True counterpart used for actual display output.
    real_url = engine.url.render_as_string(hide_password=False)
    alembic_cfg.set_main_option("sqlalchemy.url", _escape_for_configparser(real_url))
    command.upgrade(alembic_cfg, "head")


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
