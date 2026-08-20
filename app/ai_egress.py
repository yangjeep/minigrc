"""Data-egress/confidentiality boundary between compliance domain objects
and any BYOK AI provider request (issue #49).

This module is authored ahead of #15's BYOK provider abstraction, on
purpose: the issue that defines it is explicit that the boundary must
*inform* #15's implementation, not follow it. There is no prompt-assembly
code anywhere in this repository yet — grepped before writing this module
(see docs/superpowers/specs/2026-08-20-issue49-ai-data-egress-boundary-design.md
§1). When #15 is implemented, its prompt-assembly code must build every
outbound provider request exclusively from an `AiPromptContext` produced
by one of this module's `project_*` functions — never by serializing an
ORM row or reading object storage directly.

Structural guarantees this module's own shape enforces (not caller
discipline):

1. Every `project_*` function takes an already-loaded ORM row and never
   calls `app/object_storage.py`/`app/storage.py` to read file bytes, so
   raw evidence/document content cannot reach a provider through this
   module at all.
2. Each function returns only fields drawn from a fixed, reviewed
   allowlist (Tier 1 below). Free-text narrative fields (Tier 2) are
   included only when the caller supplies an explicit, reasoned
   `AiEgressOptIn` — a single, deliberate, distinguishable action.
3. Directly identifying PII of a specific person (email, display name,
   external directory id, or a bare "owner" name string) is never
   returned by any function here, opt-in or not (Tier 3) — see the
   design doc §2 for why this is a hard exclusion rather than a further
   opt-in tier.
4. `record_ai_egress` — the only function that logs what left the
   deployment — has no credential parameter, so it cannot leak one; its
   serialized output additionally passes through a defensive scrub for
   credential-shaped substrings, as belt-and-suspenders only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import AuditEvent, ControlOccurrence, EvidenceArtifact, Finding, Policy

_CREDENTIAL_LIKE_RE = re.compile(
    r"(sk-[a-zA-Z0-9_-]{10,}|bearer\s+[a-zA-Z0-9._-]+|api[_-]?key\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AiEgressOptIn:
    """A single, explicit, logged decision to include Tier 2 free-text
    narrative fields for one AI call. Never grants access to Tier 3
    (identifying PII / raw file content) — no function in this module
    accepts a `Person` row or reads file bytes regardless of opt-in."""

    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason or not self.reason.strip():
            raise ValueError(
                "AiEgressOptIn.reason must be a non-empty, deliberate justification — "
                "an opt-in cannot be satisfied by an accidental or blank value."
            )


@dataclass(frozen=True)
class AiPromptContext:
    """The only shape a future BYOK provider request (#15) may be built
    from. `fields` is exactly what would leave the deployment for this
    call; nothing else about the source row is reachable from here.

    `fields` is a read-only `MappingProxyType`, not a plain `dict` —
    deliberately: #15's prompt-assembly code must not be able to widen
    what gets sent by mutating the context after this module built it.
    Any additional field a future caller wants sent has to go through a
    `project_*` function's own reviewed allowlist, not an ad hoc
    `context.fields["x"] = ...`.
    """

    entity_type: str
    entity_id: str
    fields: MappingProxyType
    included_free_text: bool


def _context(
    entity_type: str, entity_id: str, fields: dict[str, object], opt_in: AiEgressOptIn | None
) -> AiPromptContext:
    return AiPromptContext(entity_type, entity_id, MappingProxyType(fields), opt_in is not None)


def project_evidence_artifact(
    artifact: EvidenceArtifact, *, opt_in: AiEgressOptIn | None = None
) -> AiPromptContext:
    latest = artifact.latest_version
    fields: dict[str, object] = {
        "title": artifact.title,
        "version_count": len(artifact.versions),
        "latest_version_number": latest.version_number if latest else None,
        "media_type": latest.media_type if latest else None,
        "byte_size": latest.byte_size if latest else None,
        "sha256": latest.sha256 if latest else None,
        "captured_at": latest.captured_at.isoformat() if latest else None,
        "source_type": latest.source_type if latest else None,
    }
    if opt_in is not None:
        fields["description"] = artifact.description
    return _context("evidence_artifact", artifact.id, fields, opt_in)


def project_control_occurrence(
    occurrence: ControlOccurrence, *, opt_in: AiEgressOptIn | None = None
) -> AiPromptContext:
    fields: dict[str, object] = {
        "control_name": occurrence.control.name if occurrence.control else None,
        "origin": occurrence.origin,
        "due_at": occurrence.due_at.isoformat() if occurrence.due_at else None,
        "performed_at": occurrence.performed_at.isoformat() if occurrence.performed_at else None,
        "has_responsible_owner": occurrence.responsible_person_id is not None,
    }
    if opt_in is not None:
        fields["scope_note"] = occurrence.scope_note
        fields["evidence_note"] = occurrence.evidence_note
    return _context("control_occurrence", occurrence.id, fields, opt_in)


def project_finding(finding: Finding, *, opt_in: AiEgressOptIn | None = None) -> AiPromptContext:
    fields: dict[str, object] = {
        "title": finding.title,
        "severity": finding.severity,
        "status": finding.status,
        "due_date": finding.due_date.isoformat() if finding.due_date else None,
        "has_owner": finding.owner_person_id is not None,
    }
    if opt_in is not None:
        fields["description"] = finding.description
        fields["closure_note"] = finding.closure_note
    return _context("finding", finding.id, fields, opt_in)


def project_policy(policy: Policy, *, opt_in: AiEgressOptIn | None = None) -> AiPromptContext:
    fields: dict[str, object] = {
        "title": policy.title,
        "status": policy.status,
        "effective_date": policy.effective_date.isoformat() if policy.effective_date else None,
        "next_review_date": policy.next_review_date.isoformat() if policy.next_review_date else None,
        "has_named_owner": bool(policy.owner),
    }
    if opt_in is not None:
        fields["description"] = policy.description
    return _context("policy", policy.id, fields, opt_in)


def _scrub_credential_like(text: str) -> str:
    return _CREDENTIAL_LIKE_RE.sub("[redacted-credential-like-value]", text)


def record_ai_egress(
    session: Session,
    context: AiPromptContext,
    *,
    task_type: str,
    provider_name: str = "",
    model: str = "",
    actor: str = "system",
) -> AuditEvent:
    """Log exactly what left the deployment for one AI call.

    Deliberately has no credential parameter — it cannot leak what it
    never receives. The JSON detail is additionally passed through
    `_scrub_credential_like` as defense-in-depth only; the primary
    guarantee is that no `project_*` allowlist above ever includes a
    secret-shaped field.
    """
    detail_payload = {
        "task_type": task_type,
        "provider_name": provider_name,
        "model": model,
        "included_free_text": context.included_free_text,
        "fields_sent": dict(context.fields),
    }
    detail = _scrub_credential_like(json.dumps(detail_payload, sort_keys=True, default=str))
    return record_audit_event(
        session,
        entity_type=f"ai_egress:{context.entity_type}",
        entity_id=context.entity_id,
        action="prompt_sent",
        detail=detail,
        actor=actor,
    )
