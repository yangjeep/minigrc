"""Contract tests for the scoped/read-only-field register API (Feature 3).

Exercises the FrameworkRequirement registration — the nested case where a
grid row's editable fields live on one model (FrameworkRequirement) but
read-only fields are computed from a related one (RequirementAssessment).
See docs/superpowers/specs/2026-07-20-feature3-requirements-grid-design.md.
"""

from __future__ import annotations

from app.models import AuditEvent, Framework, FrameworkRequirement, RequirementAssessment

REGISTER_URL = "/api/registers/framework-requirements"


def _csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


def _make_framework_with_requirement(app) -> tuple[str, str]:
    with app.state.session_factory() as session:
        framework = Framework(name="Grid Test Framework", version="1.0", description="")
        session.add(framework)
        session.flush()
        requirement = FrameworkRequirement(
            framework_id=framework.id,
            reference_code="G.1",
            title="Grid requirement",
            summary="",
            display_order=1,
        )
        session.add(requirement)
        session.flush()
        session.add(
            RequirementAssessment(requirement_id=requirement.id, applicable="yes", owner="owner@example.com")
        )
        session.commit()
        return framework.id, requirement.id


def test_list_requires_scope_param(logged_in_client, app):
    framework_id, _ = _make_framework_with_requirement(app)
    response = logged_in_client.get(REGISTER_URL)
    assert response.status_code == 400


def test_list_filters_by_framework_id(logged_in_client, app):
    framework_id, requirement_id = _make_framework_with_requirement(app)
    other_framework_id, _ = _make_framework_with_requirement(app)

    response = logged_in_client.get(REGISTER_URL, params={"framework_id": framework_id})
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert requirement_id in ids

    other_response = logged_in_client.get(REGISTER_URL, params={"framework_id": other_framework_id})
    other_ids = [row["id"] for row in other_response.json()]
    assert requirement_id not in other_ids


def test_row_includes_read_only_assessment_fields(logged_in_client, app):
    framework_id, requirement_id = _make_framework_with_requirement(app)
    response = logged_in_client.get(REGISTER_URL, params={"framework_id": framework_id})
    row = next(r for r in response.json() if r["id"] == requirement_id)
    assert row["applicable"] == "yes"
    assert row["owner"] == "owner@example.com"


def test_patch_updates_catalogue_field(logged_in_client, app):
    framework_id, requirement_id = _make_framework_with_requirement(app)
    row = next(
        r
        for r in logged_in_client.get(REGISTER_URL, params={"framework_id": framework_id}).json()
        if r["id"] == requirement_id
    )
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{requirement_id}",
        json={"fields": {"title": "Renamed requirement"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Renamed requirement"


def test_patch_rejects_read_only_assessment_field(logged_in_client, app):
    framework_id, requirement_id = _make_framework_with_requirement(app)
    row = next(
        r
        for r in logged_in_client.get(REGISTER_URL, params={"framework_id": framework_id}).json()
        if r["id"] == requirement_id
    )
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{requirement_id}",
        json={"fields": {"applicable": "no"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422
    with app.state.session_factory() as session:
        assessment = session.get(
            RequirementAssessment, session.get(FrameworkRequirement, requirement_id).assessment.id
        )
        assert assessment.applicable == "yes"  # unchanged — read-only field explicitly rejected


def test_create_route_not_mounted(logged_in_client):
    response = logged_in_client.post(
        REGISTER_URL,
        json={"framework_id": "x", "reference_code": "X.1", "title": "x"},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 405


def test_delete_route_not_mounted(logged_in_client, app):
    _, requirement_id = _make_framework_with_requirement(app)
    response = logged_in_client.delete(
        f"{REGISTER_URL}/{requirement_id}", headers=_csrf_headers(logged_in_client)
    )
    assert response.status_code == 405


def test_patch_writes_audit_event(logged_in_client, app):
    framework_id, requirement_id = _make_framework_with_requirement(app)
    row = next(
        r
        for r in logged_in_client.get(REGISTER_URL, params={"framework_id": framework_id}).json()
        if r["id"] == requirement_id
    )
    logged_in_client.patch(
        f"{REGISTER_URL}/{requirement_id}",
        json={"fields": {"summary": "Updated summary"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    with app.state.session_factory() as session:
        events = (
            session.query(AuditEvent)
            .filter(AuditEvent.entity_type == "framework_requirement", AuditEvent.entity_id == requirement_id)
            .all()
        )
    assert any(e.action == "update" for e in events)
