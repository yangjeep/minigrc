"""Integration tests for issue #36's read-only MCP server
(app/mcp_server.py), driven through a real MCP client session — not a
mock — via mcp.shared.memory.create_connected_server_and_client_session.
This exercises the identical protocol path (list_tools/call_tool) a real
MCP client (Claude Desktop, etc.) would use, just over in-memory streams
instead of HTTP.

A separate suite (tests/test_mcp_server_http_auth.py) proves the
HTTP-transport bearer-token enforcement that this in-memory path
deliberately bypasses (it talks directly to the FastMCP server object,
never through BearerAuthBackend/RequireAuthMiddleware).
"""

from __future__ import annotations

import datetime

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import func, select

from app.api_tokens import create_api_token
from app.compliance_scope import define_or_revise_scope
from app.control_occurrences import record_occurrence_manually
from app.events import DomainEvent
from app.finding_lifecycle import open_finding
from app.mcp_server import build_fastmcp
from app.models import (
    ConnectorInstance,
    EvidenceArtifact,
    EvidenceArtifactVersion,
    InternalControl,
    Person,
    Policy,
    User,
)
from app.security import hash_password


@pytest.fixture
def mcp_env(app):
    settings = app.state.settings
    session_factory = app.state.session_factory
    with session_factory() as session:
        admin = User(email="admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(admin)
        session.flush()
        create_api_token(session, name="test-agent", actor=admin)
        session.commit()
    return build_fastmcp(session_factory, settings), session_factory


@pytest.mark.anyio
async def test_list_tools_exposes_only_read_tools(mcp_env):
    mcp_server, _ = mcp_env
    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}

    assert "get_program_summary" in names
    assert "list_controls" in names
    # No write/mutation-shaped tool exists at all.
    forbidden_substrings = ("create", "update", "delete", "approve", "close", "revoke", "attest", "write")
    for name in names:
        for bad in forbidden_substrings:
            assert bad not in name.lower(), f"tool {name!r} looks like a write operation"


@pytest.mark.anyio
async def test_get_program_summary_before_any_scope(mcp_env):
    mcp_server, _ = mcp_env
    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("get_program_summary", {})

    assert result.structuredContent["scope_defined"] is False
    assert result.structuredContent["readiness_stage"] == "scope_incomplete"


