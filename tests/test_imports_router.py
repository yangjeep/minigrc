"""HTTP-level and CLI-level tests for the import subsystem's entry points
(Feature 8) — the underlying importer logic is covered by test_imports.py."""

from __future__ import annotations

from app.cli import import_csv_command
from app.models import ImportJob, Risk
from tests.conftest import extract_csrf_token

RISK_CSV = (
    b"title,description,category,likelihood,impact,owner,status\nCLI-imported risk,d,security,2,3,me,open\n"
)


def test_risks_import_route_creates_rows_and_tracks_import_job(logged_in_client, app):
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("risks.csv", RISK_CSV, "text/csv")}
    response = logged_in_client.post(
        "/risks/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Imported+1" in response.headers["location"]

    with app.state.session_factory() as session:
        risk = session.query(Risk).filter(Risk.title == "CLI-imported risk").one()
        assert risk is not None
        jobs = session.query(ImportJob).filter_by(importer_name="risk_register_csv").all()
        assert len(jobs) == 1
        assert jobs[0].source == "web"
        assert jobs[0].status == "completed"


def test_risks_import_route_rejects_bad_row(logged_in_client):
    csv_content = b"title,description,category,likelihood,impact,owner,status\n,d,security,2,3,me,open\n"
    page = logged_in_client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("risks.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        "/risks/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_cli_import_csv_command(app, tmp_path, monkeypatch):
    csv_path = tmp_path / "risks.csv"
    csv_path.write_bytes(RISK_CSV)
    monkeypatch.setenv("GRC_DATABASE_PATH", app.state.settings.resolved_database_path)
    monkeypatch.setattr("app.cli.get_settings", lambda: app.state.settings)

    exit_code = import_csv_command("risk_register_csv", str(csv_path), None)
    assert exit_code == 0

    with app.state.session_factory() as session:
        assert session.query(Risk).filter(Risk.title == "CLI-imported risk").count() == 1
