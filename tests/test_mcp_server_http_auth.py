"""HTTP-transport auth enforcement for issue #36's MCP server.

tests/test_mcp_server.py drives app/mcp_server.py through a real MCP
client, but over IN-MEMORY streams that talk directly to the FastMCP
server object — that path never goes through
BearerAuthBackend/RequireAuthMiddleware at all. This suite instead mounts
the real `streamable_http_app()` Starlette app and proves the actual
HTTP-transport bearer-token gate: missing/garbage/revoked tokens are
rejected by the transport layer itself, before any MCP protocol code
runs, and a valid token is let through to the protocol layer.
"""

from __future__ import annotations

import datetime

from starlette.testclient import TestClient

from app.api_tokens import create_api_token, revoke_api_token
from app.mcp_server import build_fastmcp
from app.models import User
from app.security import hash_password


def _build_http_client(app):
    mcp_server = build_fastmcp(app.state.session_factory, app.state.settings)
    starlette_app = mcp_server.streamable_http_app()
    return TestClient(starlette_app)


def _make_admin_and_token(app):
    with app.state.session_factory() as session:
        admin = User(email="admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(admin)
        session.flush()
        created = create_api_token(session, name="http-test-agent", actor=admin)
        session.commit()
        token_id = created.token.id
    return created.plaintext, token_id


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.1"},
    },
}


def test_missing_authorization_header_is_rejected(app):
    with _build_http_client(app) as client:
        response = client.post("/mcp", json=INITIALIZE_BODY, headers=MCP_HEADERS)

    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


def test_garbage_bearer_token_is_rejected(app):
    with _build_http_client(app) as client:
        headers = {**MCP_HEADERS, "Authorization": "Bearer mgrc_totally-not-a-real-token"}
        response = client.post("/mcp", json=INITIALIZE_BODY, headers=headers)

    assert response.status_code == 401


def test_revoked_token_is_rejected(app):
    plaintext, token_id = _make_admin_and_token(app)
    with app.state.session_factory() as session:
        from app.models import ApiToken

        token = session.get(ApiToken, token_id)
        admin = session.get(User, token.created_by_user_id)
        revoke_api_token(session, token, actor=admin)
        session.commit()

    with _build_http_client(app) as client:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {plaintext}"}
        response = client.post("/mcp", json=INITIALIZE_BODY, headers=headers)

    assert response.status_code == 401


def test_expired_token_is_rejected(app):
    with app.state.session_factory() as session:
        admin = User(email="admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(admin)
        session.flush()
        past = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=1)
        created = create_api_token(session, name="expired-agent", actor=admin, expires_at=past)
        session.commit()
        plaintext = created.plaintext

    with _build_http_client(app) as client:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {plaintext}"}
        response = client.post("/mcp", json=INITIALIZE_BODY, headers=headers)

    assert response.status_code == 401


def test_valid_token_passes_the_auth_gate(app):
    plaintext, _ = _make_admin_and_token(app)

    with _build_http_client(app) as client:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {plaintext}"}
        response = client.post("/mcp", json=INITIALIZE_BODY, headers=headers)

    # A valid token must never receive the 401 auth-gate rejection —
    # whatever status the protocol layer itself returns for this request
    # is a separate concern from whether auth let it through.
    assert response.status_code != 401
