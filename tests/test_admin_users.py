from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, User
from app.security import hash_password
from tests.conftest import ADMIN_EMAIL, TEST_EMAIL, extract_csrf_token


def test_users_list_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/users")
    assert response.status_code == 403


def test_users_list_shows_existing_users(admin_client, test_user):
    response = admin_client.get("/admin/users")
    assert response.status_code == 200
    assert TEST_EMAIL in response.text


def test_new_user_form_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/users/new")
    assert response.status_code == 403


def test_create_user_via_admin(admin_client, app):
    new_page = admin_client.get("/admin/users/new")
    csrf_token = extract_csrf_token(new_page.text)

    response = admin_client.post(
        "/admin/users",
        data={"email": "Invited@Example.com", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "invited@example.com"))
        assert user is not None
        assert user.role == "user"
        assert user.status == "active"
        assert user.password_hash == ""

        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "user", AuditEvent.action == "create_via_admin"
            )
        ).all()
        assert len(events) == 1


def test_create_user_duplicate_email_rejected(admin_client, app, admin_user):
    new_page = admin_client.get("/admin/users/new")
    csrf_token = extract_csrf_token(new_page.text)

    response = admin_client.post(
        "/admin/users",
        data={"email": ADMIN_EMAIL, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        count = len(session.scalars(select(User).where(User.email == ADMIN_EMAIL)).all())
        assert count == 1


def test_edit_user_form_requires_admin(logged_in_client, test_user):
    response = logged_in_client.get(f"/admin/users/{test_user.id}/edit")
    assert response.status_code == 403


def test_admin_can_promote_user_to_admin(admin_client, app, test_user):
    edit_page = admin_client.get(f"/admin/users/{test_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{test_user.id}/edit",
        data={"role": "admin", "status": "active", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        assert user.role == "admin"
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "user", AuditEvent.action == "update_role_status"
            )
        ).all()
        assert len(events) == 1


def test_admin_can_disable_another_user(admin_client, app, test_user):
    edit_page = admin_client.get(f"/admin/users/{test_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{test_user.id}/edit",
        data={"role": "user", "status": "disabled", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        assert user.status == "disabled"


def test_admin_can_approve_pending_user(admin_client, app, test_user):
    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        user.status = "pending"
        session.commit()

    edit_page = admin_client.get(f"/admin/users/{test_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{test_user.id}/edit",
        data={"role": "user", "status": "active", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        assert user.status == "active"


def test_admin_cannot_disable_own_account(admin_client, app, admin_user):
    edit_page = admin_client.get(f"/admin/users/{admin_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{admin_user.id}/edit",
        data={"role": "admin", "status": "disabled", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        user = session.get(User, admin_user.id)
        assert user.status == "active"


def test_cannot_demote_last_active_admin(admin_client, app, admin_user):
    edit_page = admin_client.get(f"/admin/users/{admin_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{admin_user.id}/edit",
        data={"role": "user", "status": "active", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        user = session.get(User, admin_user.id)
        assert user.role == "admin"


def test_can_demote_admin_when_another_active_admin_exists(admin_client, app, admin_user):
    with app.state.session_factory() as session:
        session.add(User(email="second-admin@example.com", password_hash=hash_password("x"), role="admin"))
        session.commit()

    edit_page = admin_client.get(f"/admin/users/{admin_user.id}/edit")
    csrf_token = extract_csrf_token(edit_page.text)

    response = admin_client.post(
        f"/admin/users/{admin_user.id}/edit",
        data={"role": "user", "status": "active", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        user = session.get(User, admin_user.id)
        assert user.role == "user"