@pytest.mark.anyio
async def test_get_scope_reflects_defined_scope(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        define_or_revise_scope(
            session,
            service_description="Widget SaaS",
            trust_service_categories=["availability"],
            audit_period_starts_on=None,
            audit_period_ends_on=None,
            data_categories="",
            locations="",
            exclusions="",
            exclusions_rationale="",
        )
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("get_scope", {})

    assert result.structuredContent["defined"] is True
    assert result.structuredContent["service_description"] == "Widget SaaS"
    assert "security" in result.structuredContent["trust_service_categories"]


@pytest.mark.anyio
async def test_list_controls_and_get_control_roundtrip(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        person = Person(email="owner@example.com", display_name="Owner Person", employment_status="active")
        session.add(person)
        session.flush()
        control = InternalControl(name="Access Review", owner_person_id=person.id)
        session.add(control)
        session.flush()
        record_occurrence_manually(session, control, actor_type="system")
        session.commit()
        control_id = control.id

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        listed = await client.call_tool("list_controls", {})
        detail = await client.call_tool("get_control", {"control_id": control_id})
        missing = await client.call_tool("get_control", {"control_id": "does-not-exist"})

    assert any(c["id"] == control_id for c in listed.structuredContent["controls"])
    assert detail.structuredContent["found"] is True
    assert detail.structuredContent["name"] == "Access Review"
    assert detail.structuredContent["owner_person"]["display_name"] == "Owner Person"
    assert detail.structuredContent["occurrence_count"] == 1
    assert missing.structuredContent["found"] is False


@pytest.mark.anyio
async def test_list_policies_excludes_file_bytes(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        session.add(Policy(title="Access Control Policy"))
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("list_policies", {})

    policies = result.structuredContent["policies"]
    assert any(p["title"] == "Access Control Policy" for p in policies)
    for policy in policies:
        assert "content" not in policy
        assert "bytes" not in policy


@pytest.mark.anyio
async def test_get_policy_reports_not_found_for_unknown_id(mcp_env):
    mcp_server, _ = mcp_env
    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("get_policy", {"policy_id": "does-not-exist"})

    assert result.structuredContent["found"] is False


@pytest.mark.anyio
async def test_list_findings_filters_by_status(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        control = InternalControl(name="Backup Control")
        session.add(control)
        session.flush()
        open_finding(
            session,
            title="Missing MFA",
            description="",
            severity="high",
            control_id=control.id,
        )
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        open_only = await client.call_tool("list_findings", {"status": "open"})
        closed_only = await client.call_tool("list_findings", {"status": "closed"})

    assert len(open_only.structuredContent["findings"]) == 1
    assert open_only.structuredContent["findings"][0]["severity"] == "high"
    assert len(closed_only.structuredContent["findings"]) == 0


@pytest.mark.anyio
async def test_list_evidence_artifacts_returns_latest_version_metadata_only(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        artifact = EvidenceArtifact(title="Access review export")
        session.add(artifact)
        session.flush()
        session.add(
            EvidenceArtifactVersion(
                id="evver00000000000000000000000001",
                artifact_id=artifact.id,
                version_number=1,
                object_key="evidence/x/y",
                original_filename="review.csv",
                media_type="text/csv",
                byte_size=1234,
                sha256="b" * 64,
                captured_at=datetime.datetime(2026, 1, 1),
            )
        )
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("list_evidence_artifacts", {})

    artifacts = result.structuredContent["evidence_artifacts"]
    assert len(artifacts) == 1
    entry = artifacts[0]
    assert entry["title"] == "Access review export"
    assert entry["latest_version"]["sha256"] == "b" * 64
    assert entry["latest_version"]["byte_size"] == 1234
    assert "object_key" not in entry["latest_version"]
    assert "download" not in str(entry).lower()


@pytest.mark.anyio
async def test_get_connector_status_excludes_config_and_secrets(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        admin = session.scalar(select(User))
        session.add(
            ConnectorInstance(
                connector_id="google_drive",
                manifest_version="1.0.0",
                display_name="Drive Prod",
                enabled=True,
                config_json='{"folder_id": "abc"}',
                secret_ref_json='{"api_key": "super-secret-id"}',
                created_by_user_id=admin.id,
            )
        )
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("get_connector_status", {})

    connectors = result.structuredContent["connectors"]
    assert len(connectors) == 1
    serialized = connectors[0]
    assert serialized["display_name"] == "Drive Prod"
    assert "config_json" not in serialized
    assert "secret_ref_json" not in serialized
    assert "super-secret-id" not in str(serialized)


@pytest.mark.anyio
async def test_mcp_tool_calls_never_change_domain_event_count(mcp_env):
    """Structural proof of read-only-ness: calling every list tool must
    not append a single DomainEvent row."""
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(DomainEvent))

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        await client.call_tool("get_program_summary", {})
        await client.call_tool("get_scope", {})
        await client.call_tool("list_controls", {})
        await client.call_tool("list_policies", {})
        await client.call_tool("list_findings", {})
        await client.call_tool("list_evidence_artifacts", {})
        await client.call_tool("get_connector_status", {})

    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(DomainEvent))

    assert after == before


@pytest.mark.anyio
async def test_list_controls_respects_limit_and_reports_truncated(mcp_env):
    mcp_server, session_factory = mcp_env
    with session_factory() as session:
        for i in range(5):
            session.add(InternalControl(name=f"Control {i}"))
        session.commit()

    async with create_connected_server_and_client_session(mcp_server._mcp_server) as client:
        result = await client.call_tool("list_controls", {"limit": 2})

    assert len(result.structuredContent["controls"]) == 2
    assert result.structuredContent["truncated"] is True
