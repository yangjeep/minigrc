from __future__ import annotations

import datetime

from sqlalchemy import select

from app.models import User, UserSession
from app.security import hash_password, hash_session_token, verify_password
from tests.conftest import TEST_EMAIL, TEST_PASSWORD, extract_csrf_token


def test_protected_route_redirects_unauthenticated(client):
    response = client.get("/frameworks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_valid_login_succeeds(client, test_user):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)

    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/")
    assert "flash_kind=success" in response.headers["location"]
    assert "session" in response.cookies


def test_invalid_password_fails_generically(client, test_user):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)

    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": "wrong-password", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "Invalid+email+or+password" in response.headers["location"]


def test_unknown_email_fails_with_same_generic_message(client):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)

    response = client.post(
        "/login",
        data={"email": "nobody@example.com", "password": "whatever123", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Invalid+email+or+password" in response.headers["location"]


def test_password_hash_is_not_plaintext(test_user):
    assert test_user.password_hash != TEST_PASSWORD
    assert verify_password(TEST_PASSWORD, test_user.password_hash) is True
    assert verify_password("wrong", test_user.password_hash) is False


def test_logout_invalidates_session(logged_in_client):
    page = logged_in_client.get("/")
    csrf_token = extract_csrf_token(page.text)

    response = logged_in_client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

    protected = logged_in_client.get("/frameworks", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"


def test_authenticated_pages_are_not_cached(logged_in_client):
    """Regression: the back button after logout must not replay a cached
    authenticated page from the browser's HTTP cache — see UAT finding,
    2026-07-20 admin/OAuth/IAM/connections consolidation worklog."""
    response = logged_in_client.get("/")
    assert response.headers["cache-control"] == "no-store"


def test_login_page_is_not_cached(client):
    response = client.get("/login")
    assert response.headers["cache-control"] == "no-store"


def test_expired_session_is_rejected(app, client, test_user):
    raw_token = "expired-raw-session-token"
    session_factory = app.state.session_factory
    with session_factory() as session:
        expired = UserSession(
            user_id=test_user.id,
            token_hash=hash_session_token(raw_token),
            expires_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1),
        )
        session.add(expired)
        session.commit()

    client.cookies.set("session", raw_token)
    response = client.get("/frameworks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_csrf_rejection_on_missing_token(logged_in_client):
    response = logged_in_client.post("/risks", data={"title": "No CSRF"})
    assert response.status_code == 422


def test_csrf_rejection_on_wrong_token(logged_in_client):
    response = logged_in_client.post("/risks", data={"title": "Bad CSRF", "csrf_token": "wrong"})
    assert response.status_code == 400


def test_unauthenticated_policy_download_denied(app, client):
    from app.models import Policy, PolicyVersion

    session_factory = app.state.session_factory
    with session_factory() as session:
        policy = Policy(title="Security Policy")
        session.add(policy)
        session.flush()
        version = PolicyVersion(
            policy_id=policy.id,
            version_number=1,
            original_filename="policy.pdf",
            stored_filename="document.pdf",
            media_type="application/pdf",
            byte_size=10,
            sha256="0" * 64,
            uploader="admin@example.com",
        )
        session.add(version)
        session.commit()
        policy_id, version_id = policy.id, version.id

    response = client.get(f"/policies/{policy_id}/versions/{version_id}/download", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_normalized_email_is_unique(app):
    session_factory = app.state.session_factory
    with session_factory() as session:
        session.add(User(email="Person@Example.com".lower(), password_hash=hash_password("x")))
        session.commit()

        exists = session.scalar(select(User).where(User.email == "person@example.com"))
        assert exists is not None


def test_disabled_user_login_rejected(client, app, test_user):
    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        user.status = "disabled"
        session.commit()

    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "session" not in response.cookies


def test_pending_user_login_rejected(client, app, test_user):
    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        user.status = "pending"
        session.commit()

    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "session" not in response.cookies
    assert "awaiting+administrator+approval" in response.headers["location"]


def test_disabling_user_mid_session_revokes_access_immediately(logged_in_client, app, test_user):
    still_ok = logged_in_client.get("/frameworks", follow_redirects=False)
    assert still_ok.status_code == 200

    with app.state.session_factory() as session:
        user = session.get(User, test_user.id)
        user.status = "disabled"
        session.commit()

    response = logged_in_client.get("/frameworks", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
