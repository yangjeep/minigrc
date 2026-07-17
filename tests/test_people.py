from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, Person
from tests.conftest import extract_csrf_token


def _create_person(client, *, email="alice@example.com", status="active") -> str:
    page = client.get("/people/new")
    csrf_token = extract_csrf_token(page.text)
    response = client.post(
        "/people",
        data={
            "email": email,
            "display_name": "Alice Example",
            "employment_status": status,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def test_create_person(logged_in_client):
    person_id = _create_person(logged_in_client)
    detail = logged_in_client.get(f"/people/{person_id}")
    assert detail.status_code == 200
    assert b"Alice Example" in detail.content
    assert b"alice@example.com" in detail.content


def test_email_is_normalized_and_unique(logged_in_client, app):
    _create_person(logged_in_client, email="Bob@Example.COM")

    page = logged_in_client.get("/people/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/people",
        data={
            "email": "bob@example.com",
            "display_name": "Duplicate Bob",
            "employment_status": "active",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        people = session.scalars(select(Person).where(Person.email == "bob@example.com")).all()
    assert len(people) == 1


def test_manual_edit_is_audited(logged_in_client, app):
    person_id = _create_person(logged_in_client, email="carol@example.com", status="active")

    page = logged_in_client.get(f"/people/{person_id}")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/people/{person_id}/edit",
        data={
            "display_name": "Carol Updated",
            "employment_status": "departed",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        person = session.get(Person, person_id)
        assert person.display_name == "Carol Updated"
        assert person.employment_status == "departed"

        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "person", AuditEvent.entity_id == person_id)
        ).all()
    assert any(e.action == "create" for e in events)
    assert any(e.action == "update" for e in events)


def test_search_filters_by_email_or_name(logged_in_client):
    _create_person(logged_in_client, email="dave@example.com")
    page = logged_in_client.get("/people?q=dave")
    assert b"dave@example.com" in page.content
    page = logged_in_client.get("/people?q=nonexistent")
    assert b"dave@example.com" not in page.content


def test_filter_by_employment_status(logged_in_client):
    _create_person(logged_in_client, email="active@example.com", status="active")
    _create_person(logged_in_client, email="departed@example.com", status="departed")

    page = logged_in_client.get("/people?status=departed")
    assert b"departed@example.com" in page.content
    assert b"active@example.com" not in page.content
