"""Router tests for issue #11's /control-periods routes: structural,
cross-control, period-wide actions (create/activate/generate/close) are
require_admin; viewing is require_login — matching
docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§6/§12.5's admin/login authorization split.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, ControlOccurrence, ControlPeriod, InternalControl
from tests.conftest import extract_csrf_token


def _create_period(client, name="Test period", starts_on="2026-01-01", ends_on="2026-06-30"):
    page = client.get("/control-periods")
    csrf_token = extract_csrf_token(page.text)
    return client.post(
        "/control-periods",
        data={"name": name, "starts_on": starts_on, "ends_on": ends_on, "csrf_token": csrf_token},
        follow_redirects=False,
    )


def test_admin_can_create_control_period(admin_client, app):
    response = _create_period(admin_client)
    assert response.status_code == 303

    with app.state.session_factory() as session:
        period = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period"))
        assert period is not None
        assert period.status == "planned"

        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_type == "control_period", AuditEvent.action == "create"
            )
        )
        assert audit is not None
        assert audit.actor == "root-admin@example.com"


def test_non_admin_cannot_create_control_period(logged_in_client, app):
    response = _create_period(logged_in_client)
    assert response.status_code == 403

    with app.state.session_factory() as session:
        assert session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")) is None


def test_anonymous_redirected_to_login(client):
    response = client.get("/control-periods", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_control_period_rejects_end_before_start(admin_client, app):
    response = _create_period(admin_client, starts_on="2026-06-30", ends_on="2026-01-01")
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")) is None


def test_activate_transitions_planned_to_active(admin_client, app):
    _create_period(admin_client)
    with app.state.session_factory() as session:
        period = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period"))
        period_id = period.id

    page = admin_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/control-periods/{period_id}/activate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        period = session.get(ControlPeriod, period_id)
        assert period.status == "active"


def test_non_admin_cannot_activate_period(admin_client, logged_in_client, app):
    _create_period(admin_client)
    with app.state.session_factory() as session:
        period_id = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")).id

    page = logged_in_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/control-periods/{period_id}/activate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_close_locks_period_and_records_closed_at(admin_client, app):
    _create_period(admin_client)
    with app.state.session_factory() as session:
        period_id = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")).id

    page = admin_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(f"/control-periods/{period_id}/activate", data={"csrf_token": csrf_token})

    page = admin_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/control-periods/{period_id}/close", data={"csrf_token": csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        period = session.get(ControlPeriod, period_id)
        assert period.status == "closed"
        assert period.closed_at is not None


def test_generate_occurrences_route_creates_occurrences_for_calendar_controls(admin_client, app):
    with app.state.session_factory() as session:
        control = InternalControl(
            name="Quarterly access review", cadence_type="calendar", cadence_interval_months=3
        )
        session.add(control)
        session.commit()

    _create_period(admin_client)
    with app.state.session_factory() as session:
        period_id = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")).id

    page = admin_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(f"/control-periods/{period_id}/activate", data={"csrf_token": csrf_token})

    page = admin_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/control-periods/{period_id}/generate-occurrences",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        occurrences = session.scalars(
            select(ControlOccurrence).where(ControlOccurrence.control_period_id == period_id)
        ).all()
        assert len(occurrences) == 2


def test_generate_occurrences_route_requires_admin(logged_in_client, admin_client, app):
    _create_period(admin_client)
    with app.state.session_factory() as session:
        period_id = session.scalar(select(ControlPeriod).where(ControlPeriod.name == "Test period")).id

    page = logged_in_client.get(f"/control-periods/{period_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/control-periods/{period_id}/generate-occurrences",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_view_nonexistent_period_returns_404(logged_in_client):
    response = logged_in_client.get("/control-periods/deadbeefdeadbeefdeadbeefdeadbeef")
    assert response.status_code == 404
