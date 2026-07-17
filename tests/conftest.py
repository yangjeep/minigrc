from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import User
from app.security import hash_password

TEST_PASSWORD = "correct horse battery staple"  # noqa: S105 (test fixture, not a real secret)
TEST_EMAIL = "admin@example.com"

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    return create_app(database_path=db_path)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def test_user(app):
    with app.state.session_factory() as session:
        user = User(email=TEST_EMAIL, password_hash=hash_password(TEST_PASSWORD))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def extract_csrf_token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match is not None, "no csrf_token field found in response HTML"
    return match.group(1)


@pytest.fixture
def logged_in_client(client, test_user):
    login_page = client.get("/login")
    csrf_token = extract_csrf_token(login_page.text)
    response = client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": csrf_token},
    )
    assert response.status_code in (200, 303)
    return client
