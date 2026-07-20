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


def test_jobs_register_api_requires_admin(logged_in_client):
    response = logged_in_client.get("/api/registers/admin_jobs")
    assert response.status_code == 403
