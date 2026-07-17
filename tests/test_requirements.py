from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, Framework, FrameworkRequirement, RequirementNote
from tests.conftest import extract_csrf_token


def _seeded_framework_id(app) -> str:
    with app.state.session_factory() as session:
        return session.scalar(select(Framework)).id


def _first_requirement_id(app, framework_id: str) -> str:
    with app.state.session_factory() as session:
        req = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework_id)
        )
        return req.id


def test_framework_creation(logged_in_client):
    page = logged_in_client.get("/frameworks/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/frameworks",
        data={"name": "SOC 2", "version": "2017", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    listing = logged_in_client.get("/frameworks")
    assert b"SOC 2" in listing.content


def test_requirement_creation(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements",
        data={"reference_code": "Z.9", "title": "Manually added requirement", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"Z.9" in detail.content


def test_duplicate_reference_code_rejected(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements",
        data={
            "reference_code": "A.5.1",
            "title": "Duplicate of seeded requirement",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_csv_import_success(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = (
        "reference_code,title,description,display_order\nX.1,Imported req,desc,1\nX.2,Second req,,2\n"
    )
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Imported+2" in response.headers["location"]

    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"X.1" in detail.content
    assert b"X.2" in detail.content


def test_oversize_csv_rejected_without_touching_database(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    with app.state.session_factory() as session:
        before_count = session.scalar(
            select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework_id)
        )
        before_ids = {
            r.id
            for r in session.scalars(
                select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework_id)
            ).all()
        }

    app.state.settings.max_upload_mb = 0  # any non-empty file now exceeds the cap

    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = "reference_code,title,description,display_order\nOV.1,Oversized row,,1\n"
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "maximum" in response.headers["location"].lower()

    with app.state.session_factory() as session:
        after_ids = {
            r.id
            for r in session.scalars(
                select(FrameworkRequirement).where(FrameworkRequirement.framework_id == framework_id)
            ).all()
        }
    assert after_ids == before_ids
    assert before_count is not None  # sanity: the seeded framework did have requirements
    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"OV.1" not in detail.content


def test_csv_within_size_limit_still_imports(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    app.state.settings.max_upload_mb = 25  # default cap, well above the small test file

    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = "reference_code,title,description,display_order\nBND.1,Bounded row,,1\n"
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Imported+1" in response.headers["location"]

    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"BND.1" in detail.content


def test_malformed_csv_rolls_back_entirely(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = "reference_code,title,description,display_order\nY.1,Good row,,1\n,Missing code,,2\n"
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"Y.1" not in detail.content


def test_csv_import_missing_columns_rejected(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = "title\nNo reference code column\n"
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_duplicate_reference_code_within_csv_rejected(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    page = logged_in_client.get(f"/frameworks/{framework_id}")
    csrf_token = extract_csrf_token(page.text)
    csv_content = "reference_code,title,description,display_order\nW.1,First,,1\nW.1,Duplicate,,2\n"
    files = {"file": ("import.csv", csv_content, "text/csv")}
    response = logged_in_client.post(
        f"/frameworks/{framework_id}/import",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    detail = logged_in_client.get(f"/frameworks/{framework_id}")
    assert b"W.1" not in detail.content


def test_assessment_state_update(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "yes",
            "implementation_state": "implemented",
            "owner": "security-lead@example.com",
            "note_body": "",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    detail = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    assert b"implemented" in detail.content
    assert b"security-lead@example.com" in detail.content


def test_invalid_assessment_state_rejected(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "yes",
            "implementation_state": "totally_bogus_state",
            "owner": "",
            "note_body": "",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_not_applicable_requires_a_note(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    rejected = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "no",
            "implementation_state": "not_started",
            "owner": "",
            "note_body": "   ",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 303
    assert "flash_kind=error" in rejected.headers["location"]

    accepted = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "no",
            "implementation_state": "not_started",
            "owner": "",
            "note_body": "Not relevant to our environment.",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert "flash_kind=success" in accepted.headers["location"]


def test_note_history_is_append_only_and_ordered(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    for body in ["First note", "Second note", "Third note"]:
        logged_in_client.post(
            f"/frameworks/{framework_id}/requirements/{requirement_id}/notes",
            data={"body": body, "csrf_token": csrf_token},
        )

    with app.state.session_factory() as session:
        notes = session.scalars(
            select(RequirementNote)
            .where(RequirementNote.requirement_id == requirement_id)
            .order_by(RequirementNote.created_at)
        ).all()
    assert [n.body for n in notes] == ["First note", "Second note", "Third note"]


def test_empty_note_rejected(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/notes",
        data={"body": "    ", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        count = session.scalar(
            select(RequirementNote).where(RequirementNote.requirement_id == requirement_id)
        )
    assert count is None


def test_audit_events_created_for_assessment_and_notes(logged_in_client, app):
    framework_id = _seeded_framework_id(app)
    requirement_id = _first_requirement_id(app, framework_id)
    page = logged_in_client.get(f"/frameworks/{framework_id}/requirements/{requirement_id}")
    csrf_token = extract_csrf_token(page.text)

    logged_in_client.post(
        f"/frameworks/{framework_id}/requirements/{requirement_id}/assessment",
        data={
            "applicable": "yes",
            "implementation_state": "in_progress",
            "owner": "me",
            "note_body": "Started working on this.",
            "csrf_token": csrf_token,
        },
    )

    with app.state.session_factory() as session:
        requirement = session.get(FrameworkRequirement, requirement_id)
        assessment_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "requirement_assessment",
                AuditEvent.entity_id == requirement.assessment.id,
            )
        ).all()
        note_events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "requirement_note", AuditEvent.entity_id == requirement_id
            )
        ).all()
    assert len(assessment_events) >= 1
    assert len(note_events) >= 1


def test_completion_percentage_with_no_applicable_requirements(logged_in_client):
    page = logged_in_client.get("/frameworks/new")
    csrf_token = extract_csrf_token(page.text)
    logged_in_client.post(
        "/frameworks",
        data={"name": "Empty Framework", "version": "1.0", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    listing = logged_in_client.get("/frameworks")
    assert b"N/A" in listing.content
