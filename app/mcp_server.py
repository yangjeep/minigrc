"""Read-only MCP server for external/customer-owned agents (issue #36).

Exposes a small, stable set of coarse, typed, read-only tools over
existing domain/read-model concepts — never raw tables, never arbitrary
SQL, and (structurally, not by convention) never a write/mutation path:
this module never imports any `append_and_project`/lifecycle-command
function from any domain module. See
docs/superpowers/specs/2026-08-20-issue36-readonly-mcp-server-design.md
for the full design and the primary-source MCP-spec/SDK research behind
the auth approach below.

Deliberately NOT mounted inside `app.main.create_app`'s FastAPI
instance — `build_fastmcp` returns a standalone `FastMCP`; run it via
`python -m app.cli mcp-server` (its own small ASGI process, sharing the
same database) or in tests, directly through an in-memory MCP client
session. This avoids nesting FastMCP's own required session-manager
lifespan inside FastAPI's, for a surface most self-hosted operators will
leave disabled.

Auth: this implements only the MCP **resource-server** half (verify a
bearer token against our own `ApiToken` store via `TokenVerifier`) —
never a bespoke **authorization server** (`auth_server_provider` stays
unset, so FastMCP never registers `/authorize`/`/token`/`/register`
routes). A caller supplies `Authorization: Bearer <token>` directly;
there is no OAuth login flow to stand up for this first slice.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_tokens import verify_api_token
from app.compliance_scope import get_compliance_scope
from app.config import Settings
from app.models import (
    ComplianceScope,
    ConnectorInstance,
    ControlOccurrence,
    EvidenceArtifact,
    Finding,
    InternalControl,
    Person,
    Policy,
)
from app.readiness import compute_readiness_queue, compute_readiness_stage

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _iso(value: datetime.date | datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class ApiTokenVerifier(TokenVerifier):
    """Verifies a bearer token against the `ApiToken` store
    (app/api_tokens.py). Never raises — an unknown/revoked/expired token
    simply yields `None`, which FastMCP's `BearerAuthBackend` treats as
    "not authenticated" (401), the same fail-closed shape as
    app.security.verify_password."""

    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        with self._session_factory() as session:
            api_token = verify_api_token(session, token)
            if api_token is None:
                return None
            session.commit()  # persists last_used_at
            return AccessToken(
                token=token,
                client_id=api_token.id,
                scopes=[api_token.scope],
                subject=api_token.id,
            )


def _serialize_person_ref(session: Session, person_id: str | None) -> dict[str, Any] | None:
    if person_id is None:
        return None
    person = session.get(Person, person_id)
    if person is None:
        return None
    return {"person_id": person.id, "display_name": person.display_name}


def _serialize_control(session: Session, control: InternalControl) -> dict[str, Any]:
    total = session.scalar(
        select(func.count()).select_from(ControlOccurrence).where(ControlOccurrence.control_id == control.id)
    )
    overdue = session.scalar(
        select(func.count())
        .select_from(ControlOccurrence)
        .where(
            ControlOccurrence.control_id == control.id,
            ControlOccurrence.performed_at.is_(None),
            ControlOccurrence.due_at.is_not(None),
            ControlOccurrence.due_at < datetime.datetime.now(datetime.UTC).replace(tzinfo=None),
        )
    )
    effective_version = control.effective_version
    return {
        "id": control.id,
        "name": control.name,
        "status": control.status,
        "owner": control.owner or None,
        "owner_person": _serialize_person_ref(session, control.owner_person_id),
        "cadence_type": control.cadence_type,
        "cadence_interval_months": control.cadence_interval_months,
        "effective_version_number": effective_version.version_number if effective_version else None,
        "occurrence_count": total or 0,
        "occurrence_overdue_count": overdue or 0,
    }


def _serialize_policy(session: Session, policy: Policy) -> dict[str, Any]:
    effective = policy.effective_version
    return {
        "id": policy.id,
        "title": policy.title,
        "status": policy.status,
        "owner": policy.owner or None,
        "next_review_date": _iso(policy.next_review_date),
        "effective_version": (
            {
                "version_number": effective.version_number,
                "effective_at": _iso(effective.effective_at),
                "sha256": effective.sha256,
            }
            if effective
            else None
        ),
    }


def _serialize_finding(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "title": finding.title,
        "severity": finding.severity,
        "status": finding.status,
        "control_id": finding.control_id,
        "owner_person_id": finding.owner_person_id,
        "due_date": _iso(finding.due_date),
        "created_at": _iso(finding.created_at),
    }


def _serialize_evidence_artifact(artifact: EvidenceArtifact) -> dict[str, Any]:
    latest = artifact.latest_version
    return {
        "id": artifact.id,
        "title": artifact.title,
        "latest_version": (
            {
                "version_number": latest.version_number,
                "sha256": latest.sha256,
                "byte_size": latest.byte_size,
                "media_type": latest.media_type,
                "source_type": latest.source_type,
                "captured_at": _iso(latest.captured_at),
            }
            if latest
            else None
        ),
    }


def _serialize_connector_instance(instance: ConnectorInstance) -> dict[str, Any]:
    # Deliberately does not read config_json/secret_ref_json — only these
    # six named, non-secret fields are ever serialized for this tool.
    return {
        "connector_id": instance.connector_id,
        "display_name": instance.display_name,
        "enabled": instance.enabled,
        "last_test_status": instance.last_test_status,
        "last_success_at": _iso(instance.last_success_at),
        "last_failure_at": _iso(instance.last_failure_at),
        "last_error_summary": instance.last_error_summary or None,
    }


def _serialize_scope(scope: ComplianceScope) -> dict[str, Any]:
    return {
        "defined": True,
        "service_description": scope.service_description,
        "trust_service_categories": scope.trust_service_categories.split(","),
        "audit_period_starts_on": _iso(scope.audit_period_starts_on),
        "audit_period_ends_on": _iso(scope.audit_period_ends_on),
        "data_categories": scope.data_categories,
        "locations": scope.locations,
        "exclusions": scope.exclusions,
        "exclusions_rationale": scope.exclusions_rationale,
        "revision": scope.revision,
        "updated_at": _iso(scope.updated_at),
    }


def build_fastmcp(session_factory: Callable[[], Session], settings: Settings) -> FastMCP:
    """Build the read-only miniGRC MCP server. `session_factory` and
    `settings` are injected (not read from module globals) so tests can
    point this at an isolated database, matching
    `app.main.create_app(database_path=...)`'s existing convention."""
    issuer_url = settings.public_base_url or "http://localhost"
    mcp = FastMCP(
        name="minigrc",
        instructions=(
            "Read-only interoperability surface over a miniGRC compliance program. "
            "No tool here can approve, attest, close, or mutate any compliance state."
        ),
        token_verifier=ApiTokenVerifier(session_factory),
        auth=AuthSettings(issuer_url=issuer_url, resource_server_url=None, required_scopes=["read"]),
        stateless_http=True,
    )

    @mcp.tool()
    def get_program_summary() -> dict[str, Any]:
        """Program-wide snapshot: whether scope is defined, the current
        explainable readiness stage and why, and the top readiness
        blockers."""
        with session_factory() as session:
            scope = get_compliance_scope(session)
            stage = compute_readiness_stage(session, settings)
            queue = compute_readiness_queue(session)
            top = queue[:20]
            return {
                "scope_defined": scope is not None,
                "readiness_stage": stage.stage,
                "readiness_stage_reason": stage.reason,
                "blocker_count": len(queue),
                "top_blockers": [
                    {
                        "category": item.category,
                        "reason": item.reason,
                        "link": item.link,
                        "due_on": _iso(item.due_on),
                    }
                    for item in top
                ],
                "truncated": len(queue) > len(top),
            }

    @mcp.tool()
    def get_scope() -> dict[str, Any]:
        """The org's current declared compliance-program scope, or
        `{"defined": false}` if none has been declared yet."""
        with session_factory() as session:
            scope = get_compliance_scope(session)
            if scope is None:
                return {"defined": False}
            return _serialize_scope(scope)

    @mcp.tool()
    def list_controls(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """List internal controls with owner, cadence, effective-version
        status, and occurrence counts."""
        capped = _clamp_limit(limit)
        with session_factory() as session:
            total = session.scalar(select(func.count()).select_from(InternalControl)) or 0
            controls = session.scalars(
                select(InternalControl).order_by(InternalControl.name).limit(capped)
            ).all()
            return {
                "controls": [_serialize_control(session, control) for control in controls],
                "truncated": total > len(controls),
            }

    @mcp.tool()
    def get_control(control_id: str) -> dict[str, Any]:
        """A single control's detail. Returns `{"found": false}` if
        `control_id` does not exist — every tool in this module always
        returns a plain JSON object, never a bare null, so a missing-ID
        result is unambiguous to a calling agent."""
        with session_factory() as session:
            control = session.get(InternalControl, control_id)
            if control is None:
                return {"found": False, "control_id": control_id}
            return {"found": True, **_serialize_control(session, control)}

    @mcp.tool()
    def list_policies(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """List policies with their current effective version's metadata
        (never file bytes)."""
        capped = _clamp_limit(limit)
        with session_factory() as session:
            total = session.scalar(select(func.count()).select_from(Policy)) or 0
            policies = session.scalars(select(Policy).order_by(Policy.title).limit(capped)).all()
            return {
                "policies": [_serialize_policy(session, policy) for policy in policies],
                "truncated": total > len(policies),
            }

    @mcp.tool()
    def get_policy(policy_id: str) -> dict[str, Any]:
        """A single policy's detail. Returns `{"found": false}` if
        `policy_id` does not exist (see `get_control`'s docstring for why)."""
        with session_factory() as session:
            policy = session.get(Policy, policy_id)
            if policy is None:
                return {"found": False, "policy_id": policy_id}
            return {"found": True, **_serialize_policy(session, policy)}

    @mcp.tool()
    def list_findings(
        status: str | None = None, severity: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        """List findings, optionally filtered by `status` and/or `severity`."""
        capped = _clamp_limit(limit)
        with session_factory() as session:
            query = select(Finding)
            if status is not None:
                query = query.where(Finding.status == status)
            if severity is not None:
                query = query.where(Finding.severity == severity)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            findings = session.scalars(query.order_by(Finding.created_at.desc()).limit(capped)).all()
            return {
                "findings": [_serialize_finding(finding) for finding in findings],
                "truncated": total > len(findings),
            }

    @mcp.tool()
    def list_evidence_artifacts(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """List evidence artifacts with their latest captured version's
        metadata (hash/size/type/provenance) — never file bytes or a
        download reference."""
        capped = _clamp_limit(limit)
        with session_factory() as session:
            total = session.scalar(select(func.count()).select_from(EvidenceArtifact)) or 0
            artifacts = session.scalars(
                select(EvidenceArtifact).order_by(EvidenceArtifact.title).limit(capped)
            ).all()
            return {
                "evidence_artifacts": [_serialize_evidence_artifact(artifact) for artifact in artifacts],
                "truncated": total > len(artifacts),
            }

    @mcp.tool()
    def get_connector_status(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """List configured connector instances' health status — never
        their configuration or credentials."""
        capped = _clamp_limit(limit)
        with session_factory() as session:
            total = session.scalar(select(func.count()).select_from(ConnectorInstance)) or 0
            instances = session.scalars(
                select(ConnectorInstance).order_by(ConnectorInstance.display_name).limit(capped)
            ).all()
            return {
                "connectors": [_serialize_connector_instance(instance) for instance in instances],
                "truncated": total > len(instances),
            }

    return mcp
