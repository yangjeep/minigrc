"""Headless UAT: organization-level framework/catalog version pinning
(issue #43) — the real journey of a system catalog shipping a new
version and an operator upgrading to it, run against the real app over
a real socket. See tests/uat/harness.py and
docs/superpowers/specs/2026-08-20-issue43-catalog-version-pinning-design.md.

Miniature UAT scope note: catalog versions are system-shipped, not
user-authored through any form in this app (there is no "create a new
framework version" UI) — so, exactly like
tests/test_framework_adoption_routes.py's own established pattern, a
new sibling version is simulated with a direct ORM insert
(`Framework(..., catalog_family=...)`) rather than through a route.
Everything downstream of that (viewing the adoptions page, upgrading,
re-viewing history) goes through real HTTP/auth/CSRF.
"""

from __future__ import annotations

import re

import pytest

from app.framework_adoption import get_adoption
from app.models import Framework
from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field

pytestmark = pytest.mark.uat

OPERATOR_EMAIL = "uat-catalog-operator@example.com"
READER_EMAIL = "uat-catalog-reader@example.com"

_OPTION_RE = re.compile(r'<option value="([0-9a-f]{32})">Test Catalog v2</option>')


def _login(client, email):
    login_page = client.get("/login")
    csrf_token = extract_csrf_field(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": UAT_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_catalog_upgrade_journey_preserves_history(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    # 1. Fresh deployment: both system catalogs are already reconciled
    # and auto-adopted (onboarding), and there is nothing newer yet.
    adoptions_page = client.get("/frameworks/adoptions")
    assert adoptions_page.status_code == 200
    assert "soc2" in adoptions_page.text
    assert "iso27001" in adoptions_page.text
    assert "Up to date" in adoptions_page.text

    # 2. A new sibling version of the soc2 catalog family ships (system
    # catalog update — simulated by direct insert, see module docstring).
    with session_factory() as session:
        v2 = Framework(name="Test Catalog", version="v2", catalog_family="soc2")
        session.add(v2)
        session.commit()

    # 3. The operator sees the upgrade offered for soc2 specifically.
    adoptions_page = client.get("/frameworks/adoptions")
    assert adoptions_page.status_code == 200
    match = _OPTION_RE.search(adoptions_page.text)
    assert match is not None, "expected an upgrade candidate option for the new v2 catalog"
    to_framework_id = match.group(1)

    # 4. Perform the upgrade through the real form/route.
    upgrade_response = client.post(
        "/frameworks/adoptions/soc2/upgrade",
        data={"to_framework_id": to_framework_id, "csrf_token": extract_csrf_field(adoptions_page.text)},
        follow_redirects=False,
    )
    assert upgrade_response.status_code == 303

    # 5. soc2's *adopted version* is now "Test Catalog v2"; iso27001 is
    # untouched and still up to date. Note: soc2 still offers an
    # "Upgrade" form back to the prior version — list_upgrade_candidates
    # deliberately returns every other active sibling, not only strictly
    # newer ones, so reverting a mistaken upgrade stays possible. That is
    # intended flexibility, not a defect; this scenario only asserts the
    # thing that matters, which version is currently adopted.
    adoptions_page = client.get("/frameworks/adoptions")
    assert adoptions_page.status_code == 200
    assert "Up to date" in adoptions_page.text  # iso27001, unaffected
    adopted_row = re.search(r"<td>soc2</td>\s*<td><a[^>]*>([^<]+)</a></td>", adoptions_page.text)
    assert adopted_row is not None
    assert adopted_row.group(1) == "Test Catalog v2"

    # 6. Historical fact preserved: the previous soc2 framework version
    # still exists and is still directly reachable — upgrading never
    # deletes/rewrites the version an org was previously on.
    with session_factory() as session:
        adoption = get_adoption(session, "soc2")
        assert adoption.framework_id == to_framework_id
        assert adoption.previous_framework_id is not None
        previous = session.get(Framework, adoption.previous_framework_id)
        assert previous is not None
        assert previous.catalog_family == "soc2"


def test_reader_cannot_upgrade_catalog_adoption(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=READER_EMAIL, role="reader")

    with session_factory() as session:
        v2 = Framework(name="Test Catalog", version="v2", catalog_family="soc2")
        session.add(v2)
        session.commit()
        v2_id = v2.id

    reader_client = _login(uat_client, READER_EMAIL)
    reader_adoptions_page = reader_client.get("/frameworks/adoptions")
    response = reader_client.post(
        "/frameworks/adoptions/soc2/upgrade",
        data={"to_framework_id": v2_id, "csrf_token": extract_csrf_field(reader_adoptions_page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 403

    # Confirm the rejected attempt genuinely didn't mutate the adoption.
    with session_factory() as session:
        adoption = get_adoption(session, "soc2")
        assert adoption.framework_id != v2_id
