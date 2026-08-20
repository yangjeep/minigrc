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
4. Every string value this module puts into `AiPromptContext.fields` is
   scrubbed for email-shaped and credential-shaped substrings *before*
   it is ever wrapped into the context — i.e. before it could reach a
   real provider request, not only before it reaches the audit log. Tier
   1 fields (`title`/`control_name`/etc.) are ordinary human-authored
   labels, not typed-for-identity fields, but nothing stops a human from
   pasting an email or a rotated key into one — this is defense-in-depth
   against exactly that accident, not a claim that titles are untrusted
   by design. A whole matched value is replaced outright (never just the
   matched substring), so a credential split across a line break by an
   editor's word-wrap can't leave a partially-redacted tail exposed
   while looking fully redacted.
5. `record_ai_egress` — the only function that logs what left the
   deployment — has no credential parameter, so it cannot leak one, and
   applies the same scrub to its own caller-supplied `task_type`/
   `provider_name`/`model` strings (which are not part of `fields` and
   so aren't covered by guarantee 4 above).

What this module deliberately does *not* attempt: detecting a bare,
prefix-less high-entropy secret (e.g. an Azure OpenAI key or an AWS
secret access key) by a generic "long random string" heuristic. Tier 1
already includes a legitimate long hex string (`sha256`) that such a
heuristic would false-positive on and corrupt in the audit trail — worse
than the gap it would close. The credential patterns below are therefore
restricted to distinctive, well-known prefixes/shapes; the primary
guarantee against secret leakage remains structural (no `project_*`
allowlist includes a credential-shaped column, and `record_ai_egress`
has no credential parameter), not this regex.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import AuditEvent, ControlOccurrence, EvidenceArtifact, Finding, Policy

_EMAIL_LIKE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Distinctive, well-known credential prefixes/shapes only — see the
# module docstring for why a generic high-entropy heuristic is
# deliberately avoided.
_CREDENTIAL_LIKE_RE = re.compile(
    r"("
    r"sk-[a-zA-Z0-9_-]{10,}"  # OpenAI-style (incl. sk-proj-...)
    r"|gh[po]_[a-zA-Z0-9]{20,}"  # GitHub PAT / OAuth token
    r"|github_pat_[a-zA-Z0-9_]{20,}"  # GitHub fine-grained PAT
    r"|AIzaSy[a-zA-Z0-9_-]{20,}"  # Google API key
    r"|xox[baprs]-[a-zA-Z0-9-]{10,}"  # Slack token
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|bearer\s+[a-zA-Z0-9._-]+"  # Authorization: Bearer ...
    r"|api[_\s-]?key\s*[:=]\s*\S+"  # "api key: ...", "api_key=...", etc.
    r"|[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:@/]+:[^\s@/]+@"  # scheme://user:pass@ (connection strings)
    r")",
    re.IGNORECASE,
)

# Category "C" (control/format, incl. zero-width space U+200B and the
# BOM U+FEFF) and "Z" (space/line/paragraph separators) characters don't
# make a reason meaningful — str.strip() alone does not catch these,
# which is exactly what lets an invisible-character "reason" defeat a
# naive non-empty check.
_MEANINGLESS_REASON_CATEGORIES = ("C", "Z")


def _is_meaningless_reason(reason: str) -> bool:
    visible = "".join(
        ch for ch in reason if unicodedata.category(ch)[0] not in _MEANINGLESS_REASON_CATEGORIES
    )
    return not visible.strip()


def _scrub_string(value: str) -> str:
    """Replace the *entire* value, never just a matched span, if it looks
    like it contains a credential or an email address. Whole-value
    replacement is deliberate: a regex that only substitutes the matched
    substring can leave part of a secret exposed (e.g. split across a
    line break) while the presence of the marker falsely signals a full
    redaction."""
    if _CREDENTIAL_LIKE_RE.search(value):
        return "[redacted-credential-like-value]"
    if _EMAIL_LIKE_RE.search(value):
        return "[redacted-email-like-value]"
    return value


@dataclass(frozen=True)
class AiEgressOptIn:
    """A single, explicit, logged decision to include Tier 2 free-text
    narrative fields for one AI call. Never grants access to Tier 3
    (identifying PII / raw file content) — no function in this module
    accepts a `Person` row or reads file bytes regardless of opt-in."""

    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not isinstance(self.reason, str):
            raise TypeError("AiEgressOptIn.actor and .reason must be strings.")
        if _is_meaningless_reason(self.reason):
            raise ValueError(
                "AiEgressOptIn.reason must be a non-empty, deliberate justification — "
                "an opt-in cannot be satisfied by an accidental, blank, or invisible-character value."
            )


@dataclass(frozen=True)
class AiPromptContext:
    """The only shape a future BYOK provider request (#15) may be built
    from. `fields` is exactly what would leave the deployment for this
    call (already scrubbed per the module docstring); nothing else about
    the source row is reachable from here.

    `fields` is a read-only `MappingProxyType`, not a plain `dict` —
    deliberately: #15's prompt-assembly code must not be able to widen
    what gets sent by mutating the context after this module built it.
    Any additional field a future caller wants sent has to go through a
    `project_*` function's own reviewed allowlist, not an ad hoc
    `context.fields["x"] = ...`.

    `opt_in_actor`/`opt_in_reason` carry the deliberate justification
    forward so `record_ai_egress` can persist *why* Tier 2 content was
    included, not only *that* it was — these are independent of
    `record_ai_egress`'s own `actor` parameter (which identifies who/what
    triggered that particular provider call, and may legitimately differ
    from who granted the opt-in, e.g. a scheduled job acting on a
    human's earlier decision).
    """

    entity_type: str
    entity_id: str
    fields: MappingProxyType
    included_free_text: bool
    opt_in_actor: str | None = None
    opt_in_reason: str | None = None


def _context(
    entity_type: str, entity_id: str, fields: dict[str, object], opt_in: AiEgressOptIn | None
) -> AiPromptContext:
    scrubbed = {
        key: (_scrub_string(value) if isinstance(value, str) else value) for key, value in fields.items()
    }
    return AiPromptContext(
        entity_type=entity_type,
        entity_id=entity_id,
        fields=MappingProxyType(scrubbed),
        included_free_text=opt_in is not None,
        opt_in_actor=opt_in.actor if opt_in is not None else None,
        opt_in_reason=opt_in.reason if opt_in is not None else None,
    )


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


def project_readiness_snapshot(
    stage: str,
    stage_reason: str,
    category_counts: dict[str, int],
    top_item_reasons: tuple[str, ...],
) -> AiPromptContext:
    """Aggregate-only projection for #15's Observe capability. Every value
    here is already system-computed (a readiness stage/category/count, or
    one of `app/readiness.py`'s own deterministic `reason` strings) —
    never a user-authored free-text field — so no `AiEgressOptIn` is
    required or accepted. `top_item_reasons` is joined into a single
    string (not left as a list) so it is covered by `_context`'s existing
    per-field scrub, which only inspects top-level string values.
    """
    fields: dict[str, object] = {
        "stage": stage,
        "stage_reason": stage_reason,
        "category_counts": dict(category_counts),
        "top_item_reasons": "\n".join(top_item_reasons),
    }
    return _context("readiness_snapshot", "current", fields, opt_in=None)


def project_reminder_context(category: str, reason: str, link: str) -> AiPromptContext:
    """Aggregate-only projection for #15's Nag/Prioritize capabilities:
    one deterministic `ReadinessItem`'s already-computed fields. Like
    `project_readiness_snapshot`, these are system-generated strings
    (dates/counts/fixed category labels), never user-authored narrative,
    so no opt-in is required."""
    fields: dict[str, object] = {"category": category, "reason": reason, "link": link}
    return _context("readiness_item", f"{category}:{link}", fields, opt_in=None)


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
    never receives. `context.fields` was already scrubbed when the
    `project_*` function built it (module docstring, guarantee 4); this
    function additionally scrubs its own `task_type`/`provider_name`/
    `model` strings, since those are supplied directly by the caller and
    aren't covered by that earlier pass.
    """
    detail_payload = {
        "task_type": _scrub_string(task_type),
        "provider_name": _scrub_string(provider_name),
        "model": _scrub_string(model),
        "included_free_text": context.included_free_text,
        "opt_in_actor": context.opt_in_actor,
        "opt_in_reason": context.opt_in_reason,
        "fields_sent": dict(context.fields),
    }
    detail = json.dumps(detail_payload, sort_keys=True, default=str)
    return record_audit_event(
        session,
        entity_type=f"ai_egress:{context.entity_type}",
        entity_id=context.entity_id,
        action="prompt_sent",
        detail=detail,
        actor=actor,
    )
