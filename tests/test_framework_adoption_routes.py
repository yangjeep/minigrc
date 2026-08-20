"""Browser/E2E tests for issue #43's catalog adoption/upgrade routes
(app/routers/frameworks.py's /frameworks/adoptions endpoints) — through
real auth/CSRF/routes/forms, not the service layer directly (that's
tests/test_framework_adoption.py)."""

from __future__ import annotations

from app.framework_adoption import adopt_framework, get_adoption
from app.models import Framework
from tests.conftest import extract_csrf_token


def test_adoptions_page_shows_reconciled_system_catalogs(logged_in_client):
    response = logged_in_client.get("/frameworks/adoptions")
    assert response.status_code == 200
    assert "iso27001" in response.text
    assert "soc2" in response.text
    assert "Up to date" in response.text  # no synthetic second version exists yet


def test_adoptions_page_offers_upgrade_when_a_newer_version_exists(logged_in_client, app):
    with app.state.session_factory() as session:
        v2 = Framework(name="Test Catalog", version="v2", catalog_family="soc2")
        session.add(v2)
        session.commit()

    response = logged_in_client.get("/frameworks/adoptions")
    assert response.status_code == 200
    assert "Test Catalog v2" in response.text
    assert '<button type="submit" class="btn btn-sm btn-primary">Upgrade</button>' in response.text


def test_upgrade_route_moves_adoption_and_preserves_history(admin_client, app):
    with app.state.session_factory() as session:
        v2 = Framework(name="Test Catalog", version="v2", catalog_family="soc2")
        session.add(v2)
        session.commit()
        v2_id = v2.id
        original_adoption = get_adoption(session, "soc2")
        original_framework_id = original_adoption.framework_id

    page = admin_client.get("/frameworks/adoptions")
    csrf_token = extract_csrf_token(page.text)
    response = admin_client.post(
        "/frameworks/adoptions/soc2/upgrade",
        data={"to_framework_id": v2_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        adoption = get_adoption(session, "soc2")
        assert adoption.framework_id == v2_id
        assert adoption.previous_framework_id == original_framework_id

        # The original framework row is untouched — still exists, still active.
        original = session.get(Framework, original_framework_id)
        assert original is not None
        assert original.is_active is True


def test_upgrade_route_requires_write_access(reader_client, app):
    with app.state.session_factory() as session:
        v2 = Framework(name="Test Catalog", version="v2", catalog_family="soc2")
        session.add(v2)
        session.commit()
        v2_id = v2.id

    page = reader_client.get("/frameworks/adoptions")
    csrf_token = extract_csrf_token(page.text)
    response = reader_client.post(
        "/frameworks/adoptions/soc2/upgrade",
        data={"to_framework_id": v2_id, "csrf_token": csrf_token},
    )
    assert response.status_code == 403


def test_upgrade_route_rejects_unknown_catalog_family(logged_in_client):
    page = logged_in_client.get("/frameworks/adoptions")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/frameworks/adoptions/does-not-exist/upgrade",
        data={"to_framework_id": "whatever", "csrf_token": csrf_token},
    )
    assert response.status_code == 404


def test_upgrade_route_rejects_cross_family_target(logged_in_client, app):
    with app.state.session_factory() as session:
        other_family = Framework(name="ISO clone", version="v1", catalog_family="iso27001")
        session.add(other_family)
        session.commit()
        other_family_id = other_family.id

    page = logged_in_client.get("/frameworks/adoptions")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/frameworks/adoptions/soc2/upgrade",
        data={"to_framework_id": other_family_id, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_adopt_framework_requires_catalog_family_service_layer_still_enforced(app):
    """Sanity check that the router doesn't bypass the service-layer
    validation adopt_framework already enforces."""
    with app.state.session_factory() as session:
        custom = Framework(name="Custom", version="1.0")
        session.add(custom)
        session.flush()
        try:
            adopt_framework(session, custom)
            raised = False
        except ValueError:
            raised = True
        assert raised
