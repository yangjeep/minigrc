from __future__ import annotations

import datetime
from unittest.mock import patch

from sqlalchemy import select

from app import cli
from app.config import get_settings
from app.db import build_engine, make_session_factory
from app.models import ControlPeriod, InternalControl, User


def test_create_user_creates_user(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        exit_code = cli.create_user("New@Example.com")

    assert exit_code == 0

    engine = build_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        user = session.scalar(select(User).where(User.email == "new@example.com"))
        assert user is not None
        assert user.password_hash != "a-strong-password"

    get_settings.cache_clear()


def test_create_user_rejects_mismatched_passwords(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test2.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["password-one", "password-two"]):
        exit_code = cli.create_user("mismatch@example.com")

    assert exit_code == 1
    get_settings.cache_clear()


def test_create_user_rejects_duplicate_email(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test3.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    with patch("getpass.getpass", side_effect=["a-strong-password", "a-strong-password"]):
        assert cli.create_user("dup@example.com") == 0

    exit_code = cli.create_user("dup@example.com")
    assert exit_code == 1
    get_settings.cache_clear()


def test_generate_control_occurrences_command_materializes_for_calendar_control(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_occurrences.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    engine = build_engine(str(db_path))
    from app.db import init_db

    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        control = InternalControl(name="Quarterly review", cadence_type="calendar", cadence_interval_months=3)
        session.add(control)
        session.commit()
        control_id = control.id

    exit_code = cli.generate_control_occurrences_command(control_id, None)
    assert exit_code == 0
    get_settings.cache_clear()


def test_generate_control_occurrences_command_rejects_unknown_control(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_occurrences2.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    exit_code = cli.generate_control_occurrences_command("deadbeefdeadbeefdeadbeefdeadbeef", None)
    assert exit_code == 1
    get_settings.cache_clear()


def test_generate_control_occurrences_command_rejects_unknown_period(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_occurrences3.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    exit_code = cli.generate_control_occurrences_command(None, "deadbeefdeadbeefdeadbeefdeadbeef")
    assert exit_code == 1
    get_settings.cache_clear()


def test_generate_control_occurrences_command_reports_clean_error_for_non_active_period(
    tmp_path, monkeypatch
):
    """Regression guard: a non-active --period-id must produce a clean CLI
    error, not an uncaught traceback (adversarial review finding, #11)."""
    db_path = tmp_path / "cli_occurrences4.db"
    monkeypatch.setenv("GRC_DATABASE_PATH", str(db_path))
    get_settings.cache_clear()

    engine = build_engine(str(db_path))
    from app.db import init_db

    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        # A calendar-cadence control must exist, or the per-control
        # generate_occurrences() call (where the ValueError is raised)
        # never happens and the command would report false success.
        control = InternalControl(name="Quarterly review", cadence_type="calendar", cadence_interval_months=3)
        period = ControlPeriod(
            name="Planned period",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            created_by="admin@example.com",
        )
        session.add_all([control, period])
        session.commit()
        period_id = period.id

    exit_code = cli.generate_control_occurrences_command(None, period_id)
    assert exit_code == 1
    get_settings.cache_clear()
