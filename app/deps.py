"""FastAPI dependency helpers."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
