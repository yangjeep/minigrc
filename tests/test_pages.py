import pytest

PLACEHOLDER_SLUGS = ["policies", "evidence", "actions", "connectors", "trust-center"]


def test_dashboard_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Dashboard" in response.content


def test_frameworks_list_shows_seeded_framework(client):
    response = client.get("/frameworks")
    assert response.status_code == 200
    assert b"ISO/IEC 27001:2022" in response.content


def test_framework_detail_loads(client):
    frameworks_response = client.get("/frameworks")
    assert frameworks_response.status_code == 200

    from app.models import Framework

    # Use the app's own session to grab a real id rather than parsing HTML.
    session_factory = client.app.state.session_factory
    with session_factory() as session:
        framework = session.query(Framework).first()

    response = client.get(f"/frameworks/{framework.id}")
    assert response.status_code == 200
    assert b"Requirements" in response.content


def test_controls_list_loads(client):
    response = client.get("/controls")
    assert response.status_code == 200
    assert b"Internal Controls" in response.content


def test_risks_list_loads(client):
    response = client.get("/risks")
    assert response.status_code == 200
    assert b"Risk Register" in response.content


def test_audit_log_loads(client):
    response = client.get("/audit-log")
    assert response.status_code == 200
    assert b"Audit Log" in response.content


@pytest.mark.parametrize("slug", PLACEHOLDER_SLUGS)
def test_placeholder_pages_load(client, slug):
    response = client.get(f"/{slug}")
    assert response.status_code == 200
    assert b"Status" in response.content


def test_unknown_route_returns_404(client):
    response = client.get("/does-not-exist")
    assert response.status_code == 404
