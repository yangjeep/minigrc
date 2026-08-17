"""Router tests for issue #11's operational (login-only) occurrence routes:
/controls/{id}/occurrences/generate, /controls/{id}/occurrences, and
/occurrences/{id}/perform. Individual, single-control record-keeping stays
require_login — matching
docs/superpowers/specs/2026-08-15-issue11-control-operations-lifecycle-design.md
§6/§12.5's admin/login authorization split (structural/bulk actions in
tests/test_control_periods.py instead).
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app.models import ControlOccurrence, ControlOccurrenceEvidence, ControlPeriod, EvidenceSnapshot, Person
from tests.conftest import extract_csrf_token


def _make_calendar_control(app, **kwargs):
    from app.models import InternalControl

    defaults = {
        "name": "Quarterly access review",
        "cadence_type": "calendar",
        "cadence_interval_months": 3,
    }
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        control = InternalControl(**defaults)
        session.add(control)
        session.commit()
        session.refresh(control)
        return control.id


def _make_event_driven_control(app, **kwargs):
    from app.models import InternalControl

    defaults = {"name": "Ad hoc offboarding", "cadence_type": "event_driven"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        control = InternalControl(**defaults)
        session.add(control)
        session.commit()
        session.refresh(control)
        return control.id


def _make_manual_occurrence(app, control_id, **kwargs):
    from app.control_occurrences import record_occurrence_manually
    from app.models import InternalControl

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        occurrence = record_occurrence_manually(session, control, actor_type="user", **kwargs)
        session.commit()
        return occurrence.id


def test_generate_route_creates_occurrences_for_calendar_control(logged_in_client, app):
    control_id = _make_calendar_control(app)
    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        occurrences = session.scalars(
            select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)
        ).all()
        assert len(occurrences) >= 1
        assert all(o.origin == "generated" for o in occurrences)


def test_generate_route_requires_login(client):
    response = client.post(
        "/controls/deadbeefdeadbeefdeadbeefdeadbeef/occurrences/generate",
        data={"csrf_token": "x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_generate_route_404s_on_missing_control(admin_client):
    page = admin_client.get("/control-periods")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/controls/deadbeefdeadbeefdeadbeefdeadbeef/occurrences/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_create_manual_occurrence_for_event_driven_control(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences",
        data={"scope_note": "Ad hoc offboarding check", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        occurrence = session.scalar(
            select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)
        )
        assert occurrence is not None
        assert occurrence.origin == "manual"
        assert occurrence.scope_note == "Ad hoc offboarding check"
        assert occurrence.due_at is None


def test_create_manual_occurrence_with_due_on_and_active_period(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    with app.state.session_factory() as session:
        period = ControlPeriod(
            name="H1 2026",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            status="active",
            created_by="admin@example.com",
        )
        session.add(period)
        session.commit()
        period_id = period.id

    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences",
        data={
            "due_on": "2026-03-15",
            "control_period_id": period_id,
            "scope_note": "Q1 review",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        occurrence = session.scalar(
            select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)
        )
        assert occurrence.control_period_id == period_id
        assert occurrence.due_at is not None
        assert occurrence.due_at.date() == datetime.date(2026, 3, 15)


def test_create_manual_occurrence_rejects_non_active_period(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    with app.state.session_factory() as session:
        period = ControlPeriod(
            name="Planned period",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            created_by="admin@example.com",
        )
        session.add(period)
        session.commit()
        period_id = period.id

    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences",
        data={"control_period_id": period_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        assert (
            session.scalar(select(ControlOccurrence).where(ControlOccurrence.control_id == control_id))
            is None
        )


def test_create_manual_occurrence_404s_on_missing_period(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences",
        data={"control_period_id": "deadbeefdeadbeefdeadbeefdeadbeef", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_create_manual_occurrence_rejects_duplicate_due_date(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    # End-of-day UTC — the same due_on -> due_at convention the route itself
    # applies (app/routers/controls.py::_parse_due_on), so this collides.
    _make_manual_occurrence(
        app, control_id, due_at=datetime.datetime(2026, 3, 1, 23, 59, 59, 999999, tzinfo=datetime.UTC)
    )

    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "2026-03-01", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        count = len(
            session.scalars(select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)).all()
        )
        assert count == 1


def test_view_occurrence_404s_on_missing(logged_in_client):
    response = logged_in_client.get("/occurrences/deadbeefdeadbeefdeadbeefdeadbeef")
    assert response.status_code == 404


def test_view_occurrence_anonymous_redirected_to_login(client):
    response = client.get("/occurrences/deadbeefdeadbeefdeadbeefdeadbeef", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_perform_sets_fields(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)
    with app.state.session_factory() as session:
        person = Person(email="performer@example.com", display_name="Performer")
        session.add(person)
        session.commit()
        person_id = person.id

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-15",
            "performed_by_person_id": person_id,
            "scope_note": "Reviewed access list",
            "evidence_note": "Screenshot attached externally",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        assert occurrence.performed_at.date() == datetime.date(2026, 1, 15)
        assert occurrence.performed_by_person_id == person_id
        assert occurrence.scope_note == "Reviewed access list"
        assert occurrence.evidence_note == "Screenshot attached externally"

    # Reopening the form to correct a mistake must pre-fill the date from
    # the existing claim, not default back to today (adversarial review
    # finding, #11) — silently re-submitting would otherwise shift
    # occurred_at, an immutable event field, to today.
    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    assert 'value="2026-01-15"' in page.text


def test_perform_rejects_future_date(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)).date().isoformat()
    response = logged_in_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={"performed_on": future, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        assert occurrence.performed_at is None


def test_perform_rejects_unknown_person(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-15",
            "performed_by_person_id": "deadbeefdeadbeefdeadbeefdeadbeef",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        assert occurrence.performed_at is None


def test_perform_rejects_unknown_evidence_snapshot(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-15",
            "evidence_snapshot_id": "deadbeefdeadbeefdeadbeefdeadbeef",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        # Neither the performance claim nor a bogus evidence link should
        # have been recorded — validate-before-mutate.
        assert occurrence.performed_at is None
        assert (
            session.scalar(
                select(ControlOccurrenceEvidence).where(
                    ControlOccurrenceEvidence.occurrence_id == occurrence_id
                )
            )
            is None
        )


def test_perform_404s_on_missing_occurrence(logged_in_client):
    page = logged_in_client.get("/controls")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/occurrences/deadbeefdeadbeefdeadbeefdeadbeef/perform",
        data={"performed_on": "2026-01-15", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_perform_links_evidence(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)
    with app.state.session_factory() as session:
        snapshot = EvidenceSnapshot(
            source_type="aws_iam",
            check_key="mfa-enabled",
            title="MFA enforcement snapshot",
            raw_payload_sha256="a" * 64,
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={
            "performed_on": "2026-01-15",
            "evidence_snapshot_id": snapshot_id,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        link = session.scalar(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence_id)
        )
        assert link is not None
        assert link.evidence_snapshot_id == snapshot_id


def test_perform_duplicate_evidence_link_does_not_lose_performance(logged_in_client, app):
    """Regression guard for the savepoint fix in app/routers/occurrences.py:
    a duplicate evidence-link failure must not roll back the already-applied
    ControlOccurrencePerformed event in the same request."""
    control_id = _make_event_driven_control(app)
    occurrence_id = _make_manual_occurrence(app, control_id)
    with app.state.session_factory() as session:
        snapshot = EvidenceSnapshot(
            source_type="aws_iam",
            check_key="mfa-enabled",
            title="MFA enforcement snapshot",
            raw_payload_sha256="b" * 64,
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    data = {
        "performed_on": "2026-01-15",
        "scope_note": "First submission",
        "evidence_snapshot_id": snapshot_id,
        "csrf_token": csrf_token,
    }
    first = logged_in_client.post(f"/occurrences/{occurrence_id}/perform", data=data, follow_redirects=False)
    assert first.status_code == 303
    assert "flash_kind=error" not in first.headers["location"]

    page = logged_in_client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    data = {
        "performed_on": "2026-01-16",
        "scope_note": "Corrected submission",
        "evidence_snapshot_id": snapshot_id,
        "csrf_token": csrf_token,
    }
    second = logged_in_client.post(f"/occurrences/{occurrence_id}/perform", data=data, follow_redirects=False)
    assert second.status_code == 303
    assert "flash_kind=error" in second.headers["location"]

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        # The second call's ControlOccurrencePerformed event must still have
        # applied even though its evidence-link half failed as a duplicate.
        assert occurrence.performed_at.date() == datetime.date(2026, 1, 16)
        assert occurrence.scope_note == "Corrected submission"

        links = session.scalars(
            select(ControlOccurrenceEvidence).where(ControlOccurrenceEvidence.occurrence_id == occurrence_id)
        ).all()
        assert len(links) == 1


def test_set_control_owner_assigns_person(logged_in_client, app):
    """Regression guard, issue #11 adversarial review finding: owner_person_id
    — the FK snapshotted onto generated occurrences' responsible_person_id —
    must be settable through the running app via its own small form (the
    generic register grid has no person-picker FieldType)."""
    control_id = _make_event_driven_control(app)
    with app.state.session_factory() as session:
        person = Person(email="owner@example.com", display_name="Owner")
        session.add(person)
        session.commit()
        person_id = person.id

    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": person_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        from app.models import InternalControl

        control = session.get(InternalControl, control_id)
        assert control.owner_person_id == person_id


def test_set_control_owner_rejects_unknown_person(logged_in_client, app):
    control_id = _make_event_driven_control(app)
    page = logged_in_client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": "deadbeefdeadbeefdeadbeefdeadbeef", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_set_control_owner_404s_on_missing_control(logged_in_client):
    page = logged_in_client.get("/controls")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/controls/deadbeefdeadbeefdeadbeefdeadbeef/owner",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 404
