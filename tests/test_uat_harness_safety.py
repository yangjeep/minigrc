"""Production-safety regression tests for the headless UAT harness
(issue #38). Deliberately NOT under tests/uat/ — this file always runs in
the normal `pytest` suite (no `uat` marker), so a regression here (e.g.
someone wiring UAT code into the real app) is caught by ordinary CI, not
only by an opt-in UAT run.

See docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md §4.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
REPO_ROOT = Path(__file__).resolve().parent.parent


CLI_PATH = APP_DIR / "cli.py"


def _all_app_source_files():
    """Every app/ source file EXCEPT cli.py, which is the sanctioned,
    lazy-import, explicitly-invoked `python -m app.cli uat` entrypoint
    (design doc §7) — never imported by the ASGI app itself and never
    invoked by the Dockerfile's CMD (checked separately below)."""
    return [path for path in APP_DIR.rglob("*.py") if path != CLI_PATH]


def test_app_never_imports_the_uat_harness():
    """Static guarantee: no app/ module the running web server actually
    loads references the test-only UAT harness. Combined with the
    Dockerfile never COPYing tests/ into the production image (checked
    below), this makes "UAT code reachable from a production web
    process" structurally impossible, not merely flag-gated."""
    offending = []
    for path in _all_app_source_files():
        text = path.read_text()
        if "tests.uat" in text or "tests/uat" in text or "import tests" in text:
            offending.append(str(path.relative_to(REPO_ROOT)))
    assert not offending, f"app/ must never reference the UAT harness: {offending}"


def test_cli_uat_command_is_the_only_sanctioned_reference():
    """app/cli.py's `uat` subcommand is the one deliberate, documented
    exception (it shells to `pytest -m uat tests/uat`) — pin that the
    reference stays confined to that one function and is a lazy import,
    so it can never be reached merely by importing app.cli (e.g. from a
    future admin script) without pytest installed."""
    text = CLI_PATH.read_text()
    assert "tests/uat" in text, "expected app/cli.py's uat command to reference tests/uat"
    uat_command_body = text.split("def uat_command(", 1)[1].split("\ndef ", 1)[0]
    assert "import pytest" in uat_command_body, "pytest must be imported lazily inside uat_command"
    assert "import pytest" not in text.split("def uat_command(", 1)[0], (
        "pytest must not be imported at app/cli.py module scope"
    )


def test_dockerfile_cmd_never_invokes_the_uat_subcommand():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "cli.py uat" not in dockerfile
    assert "app.cli uat" not in dockerfile


def test_app_source_defines_no_dev_or_reset_style_routes():
    """No route decorator anywhere in app/ targets a dev-bootstrap/reset/
    UAT-flavored path. A real dev/reset endpoint would be a production
    authentication-bypass risk regardless of who added it or why."""
    suspicious_fragments = ("/dev/", "/dev-", "/__reset", "/uat/", "/_uat")
    offending = []
    for path in _all_app_source_files():
        text = path.read_text()
        for fragment in suspicious_fragments:
            if fragment in text:
                offending.append(f"{path.relative_to(REPO_ROOT)}: {fragment!r}")
    assert not offending, f"found suspicious dev/reset/uat route fragments: {offending}"


def test_default_create_app_exposes_no_uat_route(tmp_path):
    """Live assertion alongside the static ones above: build the app the
    way `uvicorn app.main:app` does (only the test-isolation overrides
    this repo's own test suite always uses), and confirm no registered
    route path looks like a dev/reset/UAT backdoor."""
    from app.main import create_app

    app = create_app(database_path=str(tmp_path / "safety.db"))
    suspicious = [
        route.path
        for route in app.routes
        if hasattr(route, "path")
        and any(fragment in route.path for fragment in ("/dev/", "/dev-", "/reset", "/uat"))
    ]
    assert not suspicious, f"found suspicious route(s) on the default app: {suspicious}"


