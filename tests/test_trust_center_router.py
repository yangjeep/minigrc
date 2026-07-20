"""HTTP-level tests for the Trust Center admin router (Feature 11)."""

from __future__ import annotations

from app.models import Framework, Policy, TrustCenterSection
from tests.conftest import extract_csrf_token


def test_admin_requires_admin_role(logged_in_client):
    response = logged_in_client.get("/trust-center/admin", follow_redirects=False)
    assert response.status_code == 403


def test_admin_allows_admin(admin_client):
    response = admin_client.get("/trust-center/admin")
    assert response.status_code == 200
    assert b"Trust Center" in response.content


def test_update_settings_as_admin(admin_client, app):
    page = admin_client.get("/trust-center/admin")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/trust-center/admin/settings",
        data={
            "enabled": "true",
            "title": "Acme Trust Center",
            "intro_markdown": "We take security seriously.",
            "contact_email": "security@acme.example",
            "support_url": "https://acme.example/support",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with app.state.session_factory() as session:
        from app.trust_center import get_or_create_settings

        settings = get_or_create_settings(session)
        assert settings.enabled is True
        assert settings.title == "Acme Trust Center"
        assert settings.contact_email == "security@acme.example"


def test_update_settings_as_regular_user_forbidden(logged_in_client):
    response = logged_in_client.post(
        "/trust-center/admin/settings",
        data={"title": "x", "csrf_token": "irrelevant"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_create_section_via_register_api_as_admin(admin_client, app):
    page = admin_client.get("/trust-center/admin")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/api/registers/trust-center-sections",
        json={"title": "Security overview", "visibility": "public", "display_order": 1},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Security overview"
    assert body["status"] == "draft"
    assert body["stale"] is False


def test_sections_list_via_register_api_requires_admin(logged_in_client):
    response = logged_in_client.get("/api/registers/trust-center-sections")
    assert response.status_code == 403


def test_sections_list_via_register_api_allows_admin(admin_client):
    response = admin_client.get("/api/registers/trust-center-sections")
    assert response.status_code == 200


def test_create_section_via_register_api_as_regular_user_forbidden(logged_in_client):
    page = logged_in_client.get("/")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/api/registers/trust-center-sections",
        json={"title": "x", "visibility": "public"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 403


def test_section_detail_edit_and_publish_cycle(admin_client, app):
    with app.state.session_factory() as session:
        section = TrustCenterSection(title="Security overview", visibility="public")
        session.add(section)
        session.commit()
        section_id = section.id

    page = admin_client.get(f"/trust-center/admin/sections/{section_id}")
    assert page.status_code == 200
    csrf_token = extract_csrf_token(page.text)

    update = admin_client.post(
        f"/trust-center/admin/sections/{section_id}",
        data={
            "draft_body_markdown": "# We are secure",
            "review_date": "2027-01-01",
            "expiry_date": "",
            "linked_framework_id": "",
            "linked_policy_id": "",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert update.status_code == 303

    preview = admin_client.get(f"/trust-center/admin/sections/{section_id}/preview")
    assert preview.status_code == 200
    # heading_offset=1: preview page has its own top-level <h1>, so an
    # admin-authored "# heading" is shifted to <h2> to avoid a competing
    # top-level heading in the screen-reader outline.
    assert b"<h2>We are secure</h2>" in preview.content

    publish = admin_client.post(
        f"/trust-center/admin/sections/{section_id}/publish",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert publish.status_code == 303

    with app.state.session_factory() as session:
        refreshed = session.get(TrustCenterSection, section_id)
        assert refreshed.status == "published"
        assert refreshed.published_body_markdown == "# We are secure"

    unpublish = admin_client.post(
        f"/trust-center/admin/sections/{section_id}/unpublish",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert unpublish.status_code == 303

    with app.state.session_factory() as session:
        refreshed = session.get(TrustCenterSection, section_id)
        assert refreshed.status == "draft"
        assert refreshed.published_body_markdown == "# We are secure"


def test_section_detail_shows_stale_banner_when_expired(admin_client, app):
    with app.state.session_factory() as session:
        section = TrustCenterSection(
            title="Old policy summary",
            visibility="public",
            status="published",
            draft_body_markdown="content",
            published_body_markdown="content",
        )
        session.add(section)
        session.commit()
        section.expiry_date = __import__("datetime").date(2000, 1, 1)
        session.commit()
        section_id = section.id

    response = admin_client.get(f"/trust-center/admin/sections/{section_id}")
    assert response.status_code == 200
    assert b"stale" in response.content.lower()


def test_section_can_link_to_framework_and_policy(admin_client, app):
    with app.state.session_factory() as session:
        framework = Framework(name="ISO 27001", version="2022")
        policy = Policy(title="Acceptable Use Policy", status="approved")
        section = TrustCenterSection(title="Governance", visibility="public")
        session.add_all([framework, policy, section])
        session.commit()
        framework_id, policy_id, section_id = framework.id, policy.id, section.id

    page = admin_client.get(f"/trust-center/admin/sections/{section_id}")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        f"/trust-center/admin/sections/{section_id}",
        data={
            "draft_body_markdown": "content",
            "review_date": "",
            "expiry_date": "",
            "linked_framework_id": framework_id,
            "linked_policy_id": policy_id,
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with app.state.session_factory() as session:
        refreshed = session.get(TrustCenterSection, section_id)
        assert refreshed.linked_framework_id == framework_id
        assert refreshed.linked_policy_id == policy_id
