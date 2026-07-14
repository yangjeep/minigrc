"""SQLAlchemy engine/session wiring.

Kept as plain module-level functions rather than a generic repository layer:
with one process and one SQLite file, an ORM session is already the right
level of abstraction. Alembic is intentionally omitted for this first PR —
the schema is small and still moving; `Base.metadata.create_all` is enough
until the schema stabilizes (see docs/decisions/architectural-decisions.md).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(database_path: str) -> Engine:
    if database_path != ":memory:":
        directory = os.path.dirname(database_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    return create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )


def init_db(engine: Engine) -> None:
    # Import models so they register on Base.metadata before create_all.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


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
