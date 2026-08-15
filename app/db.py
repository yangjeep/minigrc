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


def build_engine(database_path_or_url: str) -> Engine:
    """Build the SQLAlchemy engine.

    Accepts either a bare filesystem path (the SQLite default) or a full
    SQLAlchemy URL (e.g. `postgresql+psycopg://...`) — a value containing
    `://` is treated as a URL as-is; anything else is assumed to be a
    SQLite file path. SQLite-only PRAGMAs are only attached for the
    sqlite dialect.
    """
    if "://" in database_path_or_url:
        url = database_path_or_url
    else:
        database_path = database_path_or_url
        if database_path != ":memory:":
            directory = os.path.dirname(database_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        url = f"sqlite:///{database_path}"

    is_sqlite = url.startswith("sqlite:")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _set_sqlite_pragmas)
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
