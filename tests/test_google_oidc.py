from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select

from app.google_oidc import GoogleIdentity, GoogleOidcError
from app.models import AuditEvent, Person, User

GOOGLE_LOGIN_SETTINGS = {
    "google_oidc_client_id": "test-client-id",
    "google_oidc_client_secret": "test-client-secret",
    "public_base_url": "https://grc.example.com",
}


def _enable_google_oidc(app, allowed_domains: str = "") -> None:
    for key, value in GOOGLE_LOGIN_SETTINGS.items():
        setattr(app.state.settings, key, value)
    app.state.settings.google_oidc_allowed_domains = allowed_domains


def _start_login(client) -> tuple[str, str]:
    response = client.get("/auth/google/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/")
    state = client.cookies.get("google_oidc_state")
    nonce = client.cookies.get("google_oidc_nonce")
    assert state and nonce
    return state, nonce


def test_login_disabled_returns_404_when_not_configured(client):
    response = client.get("/auth/google/login")
    assert response.status_code == 404


def test_login_redirects_to_google_with_state_and_nonce(app, client):
    _enable_google_oidc(app)
    _start_login(client)


def test_callback_rejects_state_mismatch(app, client):
    _enable_google_oidc(app)
    _start_login(client)
    response = client.get(
        "/auth/google/callback", params={"code": "abc", "state": "wrong-state"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "state" in response.headers["location"].lower()


def test_callback_rejects_provider_error(app, client):
    _enable_google_oidc(app)
    state, _nonce = _start_login(client)
    response = client.get(
        "/auth/google/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_rejects_wrong_audience(mock_exchange, app, client):
    _enable_google_oidc(app)
    state, nonce = _start_login(client)

    with patch(
        "app.google_oidc.google_id_token.verify_oauth2_token", side_effect=ValueError("wrong audience")
    ):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_rejects_nonce_mismatch(mock_exchange, app, client):
    _enable_google_oidc(app)
    state, nonce = _start_login(client)

    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "alice@example.com",
        "email_verified": True,
        "nonce": "different-nonce-than-cookie",
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]
    assert "nonce" in response.headers["location"].lower()


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_rejects_unverified_email(mock_exchange, app, client):
    _enable_google_oidc(app)
    state, nonce = _start_login(client)

    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "alice@example.com",
        "email_verified": False,
        "nonce": nonce,
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_rejects_disallowed_domain(mock_exchange, app, client):
    _enable_google_oidc(app, allowed_domains="allowed.example.com")
    state, nonce = _start_login(client)

    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "alice@not-allowed.example.com",
        "email_verified": True,
        "hd": "not-allowed.example.com",
        "nonce": nonce,
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_creates_new_user_and_logs_in(mock_exchange, app, client):
    _enable_google_oidc(app, allowed_domains="allowed.example.com")
    state, nonce = _start_login(client)

    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "NewPerson@Allowed.example.com",
        "email_verified": True,
        "hd": "allowed.example.com",
        "nonce": nonce,
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303
    assert "session" in response.cookies

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "newperson@allowed.example.com"))
        assert user is not None
        assert user.role == "admin"  # first user in a fresh test DB
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "user", AuditEvent.entity_id == user.id)
        ).all()
    assert any(e.action == "create_via_google_oidc" for e in events)
    assert any(e.action == "login_google_oidc" for e in events)


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_links_existing_local_user_by_normalized_email(mock_exchange, app, client, test_user):
    _enable_google_oidc(app)
    state, nonce = _start_login(client)

    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "Admin@Example.com",  # matches TEST_EMAIL="admin@example.com" case-insensitively
        "email_verified": True,
        "nonce": nonce,
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        response = client.get(
            "/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False
        )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        users = session.scalars(select(User).where(User.email == "admin@example.com")).all()
    assert len(users) == 1  # linked to the existing local user, not duplicated


@patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token")
def test_callback_links_to_existing_person_by_email(mock_exchange, app, client):
    _enable_google_oidc(app)
    with app.state.session_factory() as session:
        session.add(Person(email="carol@example.com", display_name="Carol"))
        session.commit()

    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "carol@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    with patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims):
        client.get("/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False)

    with app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "carol@example.com"))
        assert user.person is not None
        assert user.person.display_name == "Carol"


def test_logout_after_google_login_revokes_session(app, client):
    _enable_google_oidc(app)
    state, nonce = _start_login(client)
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "1234567890",
        "email": "logout-test@example.com",
        "email_verified": True,
        "nonce": nonce,
    }
    with (
        patch("app.routers.google_oidc.exchange_code_for_id_token", return_value="fake-id-token"),
        patch("app.google_oidc.google_id_token.verify_oauth2_token", return_value=claims),
    ):
        client.get("/auth/google/callback", params={"code": "abc", "state": state}, follow_redirects=False)

    protected = client.get("/frameworks", follow_redirects=False)
    assert protected.status_code == 200


def test_google_identity_dataclass_fields():
    identity = GoogleIdentity(subject="1", email="a@b.com", email_verified=True, hosted_domain="b.com")
    assert identity.subject == "1"
    assert identity.hosted_domain == "b.com"


def test_google_oidc_error_is_value_error():
    assert isinstance(GoogleOidcError("x"), ValueError)
