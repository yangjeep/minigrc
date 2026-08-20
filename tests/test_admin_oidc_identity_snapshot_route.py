"""Browser/E2E tests for issue #20's identity/role population snapshot
capture route (POST /admin/authentication/oidc/identity-snapshot) —
through real auth/CSRF/routes, not the service layer directly (that's
tests/test_oidc_identity_evidence.py)."""

from __future__ import annotations

from sqlalchemy import select

from app.models import EvidenceSnapshot, ExternalIdentity, User
from app.security import hash_password
from tests.conftest import extract_csrf_token


def _link_identity(app, *, email="linked-user@example.com"):
    with app.state.session_factory() as session:
        user = User(email=email, password_hash=hash_password("x"), role="operator")
        session.add(user)
        session.flush()
        session.add(ExternalIdentity(user_id=user.id, issuer="https://idp.example.test", subject="sub-1"))
        session.commit()


def test_capture_route_requires_admin(logged_in_client):
    page = logged_in_client.get("/admin/authentication/oidc")
    assert page.status_code == 403  # oidc admin page itself is admin-gated

    response = logged_in_client.post(
        "/admin/authentication/oidc/identity-snapshot", data={"csrf_token": "whatever"}
    )
    assert response.status_code == 403


def test_capture_route_creates_evidence_snapshot(admin_client, app):
    _link_identity(app)
    page = admin_client.get("/admin/authentication/oidc")
    assert page.status_code == 200
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/authentication/oidc/identity-snapshot",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        snapshot = session.scalar(
            select(EvidenceSnapshot).where(EvidenceSnapshot.source_type == "oidc_identity_population")
        )
        assert snapshot is not None
        assert "linked-user@example.com" in snapshot.normalized_payload_json


def test_captured_snapshot_is_visible_on_evidence_page(admin_client, app):
    _link_identity(app)
    page = admin_client.get("/admin/authentication/oidc")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/authentication/oidc/identity-snapshot",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )

    evidence_page = admin_client.get("/evidence")
    assert evidence_page.status_code == 200
    assert "oidc_identity_population" in evidence_page.text
