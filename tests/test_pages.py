from pathlib import Path

import pytest

PLACEHOLDER_SLUGS = ["actions", "connectors"]

VENDOR_ASSETS = [
    "app/static/vendor/bootstrap-5.3.3/bootstrap.min.css",
    "app/static/vendor/bootstrap-5.3.3/bootstrap.bundle.min.js",
    "app/static/vendor/bootstrap-icons-1.11.3/bootstrap-icons.css",
    "app/static/vendor/bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2",
]


def test_unauthenticated_dashboard_redirects_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_dashboard_loads(logged_in_client):
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert b"Dashboard" in response.content


def test_frameworks_list_shows_seeded_framework(logged_in_client):
    response = logged_in_client.get("/frameworks")
    assert response.status_code == 200
    assert b"ISO/IEC 27001:2022" in response.content


def test_framework_detail_loads(logged_in_client):
    frameworks_response = logged_in_client.get("/frameworks")
    assert frameworks_response.status_code == 200

    from app.models import Framework

    session_factory = logged_in_client.app.state.session_factory
    with session_factory() as session:
        framework = session.query(Framework).first()

    response = logged_in_client.get(f"/frameworks/{framework.id}")
    assert response.status_code == 200
    assert b"Requirements" in response.content


def test_controls_list_loads(logged_in_client):
    response = logged_in_client.get("/controls")
    assert response.status_code == 200
    assert b"Internal Controls" in response.content


def test_policies_list_loads(logged_in_client):
    response = logged_in_client.get("/policies")
    assert response.status_code == 200
    assert b"Policies" in response.content


def test_risks_list_loads(logged_in_client):
    response = logged_in_client.get("/risks")
    assert response.status_code == 200
    assert b"Risk Register" in response.content


def test_audit_log_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/audit-log")
    assert response.status_code == 403


def test_audit_log_loads_for_admin(admin_client):
    response = admin_client.get("/admin/audit-log")
    assert response.status_code == 200
    assert b"Audit Log" in response.content


def test_legacy_audit_log_path_redirects(admin_client):
    response = admin_client.get("/audit-log", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/admin/audit-log"


def test_people_list_loads(logged_in_client):
    response = logged_in_client.get("/people")
    assert response.status_code == 200
    assert b"People" in response.content


def test_vendors_list_loads(logged_in_client):
    response = logged_in_client.get("/vendors")
    assert response.status_code == 200
    assert b"Vendors" in response.content


def test_vendors_renewals_loads(logged_in_client):
    response = logged_in_client.get("/vendors/renewals")
    assert response.status_code == 200
    assert b"Upcoming renewals" in response.content


@pytest.mark.parametrize("slug", PLACEHOLDER_SLUGS)
def test_placeholder_pages_load(logged_in_client, slug):
    response = logged_in_client.get(f"/{slug}")
    assert response.status_code == 200
    assert b"Status" in response.content


def test_unknown_route_returns_404(logged_in_client):
    response = logged_in_client.get("/does-not-exist")
    assert response.status_code == 404


def test_health_does_not_require_auth(client):
    response = client.get("/health")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/", "/frameworks", "/risks", "/policies"],
)
def test_pages_render_bootstrap_shell(logged_in_client, path):
    response = logged_in_client.get(path)
    assert response.status_code == 200
    html = response.content
    assert b'id="sidebarOffcanvas"' in html
    assert b'aria-label="Primary"' in html
    assert b"visually-hidden-focusable" in html  # skip-to-content link
    assert b"/static/vendor/bootstrap-5.3.3/bootstrap.min.css" in html
    assert b"/static/vendor/bootstrap-icons-1.11.3/bootstrap-icons.css" in html
    assert b"/static/vendor/bootstrap-5.3.3/bootstrap.bundle.min.js" in html


@pytest.mark.parametrize(
    "path",
    ["/trust-center/admin", "/admin", "/admin/users", "/admin/audit-log"],
)
def test_admin_pages_render_bootstrap_shell(admin_client, path):
    response = admin_client.get(path)
    assert response.status_code == 200
    html = response.content
    assert b'id="sidebarOffcanvas"' in html
    assert b'aria-label="Primary"' in html
    assert b"/static/vendor/bootstrap-5.3.3/bootstrap.min.css" in html


@pytest.mark.parametrize("asset_path", VENDOR_ASSETS)
def test_vendored_bootstrap_assets_exist(asset_path):
    assert Path(asset_path).is_file(), f"missing vendored asset: {asset_path}"
