"""Contract tests for the generic register JSON API (app/registers/).

Exercised against the Controls registration (app/routers/controls.py) since
Controls is the reference migration for Feature 2 of the platform pivot —
see docs/superpowers/specs/2026-07-20-feature2-register-grid-design.md.
"""

from __future__ import annotations

from app.models import AuditEvent

REGISTER_URL = "/api/registers/controls"


def _csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


def _create_control(client, **overrides) -> dict:
    payload = {
        "name": "Encryption at rest",
        "owner": "security@example.com",
        "status": "not_started",
        "review_frequency": "annual",
        "description": "Disks encrypted with provider-managed keys.",
    }
    payload.update(overrides)
    response = client.post(REGISTER_URL, json=payload, headers=_csrf_headers(client))
    assert response.status_code == 201, response.text
    return response.json()


def test_list_requires_login(client):
    response = client.get(REGISTER_URL, follow_redirects=False)
    assert response.status_code == 303


def test_list_returns_seeded_and_created_rows(logged_in_client):
    created = _create_control(logged_in_client, name="Vendor risk review")
    response = logged_in_client.get(REGISTER_URL)
    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert "Vendor risk review" in names
    assert created["id"] in [row["id"] for row in response.json()]


def test_create_row_returns_full_row_with_timestamp(logged_in_client):
    row = _create_control(logged_in_client, name="Access review")
    assert row["name"] == "Access review"
    assert row["status"] == "not_started"
    assert "updated_at" in row and row["updated_at"]
    assert "id" in row and len(row["id"]) == 32


def test_create_row_rejects_missing_required_field(logged_in_client):
    response = logged_in_client.post(
        REGISTER_URL,
        json={"owner": "x", "status": "not_started", "review_frequency": "annual", "description": ""},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422
    assert "name" in response.json()["detail"]


def test_create_row_rejects_bad_enum_value(logged_in_client):
    response = logged_in_client.post(
        REGISTER_URL,
        json={
            "name": "Bad status",
            "owner": "",
            "status": "on_fire",
            "review_frequency": "annual",
            "description": "",
        },
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422
    assert "status" in response.json()["detail"]


def test_create_row_requires_csrf_header(logged_in_client):
    response = logged_in_client.post(
        REGISTER_URL,
        json={
            "name": "No CSRF",
            "owner": "",
            "status": "not_started",
            "review_frequency": "annual",
            "description": "",
        },
    )
    assert response.status_code == 400


def test_patch_updates_field_and_bumps_updated_at(logged_in_client):
    row = _create_control(logged_in_client, name="Patch me")
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"status": "implemented"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["status"] == "implemented"
    assert updated["updated_at"] != row["updated_at"]


def test_patch_rejects_stale_updated_at_with_409(logged_in_client):
    row = _create_control(logged_in_client, name="Conflict me")
    stale = row["updated_at"]
    logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"owner": "first-writer"}, "expected_updated_at": stale},
        headers=_csrf_headers(logged_in_client),
    )
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"owner": "second-writer"}, "expected_updated_at": stale},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["current"]["owner"] == "first-writer"


def test_patch_rejects_bad_enum_value(logged_in_client):
    row = _create_control(logged_in_client, name="Bad patch")
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"review_frequency": "never"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422


def test_delete_removes_row(logged_in_client):
    row = _create_control(logged_in_client, name="Delete me")
    response = logged_in_client.delete(f"{REGISTER_URL}/{row['id']}", headers=_csrf_headers(logged_in_client))
    assert response.status_code == 204
    listing = logged_in_client.get(REGISTER_URL).json()
    assert row["id"] not in [r["id"] for r in listing]


def test_bulk_update_all_or_nothing_on_one_bad_row(logged_in_client):
    good = _create_control(logged_in_client, name="Bulk good")
    other = _create_control(logged_in_client, name="Bulk other")
    response = logged_in_client.post(
        f"{REGISTER_URL}/bulk",
        json={
            "updates": [
                {
                    "id": good["id"],
                    "fields": {"status": "implemented"},
                    "expected_updated_at": good["updated_at"],
                },
                {
                    "id": other["id"],
                    "fields": {"status": "not_a_status"},
                    "expected_updated_at": other["updated_at"],
                },
            ]
        },
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422
    listing = {row["id"]: row for row in logged_in_client.get(REGISTER_URL).json()}
    assert listing[good["id"]]["status"] == "not_started"  # unchanged — bulk rolled back


def test_bulk_update_applies_when_all_valid(logged_in_client):
    a = _create_control(logged_in_client, name="Bulk a")
    b = _create_control(logged_in_client, name="Bulk b")
    response = logged_in_client.post(
        f"{REGISTER_URL}/bulk",
        json={
            "updates": [
                {"id": a["id"], "fields": {"status": "implemented"}, "expected_updated_at": a["updated_at"]},
                {"id": b["id"], "fields": {"status": "in_progress"}, "expected_updated_at": b["updated_at"]},
            ]
        },
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 200, response.text
    listing = {row["id"]: row for row in logged_in_client.get(REGISTER_URL).json()}
    assert listing[a["id"]]["status"] == "implemented"
    assert listing[b["id"]]["status"] == "in_progress"


def test_patch_rejects_unknown_field_name(logged_in_client):
    row = _create_control(logged_in_client, name="Unknown field target")
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"id": "attacker-controlled-id"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 422
    assert "id" in response.json()["detail"]


def test_any_logged_in_user_can_create_and_edit(logged_in_client):
    # Controls CRUD is ordinary GRC data — no admin gating, per decisions #8/#12.
    row = _create_control(logged_in_client, name="Non-admin create")
    response = logged_in_client.patch(
        f"{REGISTER_URL}/{row['id']}",
        json={"fields": {"owner": "someone"}, "expected_updated_at": row["updated_at"]},
        headers=_csrf_headers(logged_in_client),
    )
    assert response.status_code == 200


def test_create_writes_audit_event(logged_in_client, app):
    row = _create_control(logged_in_client, name="Audited control")
    with app.state.session_factory() as session:
        events = (
            session.query(AuditEvent)
            .filter(AuditEvent.entity_type == "control", AuditEvent.entity_id == row["id"])
            .all()
        )
    actions = [e.action for e in events]
    assert "create" in actions


def test_delete_writes_audit_event(logged_in_client, app):
    row = _create_control(logged_in_client, name="Audited delete")
    logged_in_client.delete(f"{REGISTER_URL}/{row['id']}", headers=_csrf_headers(logged_in_client))
    with app.state.session_factory() as session:
        events = (
            session.query(AuditEvent)
            .filter(AuditEvent.entity_type == "control", AuditEvent.entity_id == row["id"])
            .all()
        )
    actions = [e.action for e in events]
    assert "delete" in actions
