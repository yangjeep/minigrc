from __future__ import annotations

from cryptography.fernet import Fernet

from app.models import GoogleOidcSettings
from app.secrets import create_encrypted_secret
from tests.conftest import TEST_EMAIL, TEST_PASSWORD, extract_csrf_token


def test_local_login_works_when_google_oidc_never_configured(client, test_user):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "session" in response.cookies


def test_local_login_works_when_google_oidc_config_is_broken(app, client, test_user):
    key = Fernet.generate_key().decode()
    app.state.settings.public_base_url = "https://grc.example.com"
    app.state.settings.encryption_key = key
    with app.state.session_factory() as session:
        secret = create_encrypted_secret(
            session, name="google_oidc_client_secret", plaintext="whatever", actor="admin", key=key
        )
        session.add(
            GoogleOidcSettings(
                enabled=True,
                client_id="broken-client-id",
                secret_id=secret.id,
                updated_by="admin@example.com",
            )
        )
        session.commit()

    # Rotate the encryption key without re-saving the secret — simulates a
    # broken/misconfigured Google OAuth setup.
    app.state.settings.encryption_key = Fernet.generate_key().decode()

    assert client.get("/auth/google/login").status_code == 404

    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "session" in response.cookies


def test_local_login_works_when_google_oidc_explicitly_disabled(app, client, test_user):
    with app.state.session_factory() as session:
        session.add(GoogleOidcSettings(enabled=False, updated_by="admin@example.com"))
        session.commit()

    assert client.get("/auth/google/login").status_code == 404

    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "session" in response.cookies
