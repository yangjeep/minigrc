"""Fixtures for headless UAT scenarios (issue #38).

Every test collected under tests/uat/ is auto-marked `uat`; pyproject.toml
sets `addopts = "-m 'not uat'"` so the default `pytest` command (and the
existing CI job that runs it) never executes any of this. The documented
opt-in command is `GRC_UAT_MODE=1 pytest -m uat tests/uat` (or
`python -m app.cli uat`).
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

import httpx
import pytest

from tests.uat.harness import LiveServer, RequestRecorder, build_uat_app

ARTIFACTS_DIR = Path(os.environ.get("GRC_UAT_ARTIFACTS_DIR", "artifacts/uat"))

# Every scenario module under tests/uat/ sets `pytestmark = pytest.mark.uat`
# itself (see test_soc2_control_lifecycle.py / test_authorization_boundaries.py)
# — pyproject.toml's `addopts = "-m 'not uat'"` then keeps the default
# `pytest` command from ever executing them.


@pytest.fixture
def uat_server(tmp_path, request):
    backend = getattr(request, "param", "sqlite")
    database_url = None
    if backend == "postgres":
        database_url = os.environ.get("TEST_DATABASE_URL", "")
        if not database_url:
            pytest.skip("TEST_DATABASE_URL not set — no live Postgres available")

    app = build_uat_app(tmp_path=tmp_path, database_url=database_url)
    with LiveServer(app) as server:
        yield server.base_url, app.state.session_factory


@pytest.fixture
def recorder():
    return RequestRecorder()


@pytest.fixture
def uat_client(uat_server, recorder):
    base_url, _session_factory = uat_server
    with httpx.Client(base_url=base_url, event_hooks=recorder.event_hooks(), timeout=10.0) as client:
        yield client


@pytest.fixture
def uat_session_factory(uat_server):
    _base_url, session_factory = uat_server
    return session_factory


def _run_timestamp() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


_RUN_DIR_NAME = _run_timestamp()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when not in ("call", "setup") or not report.failed:
        return
    fspath = str(item.fspath).replace(os.sep, "/")
    if "/tests/uat/" not in fspath:
        return

    recorder = item.funcargs.get("recorder")
    artifact_dir = ARTIFACTS_DIR / _RUN_DIR_NAME
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{item.name}.txt"

    lines = [
        f"scenario: {item.nodeid}",
        "outcome: FAILED",
        "",
        str(report.longrepr),
        "",
        "--- recorded HTTP exchanges ---",
        recorder.render() if recorder is not None else "(no recorder fixture used by this scenario)",
    ]
    artifact_path.write_text("\n".join(lines))
    report.sections.append(("UAT failure artifact", str(artifact_path)))
