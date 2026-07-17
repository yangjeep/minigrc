from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, Person, VendorSystem, VendorUserSnapshot, VendorUserSnapshotRow
from tests.conftest import extract_csrf_token

ROSTER_CSV_V1 = (
    "email,name,role,status,last_login_at\n"
    "alice@example.com,Alice,Admin,active,2026-07-01T12:00:00Z\n"
    "bob@example.com,Bob,Member,active,2026-06-28T09:30:00Z\n"
)

ROSTER_CSV_V2 = (
    "email,name,role,status,last_login_at\n"
    "alice@example.com,Alice,Admin,active,2026-07-10T12:00:00Z\n"
    "carol@example.com,Carol,Admin,active,2026-07-11T09:30:00Z\n"
)


def _create_vendor(client) -> str:
    page = client.get("/vendors/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/vendors",
        data={"system_name": "GitHub", "vendor_name": "GitHub, Inc.", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def _import_roster(client, vendor_id: str, csv_content: str):
    page = client.get(f"/vendors/{vendor_id}/roster/new")
    csrf_token = extract_csrf_token(page.text)
    files = {"file": ("roster.csv", csv_content, "text/csv")}
    return client.post(
        f"/vendors/{vendor_id}/roster",
        data={"csrf_token": csrf_token},
        files=files,
        follow_redirects=False,
    )


def test_import_creates_snapshot(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    response = _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)
    assert response.status_code == 303
    assert "Imported+2" in response.headers["location"]

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot).where(VendorUserSnapshot.vendor_system_id == vendor_id)
        ).all()
        assert len(snapshots) == 1
        assert snapshots[0].row_count == 2
        rows = session.scalars(
            select(VendorUserSnapshotRow).where(VendorUserSnapshotRow.snapshot_id == snapshots[0].id)
        ).all()
        assert {r.normalized_email for r in rows} == {"alice@example.com", "bob@example.com"}


def test_import_updates_roster_last_confirmed_at(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)
    with app.state.session_factory() as session:
        vendor = session.get(VendorSystem, vendor_id)
        assert vendor.roster_last_confirmed_at is not None


def test_second_import_creates_new_snapshot_not_overwrite(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V2)

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot)
            .where(VendorUserSnapshot.vendor_system_id == vendor_id)
            .order_by(VendorUserSnapshot.imported_at)
        ).all()
    assert len(snapshots) == 2  # first snapshot preserved, not overwritten


def test_duplicate_normalized_email_within_file_rejected(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    csv_content = (
        "email,name,role,status,last_login_at\n"
        "Alice@Example.com,Alice,Admin,active,\n"
        "alice@example.com,Alice Dup,Member,active,\n"
    )
    response = _import_roster(logged_in_client, vendor_id, csv_content)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot).where(VendorUserSnapshot.vendor_system_id == vendor_id)
        ).all()
    assert snapshots == []  # nothing written


def test_missing_required_column_rejected(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    response = _import_roster(logged_in_client, vendor_id, "email,name\nalice@example.com,Alice\n")
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot).where(VendorUserSnapshot.vendor_system_id == vendor_id)
        ).all()
    assert snapshots == []


def test_oversize_roster_csv_rejected_without_writing(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    app.state.settings.max_upload_mb = 0

    response = _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "maximum" in response.headers["location"].lower()

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot).where(VendorUserSnapshot.vendor_system_id == vendor_id)
        ).all()
    assert snapshots == []


def test_too_many_rows_rejected_without_writing(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    app.state.settings.max_vendor_roster_rows = 1

    response = _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)  # 2 rows > limit of 1
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        snapshots = session.scalars(
            select(VendorUserSnapshot).where(VendorUserSnapshot.vendor_system_id == vendor_id)
        ).all()
    assert snapshots == []


def test_roster_page_shows_delta_since_previous_snapshot(logged_in_client):
    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V2)

    page = logged_in_client.get(f"/vendors/{vendor_id}/roster")
    assert page.status_code == 200
    assert b"Added: 1" in page.content  # carol
    assert b"Removed: 1" in page.content  # bob


def test_snapshot_import_matches_existing_person(logged_in_client, app):
    with app.state.session_factory() as session:
        session.add(Person(email="alice@example.com", display_name="Alice Internal"))
        session.commit()

    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)

    with app.state.session_factory() as session:
        row = session.scalar(
            select(VendorUserSnapshotRow).where(VendorUserSnapshotRow.normalized_email == "alice@example.com")
        )
        assert row.matched_person is not None
        assert row.matched_person.display_name == "Alice Internal"


def test_departed_person_in_roster_is_flagged(logged_in_client, app):
    with app.state.session_factory() as session:
        session.add(Person(email="alice@example.com", employment_status="departed"))
        session.commit()

    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)

    roster_page = logged_in_client.get(f"/vendors/{vendor_id}/roster")
    assert b"alice@example.com" in roster_page.content
    assert b"Departed/suspended" in roster_page.content

    vendor_detail = logged_in_client.get(f"/vendors/{vendor_id}")
    assert b"Former employee appears in latest vendor roster" in vendor_detail.content


def test_admin_can_link_row_to_person_without_changing_imported_values(logged_in_client, admin_client, app):
    with app.state.session_factory() as session:
        person = Person(email="someone-else@example.com", display_name="Someone Else")
        session.add(person)
        session.commit()
        person_id = person.id

    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)

    with app.state.session_factory() as session:
        row = session.scalar(
            select(VendorUserSnapshotRow).where(VendorUserSnapshotRow.normalized_email == "bob@example.com")
        )
        row_id = row.id
        original_email = row.imported_email

    roster_page = admin_client.get(f"/vendors/{vendor_id}/roster")
    csrf_token = extract_csrf_token(roster_page.text)
    response = admin_client.post(
        f"/vendors/{vendor_id}/roster/rows/{row_id}/link",
        data={"person_id": person_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        row = session.get(VendorUserSnapshotRow, row_id)
        assert row.matched_person_id == person_id
        assert row.imported_email == original_email  # imported value untouched

        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "vendor_user_snapshot_row", AuditEvent.action == "link_person"
            )
        ).all()
    assert len(events) == 1


def test_non_admin_cannot_link_row_to_person(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    _import_roster(logged_in_client, vendor_id, ROSTER_CSV_V1)

    with app.state.session_factory() as session:
        row = session.scalar(
            select(VendorUserSnapshotRow).where(VendorUserSnapshotRow.normalized_email == "bob@example.com")
        )
        row_id = row.id

    roster_page = logged_in_client.get(f"/vendors/{vendor_id}/roster")
    csrf_token = extract_csrf_token(roster_page.text)
    response = logged_in_client.post(
        f"/vendors/{vendor_id}/roster/rows/{row_id}/link",
        data={"person_id": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 403