def test_dockerfile_never_copies_tests_directory():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "COPY tests" not in dockerfile
    assert "pip install --no-cache-dir ." in dockerfile, (
        "production install must stay plain `pip install .` (no [dev] extras) "
        "so pytest is never present in the shipped image"
    )


def test_uat_harness_only_activates_with_explicit_env_var(tmp_path):
    import pytest

    from tests.uat.harness import assert_uat_mode_enabled, build_uat_app

    old = os.environ.pop("GRC_UAT_MODE", None)
    try:
        with pytest.raises(RuntimeError):
            assert_uat_mode_enabled()
        with pytest.raises(RuntimeError):
            build_uat_app(tmp_path=tmp_path)
    finally:
        if old is not None:
            os.environ["GRC_UAT_MODE"] = old


def test_reset_postgres_schema_rejects_a_non_test_scoped_database_name():
    """Adversarial-review finding: reset_postgres_schema is a
    schema-destroying operation, so it must refuse a URL that doesn't
    look test/uat-scoped even though its only real call site (build_uat_app)
    always passes the caller-supplied TEST_DATABASE_URL, never the
    ambient DATABASE_URL."""
    import pytest

    from tests.uat.harness import reset_postgres_schema

    with pytest.raises(RuntimeError):
        reset_postgres_schema("postgresql+psycopg://user@localhost:5432/production")


_SYNTHETIC_PASSING_TEST = textwrap.dedent(
    """
    import pytest
    pytestmark = pytest.mark.uat

    def test_synthetic_pass(recorder):
        assert 1 + 1 == 2
    """
)

_SYNTHETIC_FAILING_TEST = textwrap.dedent(
    """
    import pytest
    pytestmark = pytest.mark.uat

    def test_synthetic_fail(recorder):
        fake_exchange = type(
            "E", (), {"method": "GET", "url": "http://x/probe", "status_code": 200, "body_snippet": "probe"}
        )
        recorder.entries.append(fake_exchange())
        assert 1 + 1 == 3, "intentional failure for the harness self-test"
    """
)

_SYNTHETIC_CONFTEST = textwrap.dedent(
    f"""
    import sys
    sys.path.insert(0, {str(REPO_ROOT)!r})
    from tests.uat.conftest import pytest_runtest_makereport, ARTIFACTS_DIR, recorder  # noqa: F401
    """
)


def _run_synthetic_uat_suite(tmp_path: Path, test_source: str) -> subprocess.CompletedProcess:
    suite_dir = tmp_path / "tests" / "uat"
    suite_dir.mkdir(parents=True)
    (suite_dir / "__init__.py").write_text("")
    (suite_dir / "conftest.py").write_text(_SYNTHETIC_CONFTEST)
    (suite_dir / "test_synthetic.py").write_text(test_source)
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nmarkers = ["uat"]\n')

    return subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "uat", "tests/uat", "-q"],
        cwd=tmp_path,
        env={**os.environ, "GRC_UAT_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_passing_headless_uat_scenario_exits_zero(tmp_path):
    result = _run_synthetic_uat_suite(tmp_path, _SYNTHETIC_PASSING_TEST)
    assert result.returncode == 0, result.stdout + result.stderr


def test_failing_headless_uat_scenario_exits_nonzero_with_diagnostics(tmp_path):
    result = _run_synthetic_uat_suite(tmp_path, _SYNTHETIC_FAILING_TEST)
    assert result.returncode != 0, "a failing headless UAT scenario must not exit zero"

    artifact_dir = tmp_path / "artifacts" / "uat"
    artifacts = list(artifact_dir.rglob("*.txt")) if artifact_dir.exists() else []
    assert artifacts, f"expected a failure artifact under {artifact_dir}; stdout was:\n{result.stdout}"
    artifact_text = artifacts[0].read_text()
    assert "intentional failure for the harness self-test" in artifact_text
    assert "GET http://x/probe -> 200" in artifact_text
