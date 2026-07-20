"""HTTP-level tests for the public Trust Center route (Feature 12).

Unauthenticated throughout — uses the plain `client` fixture, never
`logged_in_client`/`admin_client`, since the whole point is that no
login is required (or possible) to reach this surface.
"""

from __future__ import annotations

from app.models import TrustCenterSection
from app.trust_center import get_or_create_settings
from tests.conftest import extract_csrf_token
from tests.test_policies import VALID_PDF, _create_policy_with_pdf


def _enable_trust_center(app, **overrides):
    with app.state.session_factory() as session:
        settings = get_or_create_settings(session)
        settings.enabled = True
        for key, value in overrides.items():
            setattr(settings, key, value)
        session.commit()


def _make_section(app, **kwargs):
    with app.state.session_factory() as session:
        section = TrustCenterSection(**kwargs)
        session.add(section)
        session.commit()
        return section.id


def test_disabled_trust_center_returns_404(client, app):
    response = client.get("/trust-center")
    assert response.status_code == 404


def test_enabled_trust_center_shows_title_and_intro(client, app):
    _enable_trust_center(app, title="Acme Trust Center", intro_markdown="We take **security** seriously.")
    response = client.get("/trust-center")
    assert response.status_code == 200
    assert b"Acme Trust Center" in response.content
    assert b"<strong>security</strong>" in response.content


def test_only_public_published_sections_appear(client, app):
    _enable_trust_center(app)
    _make_section(
        app,
        title="Visible section",
        visibility="public",
        status="published",
        draft_body_markdown="visible draft",
        published_body_markdown="visible published content",
    )
    _make_section(
        app,
        title="Draft section",
        visibility="public",
        status="draft",
        draft_body_markdown="draft only content",
    )
    _make_section(
        app,
        title="Internal section",
        visibility="internal",
        status="published",
        draft_body_markdown="internal draft",
        published_body_markdown="internal published content",
    )

    response = client.get("/trust-center")
    assert response.status_code == 200
    assert b"visible published content" in response.content
    assert b"draft only content" not in response.content
    assert b"internal published content" not in response.content
    assert b"Draft section" not in response.content
    assert b"Internal section" not in response.content


def test_shows_published_snapshot_not_newer_draft(client, app):
    _enable_trust_center(app)
    section_id = _make_section(
        app,
        title="Security overview",
        visibility="public",
        status="published",
        draft_body_markdown="newer unpublished draft content",
        published_body_markdown="original published content",
    )
    response = client.get("/trust-center")
    assert b"original published content" in response.content
    assert b"newer unpublished draft content" not in response.content
    assert section_id  # keep id referenced for clarity


def test_section_body_headings_are_shifted_below_section_title(client, app):
    _enable_trust_center(app)
    _make_section(
        app,
        title="Security overview",
        visibility="public",
        status="published",
        published_body_markdown="# Encryption\n\nDetails here.",
    )
    response = client.get("/trust-center")
    # heading_offset=2: body sits under this section's own <h2> title,
    # which itself sits under the page's <h1> — an author-written "#"
    # must not produce a second top-level heading.
    assert b"<h3>Encryption</h3>" in response.content
    assert b"<h1>Encryption</h1>" not in response.content


def test_public_page_has_no_store_cache_header(client, app):
    _enable_trust_center(app)
    response = client.get("/trust-center")
    assert response.headers["cache-control"] == "no-store"


def test_public_page_has_meta_description_and_og_tags(client, app):
    _enable_trust_center(app, title="Acme Trust Center", intro_markdown="Our security posture.")
    response = client.get("/trust-center")
    html = response.text
    assert '<meta name="description"' in html
    assert 'property="og:title"' in html
    assert 'content="Acme Trust Center"' in html


def test_public_page_does_not_render_authenticated_shell(client, app):
    _enable_trust_center(app)
    response = client.get("/trust-center")
    assert b"sidebarOffcanvas" not in response.content
    assert b'aria-label="Primary"' not in response.content


def test_policy_download_succeeds_when_approved_and_linked_from_public_published_section(admin_client, app):
    policy_id = _create_policy_with_pdf(admin_client, title="Acceptable Use Policy")
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}",
        data={"title": "Acceptable Use Policy", "status": "approved", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    _enable_trust_center(app)
    _make_section(
        app,
        title="Policies",
        visibility="public",
        status="published",
        published_body_markdown="See our policies below.",
        linked_policy_id=policy_id,
    )

    from fastapi.testclient import TestClient

    anonymous = TestClient(app)
    response = anonymous.get(f"/trust-center/policies/{policy_id}/download")
    assert response.status_code == 200
    assert response.content == VALID_PDF
    assert response.headers["x-content-type-options"] == "nosniff"


def test_policy_download_404_when_not_approved(admin_client, app):
    policy_id = _create_policy_with_pdf(admin_client, title="Draft Policy")
    _enable_trust_center(app)
    _make_section(
        app,
        title="Policies",
        visibility="public",
        status="published",
        published_body_markdown="See our policies below.",
        linked_policy_id=policy_id,
    )

    from fastapi.testclient import TestClient

    anonymous = TestClient(app)
    response = anonymous.get(f"/trust-center/policies/{policy_id}/download")
    assert response.status_code == 404


def test_policy_download_404_when_not_linked_from_any_public_published_section(admin_client, app):
    policy_id = _create_policy_with_pdf(admin_client, title="Unlinked Policy")
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}",
        data={"title": "Unlinked Policy", "status": "approved", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    _enable_trust_center(app)

    from fastapi.testclient import TestClient

    anonymous = TestClient(app)
    response = anonymous.get(f"/trust-center/policies/{policy_id}/download")
    assert response.status_code == 404


def test_policy_download_404_when_section_not_public_or_not_published(admin_client, app):
    policy_id = _create_policy_with_pdf(admin_client, title="Internal Only Policy")
    page = admin_client.get(f"/policies/{policy_id}")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        f"/policies/{policy_id}",
        data={"title": "Internal Only Policy", "status": "approved", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    _enable_trust_center(app)
    _make_section(
        app,
        title="Internal policies",
        visibility="internal",
        status="published",
        published_body_markdown="internal only",
        linked_policy_id=policy_id,
    )

    from fastapi.testclient import TestClient

    anonymous = TestClient(app)
    response = anonymous.get(f"/trust-center/policies/{policy_id}/download")
    assert response.status_code == 404
