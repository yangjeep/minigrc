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


def init_db(engine: Engine) -> None:
    """Bring the database schema up to the latest Alembic revision."""
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
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
