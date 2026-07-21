from __future__ import annotations

from app.jobs import enqueue_job


def test_jobs_list_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/jobs")
    assert response.status_code == 403


def test_jobs_list_loads_for_admin(admin_client):
    response = admin_client.get("/admin/jobs")
    assert response.status_code == 200
    assert b"Jobs" in response.content


def test_jobs_register_api_shows_enqueued_job(admin_client, app):
    with app.state.session_factory() as session:
        enqueue_job(session, job_type="connection_test", payload={"connection_id": "abc123"}, actor="admin")
        session.commit()

    response = admin_client.get("/api/registers/admin_jobs")
    assert response.status_code == 200
    rows = response.json()
    assert any(row["job_type"] == "connection_test" for row in rows)


def test_jobs_register_api_formats_timestamps(admin_client, app):
    """UAT finding: the Jobs list showed raw isoformat() timestamps with
    microseconds ('2026-07-21T00:20:10.060557') for Available/Claimed,
    matching the same bug already fixed on the Connections index — same
    register-grid framework, same missed formatting. See 2026-07-20
    admin/OAuth/IAM/connections consolidation worklog."""
    with app.state.session_factory() as session:
        enqueue_job(session, job_type="connection_test", payload={}, actor="admin")
        session.commit()

    response = admin_client.get("/api/registers/admin_jobs")
    assert response.status_code == 200
    row = response.json()[0]
    assert "." not in row["available_at"]


def test_jobs_list_status_column_uses_badge_styling(admin_client):
    """No JS test harness in this repo — see the same guard rationale on
    test_users_list_status_column_uses_badge_styling."""
    response = admin_client.get("/admin/jobs")
    assert response.status_code == 200
    assert "badge badge-" in response.text


def test_jobs_register_api_requires_admin(logged_in_client):
    response = logged_in_client.get("/api/registers/admin_jobs")
    assert response.status_code == 403


def test_jobs_register_api_edit_requires_admin(logged_in_client, app):
    with app.state.session_factory() as session:
        job = enqueue_job(session, job_type="connection_test", payload={}, actor="admin")
        session.commit()
        job_id = job.id

    headers = {"X-CSRF-Token": logged_in_client.cookies.get("csrf_token")}
    response = logged_in_client.patch(
        f"/api/registers/admin_jobs/{job_id}",
        json={"fields": {}, "expected_updated_at": None},
        headers=headers,
    )
    assert response.status_code == 403
