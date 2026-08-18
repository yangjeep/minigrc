"""Unit tests for app.main.create_app's database_url parameter (issue #38).

Exercises the parameter with a sqlite:// URL rather than a real Postgres
server — the point under test is create_app's own routing/validation
logic (which backend override wins, mutual exclusivity), not Postgres
connectivity itself (see tests/test_postgres_compat.py for that, and
tests/uat/ for the real-Postgres headless UAT path).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import Framework


def test_create_app_uses_explicit_database_url(tmp_path):
    db_file = tmp_path / "explicit-url.db"
    app = create_app(database_url=f"sqlite:///{db_file}")

    assert db_file.exists()
    with app.state.session_factory() as session:
        # init_db/reconcile_system_catalogs ran against this exact file.
        assert session.scalar(select(Framework)) is not None


def test_create_app_rejects_database_url_with_database_path(tmp_path):
    with pytest.raises(ValueError):
        create_app(database_url="sqlite:///x.db", database_path=str(tmp_path / "y.db"))


def test_create_app_rejects_database_url_with_data_dir(tmp_path):
    with pytest.raises(ValueError):
        create_app(database_url="sqlite:///x.db", data_dir=str(tmp_path))


def test_create_app_database_path_behavior_unchanged(tmp_path):
    """Regression guard: the pre-existing database_path override path
    must keep clearing database_url exactly as before — the new
    database_url parameter must not change this branch's behavior."""
    db_file = tmp_path / "via-path.db"
    app = create_app(database_path=str(db_file))
    assert app.state.settings.database_url == ""
    assert app.state.settings.database_path == str(db_file)
