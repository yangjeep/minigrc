"""Generic-OIDC identity/role population as access-control evidence
(issue #20) — a second producer of `EvidenceSnapshot` (issue #32),
alongside `app/aws_connector.py::build_evidence_snapshot`, not a new
evidence model.

Captures miniGRC's own already-collected record of who has
authenticated via the configured generic OIDC provider
(`ExternalIdentity`, #17) and their currently-mapped role (#18) — never
a live call to an external IdP directory/admin API, which no standards-
based OIDC endpoint exposes (see
docs/superpowers/specs/2026-08-20-issue20-idp-identity-evidence-design.md
for the repository-reality check behind this scoping decision). Only
non-secret identity/role facts are captured; no token, client secret, or
raw provider claim of any kind is ever included.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EvidenceSnapshot, ExternalIdentity

MAX_NORMALIZED_PAYLOAD_CHARS = 20000

SOURCE_TYPE = "oidc_identity_population"
CHECK_KEY = "oidc_identity_population"


def capture_oidc_identity_population_snapshot(session: Session) -> EvidenceSnapshot:
    """Build (but don't add-to-session/commit) an EvidenceSnapshot
    freezing every ExternalIdentity currently linked via generic OIDC
    login, plus each linked User's currently-mapped role — same
    construction shape as app.aws_connector.build_evidence_snapshot, so
    both flow through the exact same evidence-linkage/readiness/#14
    machinery unmodified."""
    identities = session.scalars(
        select(ExternalIdentity).order_by(ExternalIdentity.issuer, ExternalIdentity.subject)
    ).all()

    population = [
        {
            "issuer": identity.issuer,
            "subject": identity.subject,
            "user_id": identity.user.id,
            "email": identity.user.email,
            "role": identity.user.role,
            "role_source": identity.user.role_source,
            "user_status": identity.user.status,
            "linked_at": identity.created_at.isoformat(),
        }
        for identity in identities
    ]

    canonical_json = json.dumps({"population": population}, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    return EvidenceSnapshot(
        source_type=SOURCE_TYPE,
        check_key=CHECK_KEY,
        status="collected",
        title=f"OIDC identity/role population ({len(population)} linked identities)",
        summary=(
            f"{len(population)} externally-linked identit{'y' if len(population) == 1 else 'ies'} via "
            "generic OIDC, with each account's currently-mapped miniGRC role."
        ),
        normalized_payload_json=canonical_json[:MAX_NORMALIZED_PAYLOAD_CHARS],
        raw_payload_sha256=payload_hash,
    )
