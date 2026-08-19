"""RBAC regression coverage (issue #37).

See docs/superpowers/specs/2026-08-19-issue37-rbac-design.md §1/§8 for the
full inventory this file closes: every route that used to be reachable by
any logged-in user (`require_login`) and now requires `require_write_access`
(`admin`/`operator`) must reject `reader`/`auditor` with 403 and leave no
state change, while `operator` (today's `logged_in_client`/`test_user`
default role) continues to succeed exactly as the old plain `user` role did.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.control_occurrences import record_occurrence_manually
from app.deps import require_write_access
from app.models import (
    ControlOccurrence,
    EvidenceSnapshot,
    Framework,
    FrameworkRequirement,
    InternalControl,
    Person,
    Policy,
    Risk,
    User,
    VendorSystem,
)
from app.requirements import add_requirement
from tests.conftest import extract_csrf_token

NON_WRITE_CLIENTS = ["reader_client", "auditor_client"]


# --- 1. Unit: permission matrix -------------------------------------------


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_require_write_access_allows_admin_and_operator(role):
    user = User(email=f"{role}@example.com", password_hash="x", role=role)
    assert require_write_access(user=user) is user


@pytest.mark.parametrize("role", ["reader", "auditor"])
def test_require_write_access_rejects_reader_and_auditor(role):
    user = User(email=f"{role}@example.com", password_hash="x", role=role)
    with pytest.raises(HTTPException) as exc_info:
        require_write_access(user=user)
    assert exc_info.value.status_code == 403


# --- 2. Per-route regression: controls.py ---------------------------------


def _make_control(app, **kwargs) -> str:
    defaults = {"name": "RBAC test control"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        control = InternalControl(**defaults)
        session.add(control)
        session.commit()
        session.refresh(control)
        return control.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_set_control_owner_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)
    with app.state.session_factory() as session:
        person = Person(email="rbac-owner@example.com", display_name="Owner")
        session.add(person)
        session.commit()
        person_id = person.id

    page = client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/controls/{control_id}/owner",
        data={"owner_person_id": person_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        assert control.owner_person_id is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_generate_control_occurrences_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app, cadence_type="calendar", cadence_interval_months=3)

    page = client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/controls/{control_id}/occurrences/generate",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        count = len(
            session.scalars(select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)).all()
        )
        assert count == 0


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_control_occurrence_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app, cadence_type="event_driven")

    page = client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/controls/{control_id}/occurrences",
        data={"due_on": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        count = len(
            session.scalars(select(ControlOccurrence).where(ControlOccurrence.control_id == control_id)).all()
        )
        assert count == 0


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_add_control_mapping_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)
    with app.state.session_factory() as session:
        framework = Framework(name="RBAC framework", version="1.0")
        session.add(framework)
        session.flush()
        requirement = add_requirement(session, framework, reference_code="R1", title="Requirement 1")
        session.commit()
        requirement_id = requirement.id

    page = client.get(f"/controls/{control_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/controls/{control_id}/mappings",
        data={"requirement_id": requirement_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        assert control.mappings == []


# --- 3. Per-route regression: occurrences.py ------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_perform_occurrence_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app, cadence_type="event_driven")
    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        occurrence = record_occurrence_manually(session, control, actor_type="user")
        session.commit()
        occurrence_id = occurrence.id

    page = client.get(f"/occurrences/{occurrence_id}")
    csrf_token = extract_csrf_token(page.text)
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    response = client.post(
        f"/occurrences/{occurrence_id}/perform",
        data={"performed_on": today, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        occurrence = session.get(ControlOccurrence, occurrence_id)
        assert occurrence.performed_at is None


# --- 4. Per-route regression: evidence.py ---------------------------------


def _make_evidence(app, **kwargs) -> str:
    defaults = {
        "source_type": "manual",
        "check_key": "rbac-check",
        "title": "RBAC evidence",
        "raw_payload_sha256": "0" * 64,
    }
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        snapshot = EvidenceSnapshot(**defaults)
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_map_requirement_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    evidence_id = _make_evidence(app)
    with app.state.session_factory() as session:
        framework = Framework(name="RBAC framework", version="1.0")
        session.add(framework)
        session.flush()
        requirement = add_requirement(session, framework, reference_code="R1", title="Requirement 1")
        session.commit()
        requirement_id = requirement.id

    page = client.get(f"/evidence/{evidence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/evidence/{evidence_id}/map-requirement",
        data={"requirement_id": requirement_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        snapshot = session.get(EvidenceSnapshot, evidence_id)
        assert snapshot.requirement_mappings == []


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_map_control_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    evidence_id = _make_evidence(app)
    control_id = _make_control(app)

    page = client.get(f"/evidence/{evidence_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/evidence/{evidence_id}/map-control",
        data={"control_id": control_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        snapshot = session.get(EvidenceSnapshot, evidence_id)
        assert snapshot.control_mappings == []


# --- 5. Per-route regression: people.py -----------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_person_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/people/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/people",
        data={"email": "rbac-new-person@example.com", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        person = session.scalar(select(Person).where(Person.email == "rbac-new-person@example.com"))
        assert person is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_update_person_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    with app.state.session_factory() as session:
        person = Person(email="rbac-existing-person@example.com", display_name="Original Name")
        session.add(person)
        session.commit()
        person_id = person.id

    page = client.get(f"/people/{person_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/people/{person_id}/edit",
        data={"display_name": "Tampered Name", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        person = session.get(Person, person_id)
        assert person.display_name == "Original Name"


# --- 6. Per-route regression: policies.py ---------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_policy_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/policies/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/policies",
        data={"title": "RBAC test policy", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        policy = session.scalar(select(Policy).where(Policy.title == "RBAC test policy"))
        assert policy is None


def _make_policy(app, **kwargs) -> str:
    defaults = {"title": "RBAC existing policy"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        policy = Policy(**defaults)
        session.add(policy)
        session.commit()
        session.refresh(policy)
        return policy.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_update_policy_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    policy_id = _make_policy(app)

    page = client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/policies/{policy_id}",
        data={"title": "Tampered title", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.title == "RBAC existing policy"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_upload_policy_version_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    policy_id = _make_policy(app)

    page = client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/policies/{policy_id}/versions",
        data={"csrf_token": csrf_token},
        files={"file": ("policy.pdf", b"%PDF-1.4 fake", "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.versions == []


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_retire_policy_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    policy_id = _make_policy(app, status="effective")

    page = client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/policies/{policy_id}/retire",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        policy = session.get(Policy, policy_id)
        assert policy.status == "effective"


# --- 7. Per-route regression: risks.py ------------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_risk_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/risks",
        data={"title": "RBAC test risk", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        risk = session.scalar(select(Risk).where(Risk.title == "RBAC test risk"))
        assert risk is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_import_risks_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/risks")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/risks/import",
        data={"csrf_token": csrf_token},
        files={"file": ("risks.csv", b"title,description\nRBAC import risk,\n", "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        risk = session.scalar(select(Risk).where(Risk.title == "RBAC import risk"))
        assert risk is None


# --- 8. Per-route regression: frameworks.py -------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_framework_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/frameworks/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/frameworks",
        data={"name": "RBAC test framework", "version": "1.0", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        framework = session.scalar(select(Framework).where(Framework.name == "RBAC test framework"))
        assert framework is None


def _make_framework(app, **kwargs) -> str:
    defaults = {"name": "RBAC existing framework", "version": "1.0"}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        framework = Framework(**defaults)
        session.add(framework)
        session.commit()
        session.refresh(framework)
        return framework.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_update_framework_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)

    page = client.get(f"/frameworks/{framework_id}/edit")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/frameworks/{framework_id}/edit",
        data={"name": "Tampered name", "version": "9.9", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        framework = session.get(Framework, framework_id)
        assert framework.name == "RBAC existing framework"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_requirement_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)

    page = client.get(f"/frameworks/{framework_id}/requirements/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/frameworks/{framework_id}/requirements",
        data={"reference_code": "RBAC-1", "title": "RBAC test requirement", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        requirement = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.reference_code == "RBAC-1")
        )
        assert requirement is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_import_requirements_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)

    page = client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files={
            "file": (
                "requirements.csv",
                b"reference_code,title\nRBAC-IMPORT-1,Imported requirement\n",
                "text/csv",
            )
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        requirement = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.reference_code == "RBAC-IMPORT-1")
        )
        assert requirement is None


def _make_requirement(app, framework_id: str, **kwargs) -> str:
    with app.state.session_factory() as session:
        framework = session.get(Framework, framework_id)
        defaults = {"reference_code": "RBAC-EXISTING-1", "title": "RBAC existing requirement"}
        defaults.update(kwargs)
        requirement = add_requirement(session, framework, **defaults)
        session.commit()
        return requirement.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_update_assessment_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)
    requirement_id = _make_requirement(app, framework_id)

    page = client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "no",
            "implementation_state": "not_started",
            "note_body": "attempted tamper",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        requirement = session.get(FrameworkRequirement, requirement_id)
        assert requirement.assessment.applicable == "yes"
        assert requirement.notes == []


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_add_note_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)
    requirement_id = _make_requirement(app, framework_id)

    page = client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/notes",
        data={"body": "attempted note", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        requirement = session.get(FrameworkRequirement, requirement_id)
        assert requirement.notes == []


# --- 9. Per-route regression: vendor_systems.py ---------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_vendor_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    page = client.get("/vendors/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/vendors",
        data={
            "system_name": "RBAC test vendor",
            "vendor_name": "RBAC Vendor Inc.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        vendor = session.scalar(select(VendorSystem).where(VendorSystem.system_name == "RBAC test vendor"))
        assert vendor is None


def _make_vendor(app, **kwargs) -> str:
    defaults = {"system_name": "RBAC existing vendor", "vendor_name": "RBAC Existing Vendor Inc."}
    defaults.update(kwargs)
    with app.state.session_factory() as session:
        vendor = VendorSystem(**defaults)
        session.add(vendor)
        session.commit()
        session.refresh(vendor)
        return vendor.id


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_update_vendor_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    vendor_id = _make_vendor(app)

    page = client.get(f"/vendors/{vendor_id}/edit")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/vendors/{vendor_id}/edit",
        data={
            "system_name": "Tampered vendor name",
            "vendor_name": "Tampered Vendor Inc.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        vendor = session.get(VendorSystem, vendor_id)
        assert vendor.system_name == "RBAC existing vendor"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_create_roster_snapshot_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    vendor_id = _make_vendor(app)

    page = client.get(f"/vendors/{vendor_id}/roster/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        f"/vendors/{vendor_id}/roster",
        data={"csrf_token": csrf_token},
        files={"file": ("roster.csv", b"email,status\nrbac@example.com,active\n", "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        from app.vendor_roster_import import latest_snapshot

        assert latest_snapshot(session, vendor_id) is None


# --- 10. Register-grid regression ------------------------------------------


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_controls_register_create_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    response = client.post(
        "/api/registers/controls",
        json={"name": "RBAC register control"},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        control = session.scalar(
            select(InternalControl).where(InternalControl.name == "RBAC register control")
        )
        assert control is None


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_controls_register_edit_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    control_id = _make_control(app)
    client.get("/controls")  # ensure a csrf_token cookie is set for this client

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        expected_updated_at = control.updated_at.isoformat()

    response = client.patch(
        f"/api/registers/controls/{control_id}",
        json={"fields": {"name": "Tampered via register"}, "expected_updated_at": expected_updated_at},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        control = session.get(InternalControl, control_id)
        assert control.name == "RBAC test control"


@pytest.mark.parametrize("client_fixture", NON_WRITE_CLIENTS)
def test_framework_requirements_register_edit_requires_write_access(client_fixture, app, request):
    client = request.getfixturevalue(client_fixture)
    framework_id = _make_framework(app)
    requirement_id = _make_requirement(app, framework_id)
    client.get("/frameworks")  # ensure a csrf_token cookie is set for this client

    with app.state.session_factory() as session:
        requirement = session.get(FrameworkRequirement, requirement_id)
        expected_updated_at = requirement.updated_at.isoformat()

    response = client.patch(
        f"/api/registers/framework-requirements/{requirement_id}",
        json={"fields": {"title": "Tampered via register"}, "expected_updated_at": expected_updated_at},
        headers={"X-CSRF-Token": client.cookies.get("csrf_token")},
    )
    assert response.status_code == 403

    with app.state.session_factory() as session:
        requirement = session.get(FrameworkRequirement, requirement_id)
        assert requirement.title == "RBAC existing requirement"


# --- 11. Operator success sanity (the 1:1 rename didn't regress writes) ---


def test_operator_can_still_create_control_via_register(logged_in_client, app):
    """logged_in_client's test_user has no explicit role, so this exercises
    the model's default (now "operator", issue #37) end to end — the same
    default that would apply to any freshly-created account."""
    response = logged_in_client.post(
        "/api/registers/controls",
        json={"name": "Operator-created control"},
        headers={"X-CSRF-Token": logged_in_client.cookies.get("csrf_token")},
    )
    assert response.status_code == 201, response.text

    with app.state.session_factory() as session:
        control = session.scalar(
            select(InternalControl).where(InternalControl.name == "Operator-created control")
        )
        assert control is not None
