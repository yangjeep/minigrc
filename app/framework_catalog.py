"""System framework catalog reconciliation (issue #12).

Creates/fills the ISO 27001 and SOC 2 placeholder catalogs idempotently,
safe under concurrent callers (see `_find_or_create_catalog_framework`'s
savepoint-and-retry shape, matching app/events.py's established pattern).
Never updates or removes an existing Framework/FrameworkRequirement row —
additive only. See
docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md
for the full design and its reconciliation against prior unshipped work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.models import Framework, FrameworkRequirement
from app.requirements import add_requirement

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogRequirement:
    reference_code: str
    title: str
    summary: str
    display_order: int


@dataclass(frozen=True)
class SystemCatalog:
    catalog_key: str
    name: str
    version: str
    description: str
    is_primary: bool
    requirements: tuple[CatalogRequirement, ...]


# Placeholder catalogues, paraphrased for this repository — not the
# licensed normative text of ISO/IEC 27001 or the AICPA Trust Services
# Criteria. See docs/domain/domain-model.md's copyright boundary and
# docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md
# §3 for the sources used to shape (not copy) this structure.
SYSTEM_CATALOGS: tuple[SystemCatalog, ...] = (
    SystemCatalog(
        catalog_key="iso27001-2022-sample",
        name="ISO/IEC 27001:2022 Annex A (sample catalogue)",
        version="2022",
        description=(
            "Placeholder catalogue for local development and demos. Reference codes "
            "follow the public Annex A theme numbering, but titles and summaries below "
            "are paraphrased placeholders written for this repository — not the "
            "licensed normative text of the standard. Replace with your "
            "organization's own licensed copy before relying on this for a real audit."
        ),
        is_primary=False,
        requirements=(
            CatalogRequirement(
                "A.5.1",
                "Policies for information security",
                "Top-level direction and topic-specific policies exist, are approved, and communicated.",
                0,
            ),
            CatalogRequirement(
                "A.5.9",
                "Inventory of information and assets",
                "Assets associated with information and information processing are identified "
                "and inventoried.",
                1,
            ),
            CatalogRequirement(
                "A.8.1",
                "User endpoint devices",
                "Information stored on, processed by, or accessible via endpoint devices is protected.",
                2,
            ),
            CatalogRequirement(
                "A.8.8",
                "Management of technical vulnerabilities",
                "Information about technical vulnerabilities is obtained and exposure is evaluated.",
                3,
            ),
            CatalogRequirement(
                "A.5.30",
                "ICT readiness for business continuity",
                "ICT readiness is planned, implemented, maintained, and tested against continuity "
                "objectives.",
                4,
            ),
        ),
    ),
    SystemCatalog(
        catalog_key="soc2-2017-sample",
        name="SOC 2 Trust Services Criteria — Security (sample catalogue)",
        version="2017 (2022 revised points of focus)",
        description=(
            "Placeholder catalogue for local development and demos, covering only the "
            "mandatory Security category's nine Common Criteria (CC) series with one "
            "representative reference code per series — not a full catalogue. Titles "
            "and summaries are paraphrased placeholders written for this repository, "
            "not the licensed AICPA Trust Services Criteria text. The four optional "
            "categories (Availability, Processing Integrity, Confidentiality, Privacy) "
            "are not seeded; add them via CSV import or the requirement form once your "
            "organization scopes them in. Replace with your own licensed copy before "
            "relying on this for a real audit."
        ),
        is_primary=True,
        requirements=(
            CatalogRequirement(
                "CC1.1",
                "Control environment: commitment to integrity and ethical values",
                "Leadership sets and models expected standards of conduct, and violations are "
                "addressed consistently.",
                0,
            ),
            CatalogRequirement(
                "CC2.1",
                "Communication and information: internal communication",
                "Operational and control-relevant information reaches the people who need it to "
                "carry out their responsibilities.",
                1,
            ),
            CatalogRequirement(
                "CC3.1",
                "Risk assessment: objectives",
                "Business objectives are documented clearly enough that risks threatening them can "
                "be identified and evaluated.",
                2,
            ),
            CatalogRequirement(
                "CC4.1",
                "Monitoring activities: ongoing and separate evaluations",
                "Controls are periodically checked to confirm they are actually operating, not "
                "just assumed to be.",
                3,
            ),
            CatalogRequirement(
                "CC5.1",
                "Control activities: mitigating risk",
                "Specific activities are put in place to bring identified risks down to an acceptable level.",
                4,
            ),
            CatalogRequirement(
                "CC6.1",
                "Logical and physical access controls: restricting access",
                "Access to systems and data is restricted through technical controls so only "
                "authorized users can get in.",
                5,
            ),
            CatalogRequirement(
                "CC7.1",
                "System operations: detecting and monitoring anomalies",
                "System activity is monitored to catch unusual behavior that could signal an error "
                "or a malicious act.",
                6,
            ),
            CatalogRequirement(
                "CC8.1",
                "Change management: authorizing changes",
                "Changes to infrastructure and software go through approval and review before release.",
                7,
            ),
            CatalogRequirement(
                "CC9.1",
                "Risk mitigation: identifying and managing business disruption",
                "Risks that could significantly disrupt the business are identified and actively "
                "managed, not merely noted.",
                8,
            ),
        ),
    ),
)


def _find_or_create_catalog_framework(session: Session, catalog: SystemCatalog) -> tuple[Framework, bool]:
    """Returns (framework, created). Safe under concurrent callers: a
    savepoint isolates the insert attempt so a losing concurrent creator
    re-reads the winner's row instead of crashing the whole session —
    matches app/events.py's established idempotent-append shape, not a
    new locking primitive."""
    existing = session.scalar(select(Framework).where(Framework.catalog_key == catalog.catalog_key))
    if existing is not None:
        return existing, False

    try:
        with session.begin_nested():
            framework = Framework(
                catalog_key=catalog.catalog_key,
                name=catalog.name,
                version=catalog.version,
                description=catalog.description,
                is_placeholder_content=True,
                is_primary=catalog.is_primary,
            )
            session.add(framework)
            session.flush()
    except IntegrityError as exc:
        # Only treat this as "someone else won the race" if a row with
        # this exact catalog_key now genuinely exists — matching
        # app/events.py's discriminate-before-recover shape, not a blanket
        # "any IntegrityError means a race." A different, real caller bug
        # (e.g. a NOT NULL/CHECK violation) would otherwise be silently
        # swallowed and reported as "not created" forever.
        winner = session.scalar(select(Framework).where(Framework.catalog_key == catalog.catalog_key))
        if winner is None:
            raise RuntimeError(
                f"IntegrityError creating catalog framework '{catalog.catalog_key}' but no row "
                "with that catalog_key exists — not a concurrent-creation race, re-raising"
            ) from exc
        return winner, False
    return framework, True


def _find_or_create_catalog_requirement(
    session: Session, framework: Framework, requirement: CatalogRequirement
) -> bool:
    """Returns True if this call created the requirement (used for the
    rowcount-gated audit-write pattern below)."""
    existing = session.scalar(
        select(FrameworkRequirement).where(
            FrameworkRequirement.framework_id == framework.id,
            FrameworkRequirement.reference_code == requirement.reference_code,
        )
    )
    if existing is not None:
        return False

    try:
        with session.begin_nested():
            add_requirement(
                session,
                framework,
                reference_code=requirement.reference_code,
                title=requirement.title,
                summary=requirement.summary,
                display_order=requirement.display_order,
            )
    except IntegrityError as exc:
        # Same discriminate-before-recover shape as
        # _find_or_create_catalog_framework above — verify a concurrent
        # creator actually won before treating this as a race rather than
        # a real caller bug.
        winner = session.scalar(
            select(FrameworkRequirement).where(
                FrameworkRequirement.framework_id == framework.id,
                FrameworkRequirement.reference_code == requirement.reference_code,
            )
        )
        if winner is None:
            raise RuntimeError(
                f"IntegrityError creating requirement '{requirement.reference_code}' for "
                f"framework '{framework.catalog_key}' but no matching row exists — not a "
                "concurrent-creation race, re-raising"
            ) from exc
        return False
    return True


def reconcile_system_catalogs(session: Session) -> list[Framework]:
    """Idempotently ensure every SYSTEM_CATALOGS entry, and all of its
    requirements, exist. Safe to call on every startup, unconditionally,
    including under concurrent callers. Never mutates an existing row."""
    frameworks = []
    for catalog in SYSTEM_CATALOGS:
        framework, framework_created = _find_or_create_catalog_framework(session, catalog)
        if framework_created:
            record_audit_event(
                session,
                entity_type="framework",
                entity_id=framework.id,
                action="reconcile_create",
                detail=f"Reconciled system catalog framework '{framework.name}' ({catalog.catalog_key})",
            )
        for requirement in catalog.requirements:
            requirement_created = _find_or_create_catalog_requirement(session, framework, requirement)
            if requirement_created:
                record_audit_event(
                    session,
                    entity_type="framework",
                    entity_id=framework.id,
                    action="reconcile_create",
                    detail=f"Reconciled requirement '{requirement.reference_code}' for {catalog.catalog_key}",
                )
        frameworks.append(framework)
    return frameworks
