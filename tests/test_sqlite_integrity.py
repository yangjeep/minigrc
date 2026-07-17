from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import build_engine, init_db
from app.models import FrameworkRequirement


def test_foreign_keys_pragma_is_enabled(tmp_path):
    engine = build_engine(str(tmp_path / "pragma_test.db"))
    with engine.connect() as conn:
        value = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1


def test_busy_timeout_pragma_is_set(tmp_path):
    engine = build_engine(str(tmp_path / "pragma_test.db"))
    with engine.connect() as conn:
        value = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert value == 5000


def test_foreign_keys_reject_orphan_requirement(tmp_path):
    engine = build_engine(str(tmp_path / "fk_test.db"))
    init_db(engine)

    with Session(engine) as session:
        session.add(
            FrameworkRequirement(
                framework_id="nonexistent-framework-id-000000000000",
                reference_code="X.1",
                title="Orphan requirement",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
