"""Route-level tests for issue #14's readiness queue on the dashboard
(app/routers/dashboard.py's `framework_id` filter and queue rendering).
Domain-level tests live in tests/test_readiness.py.
"""

from __future__ import annotations

from app.finding_lifecycle import open_finding
from app.models import (
    ControlRequirementMapping,
    Finding,
    Framework,
    FrameworkRequirement,
    InternalControl,
    Person,
)


def _make_control(app, **kwargs):
    defaults = {"name": "Test control", "owner": "owner@example.com"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        control = InternalControl(**defaults)
        session.add(control)
        session.commit()
        session.refresh(control)
        return control.id


def test_dashboard_shows_empty_queue_message_on_a_healthy_state(logged_in_client):
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Nothing needs attention right now." in response.text


def test_dashboard_shows_finding_needing_attention(logged_in_client, app):
    with app.state.session_factory() as session:
        finding = open_finding(session, title="Access review skipped", severity="high")
        session.commit()
        finding_id = finding.id

    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Access review skipped" not in response.text  # queue shows reason, not the title
    assert f"/findings/{finding_id}" in response.text
    assert "No owner assigned" in response.text


def test_dashboard_framework_filter_narrows_the_queue(logged_in_client, app):
    control_id = _make_control(app, owner="")
    with app.state.session_factory() as session:
        framework_a = Framework(name="Filter Framework A", version="1")
        framework_b = Framework(name="Filter Framework B", version="1")
        session.add_all([framework_a, framework_b])
        session.flush()
        requirement_a = FrameworkRequirement(framework_id=framework_a.id, reference_code="A.1", title="A1")
        session.add(requirement_a)
        session.flush()
        session.add(ControlRequirementMapping(control_id=control_id, requirement_id=requirement_a.id))
        session.commit()
        framework_a_id = framework_a.id
        framework_b_id = framework_b.id

    response_a = logged_in_client.get(f"/?framework_id={framework_a_id}")
    response_b = logged_in_client.get(f"/?framework_id={framework_b_id}")
    assert f"/controls/{control_id}" in response_a.text
    assert f"/controls/{control_id}" not in response_b.text


def test_dashboard_readiness_queue_visible_to_reader(reader_client, app):
    with app.state.session_factory() as session:
        open_finding(session, title="x", severity="low")
        session.commit()

    response = reader_client.get("/")
    assert response.status_code == 200
    assert "No owner assigned" in response.text


def test_dashboard_resolving_the_underlying_finding_removes_the_queue_item(logged_in_client, app):
    with app.state.session_factory() as session:
        person = Person(email="owner@example.com")
        session.add(person)
        session.flush()
        person_id = person.id
        finding = open_finding(session, title="x", severity="low")
        session.commit()
        finding_id = finding.id

    before = logged_in_client.get("/")
    assert f"/findings/{finding_id}" in before.text

    # Resolve the ownership gap directly through the domain layer (the
    # findings router has no "assign owner" route yet — this proves the
    # queue reflects real state regardless of how it changed).
    with app.state.session_factory() as session:
        finding = session.get(Finding, finding_id)
        finding.owner_person_id = person_id
        session.commit()

    after = logged_in_client.get("/")
    assert "No owner assigned" not in after.text
