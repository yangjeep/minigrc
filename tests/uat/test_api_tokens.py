"""Headless UAT: issue #36's admin API-token management UI — an admin
issues a read-only MCP token, sees the plaintext exactly once, confirms
it never reappears, then revokes it; a non-admin is denied the whole
surface. Run against the real app over a real socket (tests/uat/harness.py).

The MCP protocol/tool-calling surface itself is not a browser flow, so
its own real-client/HTTP-auth coverage lives in
tests/test_mcp_server.py and tests/test_mcp_server_http_auth.py instead
— this scenario covers exactly the user-visible admin UI.
"""

from __future__ import annotations

import re

import pytest

from tests.uat.harness import UAT_PASSWORD, create_uat_user, extract_csrf_field

_REVOKE_ACTION_RE = re.compile(r'action="(/admin/api-tokens/[^"]+/revoke)"')

pytestmark = pytest.mark.uat

ADMIN_EMAIL = "uat-api-tokens-admin@example.com"
OPERATOR_EMAIL = "uat-api-tokens-operator@example.com"


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


def test_admin_issues_views_once_and_revokes_a_token(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=ADMIN_EMAIL, role="admin")
    client = _login(uat_client, ADMIN_EMAIL)

    new_form = client.get("/admin/api-tokens/new")
    assert new_form.status_code == 200
    created = client.post(
        "/admin/api-tokens",
        data={"name": "UAT MCP client", "csrf_token": extract_csrf_field(new_form.text)},
    )
    assert created.status_code == 200
    assert "mgrc_" in created.text

    list_page = client.get("/admin/api-tokens")
    assert list_page.status_code == 200
    assert "UAT MCP client" in list_page.text
    assert "mgrc_" not in list_page.text  # never shown a second time

    csrf_token = extract_csrf_field(list_page.text)
    revoke_match = _REVOKE_ACTION_RE.search(list_page.text)
    assert revoke_match is not None, "no revoke form action found on the token list page"
    revoke = client.post(
        revoke_match.group(1),
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert revoke.status_code == 303

    after_revoke = client.get("/admin/api-tokens")
    assert "revoked" in after_revoke.text.lower()


def test_non_admin_cannot_reach_api_token_management(uat_server, uat_client):
    _base_url, session_factory = uat_server
    create_uat_user(session_factory, email=OPERATOR_EMAIL, role="operator")
    client = _login(uat_client, OPERATOR_EMAIL)

    assert client.get("/admin/api-tokens").status_code == 403
    assert client.get("/admin/api-tokens/new").status_code == 403
