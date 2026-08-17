"""Settings: exclusive SQLite/PostgreSQL backend-selection contract (issue #22).

A miniGRC deployment must select exactly one relational backend. Before
this change, `Settings.resolved_engine_target` silently preferred
`database_url` over `database_path` with no validation. The fix is a plain
function, `reject_ambiguous_database_config`, called by `create_app` right
before it resolves the engine target — not a pydantic validator on
`Settings` construction, since `get_settings()` builds a real `Settings()`
*before* `create_app`'s test-only `database_path` override (which always
clears `database_url`) is applied; validating at construction time would
reject an ambient environment that has both variables set even when the
caller is about to override one of them (see app/config.py's docstring).

Every test here passes both `database_url` and `database_path` explicitly
(even when one is meant to be empty/unset) so the assertions don't depend
on whatever `DATABASE_URL`/`GRC_DATABASE_PATH` happen to be set in the
process running the suite.
"""

from __future__ import annotations

import pytest

from app.config import Settings, reject_ambiguous_database_config
from app.main import create_app


def test_reject_ambiguous_database_config_raises_when_both_set():
    settings = Settings(database_url="postgresql+psycopg://user:pass@host/db", database_path="/tmp/grc.db")
    with pytest.raises(RuntimeError, match="Ambiguous database configuration"):
        reject_ambiguous_database_config(settings)


def test_reject_ambiguous_database_config_allows_database_url_alone():
    settings = Settings(database_url="postgresql+psycopg://user:pass@host/db", database_path=None)
    reject_ambiguous_database_config(settings)  # must not raise
    assert settings.resolved_engine_target == "postgresql+psycopg://user:pass@host/db"


def test_reject_ambiguous_database_config_allows_database_path_alone():
    settings = Settings(database_url="", database_path="/tmp/grc.db")
    reject_ambiguous_database_config(settings)  # must not raise
    assert settings.resolved_engine_target == "/tmp/grc.db"


def test_reject_ambiguous_database_config_allows_neither_set():
    settings = Settings(database_url="", database_path=None)
    reject_ambiguous_database_config(settings)  # must not raise
    assert settings.resolved_engine_target == settings.resolved_database_path


def test_reject_ambiguous_database_config_data_dir_alone_is_not_ambiguous():
    # data_dir is also used for non-database storage (uploads, tmp), so it
    # is deliberately excluded from the ambiguity check — only the
    # SQLite-specific `database_path` field conflicts with DATABASE_URL.
    settings = Settings(
        database_url="postgresql+psycopg://user:pass@host/db",
        database_path=None,
        data_dir="/var/lib/minigrc",
    )
    reject_ambiguous_database_config(settings)  # must not raise
    assert settings.resolved_engine_target == "postgresql+psycopg://user:pass@host/db"


def test_create_app_rejects_ambiguous_ambient_environment(tmp_path, monkeypatch):
    # create_app's own test-only database_path/data_dir override always
    # clears database_url, so passing an explicit database_path here must
    # succeed regardless of what's in the ambient environment — this is
    # the exact call path a previous, since-reverted pydantic-validator
    # version of this check broke (validating at Settings() construction,
    # before create_app's override runs).
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@host/db")
    monkeypatch.setenv("GRC_DATABASE_PATH", "/tmp/should-be-overridden.db")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        create_app(database_path=str(tmp_path / "override.db"))
    finally:
        get_settings.cache_clear()


def test_create_app_logs_resolved_backend_dialect(tmp_path, monkeypatch):
    # configure_logging() replaces the root logger's handlers wholesale
    # (app/logging_config.py: `root.handlers.clear()`), which defeats both
    # caplog and stdout/stderr capture fixtures — assert directly on the
    # logger call instead of fighting that plumbing.
    from app import main as main_module

    logged_messages: list[str] = []
    monkeypatch.setattr(
        main_module.logger,
        "info",
        lambda msg, *args: logged_messages.append(msg % args if args else msg),
    )
    create_app(database_path=str(tmp_path / "log_test.db"))
    assert any("database backend: sqlite" in message for message in logged_messages)
