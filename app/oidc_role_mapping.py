"""Claim/group-to-role mapping for the generic OIDC login path (issue #18).

Deterministic, admin-configured, and fully inert until an admin adds at
least one `OidcRoleMapping` row — with zero rows configured, every #17
behavior (new-user default role, no change on return login) is
unchanged. Once configured, a user's role is recomputed from their
current verified ID token claims on every login, unless
`User.role_source == "local"` (an admin's explicit override, or the
bootstrap first-admin grant — see app/routers/oidc.py).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import USER_ROLES, OidcRoleMapping, User

LEAST_PRIVILEGED_ROLE = "reader"
ROLE_PRECEDENCE = USER_ROLES  # ("admin", "operator", "reader", "auditor")
_MAX_CLAIM_VALUES = 1000


def extract_claim_values(claims: dict, claim_name: str) -> list[str]:
    """Extract candidate group/role claim values for mapping.

    Never guesses a provider-specific delimiter: a list claim (the
    normal OIDC `groups` shape) is used item-by-item; a string claim is
    treated as exactly one value. Capped defensively against an
    oversized/malicious claim array.
    """
    value = claims.get(claim_name)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value[:_MAX_CLAIM_VALUES]]
    return [str(value)]


def compute_mapped_role(claim_values: list[str], mapping_by_value: dict[str, str]) -> str:
    """The highest-precedence role among the user's claim values that
    have a configured mapping, or the least-privileged role if none of
    the user's claim values match any configured mapping."""
    matched_roles = {mapping_by_value[value] for value in claim_values if value in mapping_by_value}
    for role in ROLE_PRECEDENCE:
        if role in matched_roles:
            return role
    return LEAST_PRIVILEGED_ROLE


def load_role_mappings(session: Session) -> dict[str, str]:
    rows = session.scalars(select(OidcRoleMapping)).all()
    return {row.claim_value: row.role for row in rows}


def apply_role_mapping(
    session: Session, user: User, claims: dict, *, role_claim_name: str, actor: str
) -> None:
    """Recompute `user.role` from configured claim mappings.

    No-op if no `OidcRoleMapping` rows exist at all (feature not opted
    into) or if `user.role_source == "local"` (admin-pinned/bootstrap).
    Writes an audit event only when the role actually changes.
    """
    mapping_by_value = load_role_mappings(session)
    if not mapping_by_value or user.role_source == "local":
        return

    claim_values = extract_claim_values(claims, role_claim_name)
    new_role = compute_mapped_role(claim_values, mapping_by_value)

    if new_role == user.role and user.role_source == "oidc_mapped":
        return

    old_role = user.role
    user.role = new_role
    user.role_source = "oidc_mapped"
    session.flush()
    record_audit_event(
        session,
        entity_type="user",
        entity_id=user.id,
        action="oidc_role_mapping_applied",
        detail=(
            f"Role changed from '{old_role}' to '{new_role}' via OIDC group mapping (claims: {claim_values})"
        ),
        actor=actor,
    )
