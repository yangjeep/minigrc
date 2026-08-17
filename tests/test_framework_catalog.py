"""Tests for issue #12's system framework catalog reconciliation
(app/framework_catalog.py) and the app/seed.py changes that depend on it.
See docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md
for the design, including the seed-duplication and concurrency-race bugs
this file's tests specifically guard against.
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import func, select

from app.framework_catalog import (
    SYSTEM_CATALOGS,
    CatalogRequirement,
    SystemCatalog,
    _find_or_create_catalog_framework,
    reconcile_system_catalogs,
)
from app.models import AuditEvent, Framework, FrameworkRequirement, InternalControl


def test_reconcile_creates_both_system_catalogs(app):
    with app.state.session_factory() as session:
        iso = session.scalar(select(Framework).where(Framework.catalog_key == "iso27001-2022-sample"))
        soc2 = session.scalar(select(Framework).where(Framework.catalog_key == "soc2-2017-sample"))

        assert iso is not None
        assert soc2 is not None
        assert iso.is_primary is False
        assert soc2.is_primary is True
        assert iso.is_placeholder_content is True
        assert soc2.is_placeholder_content is True
        assert len(iso.requirements) == 5
        assert len(soc2.requirements) == 9


def test_reconcile_is_idempotent_on_sequential_rerun(app):
    with app.state.session_factory() as session:
        reconcile_system_catalogs(session)
        session.commit()

        framework_count = session.scalar(select(func.count()).select_from(Framework))
        requirement_count = session.scalar(select(func.count()).select_from(FrameworkRequirement))

        reconcile_system_catalogs(session)
        session.commit()

        assert session.scalar(select(func.count()).select_from(Framework)) == framework_count
        assert session.scalar(select(func.count()).select_from(FrameworkRequirement)) == requirement_count


def test_reconcile_does_not_duplicate_audit_events_on_rerun(app):
    with app.state.session_factory() as session:
        before = session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "reconcile_create")
        )
        reconcile_system_catalogs(session)
        session.commit()
        after = session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "reconcile_create")
        )
        assert before == after


def test_find_or_create_catalog_framework_recovers_from_concurrent_insert(app):
    """Regression guard for the concurrency race (adversarial review of the
    #12 design): if another process/session commits the same catalog_key
    between this call's own existence check and its insert attempt, the
    resulting real IntegrityError must be caught and the winning row
    returned — not an uncaught exception that crashes startup."""
    catalog = SystemCatalog(
        catalog_key="race-test-catalog",
        name="Race Test Catalog",
        version="1",
        description="",
        is_primary=False,
        requirements=(),
    )

    with app.state.session_factory() as winner_session:
        winner = Framework(
            catalog_key=catalog.catalog_key,
            name=catalog.name,
            version=catalog.version,
            description="",
            is_placeholder_content=True,
            is_primary=False,
        )
        winner_session.add(winner)
        winner_session.commit()
        winner_id = winner.id

    with app.state.session_factory() as session:
        original_scalar = session.scalar
        call_count = {"n": 0}

        def flaky_scalar(*args, **kwargs):
            call_count["n"] += 1
            # First call is this function's own "does it already exist"
            # check — force it to miss, simulating the exact race window
            # where another process's commit lands after this check but
            # before this session's own insert. The recovery re-check
            # (second call, inside the except branch) uses the real
            # implementation, proving the actual row is found correctly.
            if call_count["n"] == 1:
                return None
            return original_scalar(*args, **kwargs)

        with patch.object(session, "scalar", side_effect=flaky_scalar):
            framework, created = _find_or_create_catalog_framework(session, catalog)

        assert created is False
        assert framework.id == winner_id

    with app.state.session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(Framework).where(Framework.catalog_key == catalog.catalog_key)
        )
        assert count == 1


def test_reconcile_never_mutates_existing_requirement_content(app):
    """Reconciliation is create-or-fill-missing only — it must never
    overwrite an existing requirement's title/summary even if the catalog
    definition in code changes later."""
    with app.state.session_factory() as session:
        iso = session.scalar(select(Framework).where(Framework.catalog_key == "iso27001-2022-sample"))
        requirement = session.scalar(
            select(FrameworkRequirement).where(
                FrameworkRequirement.framework_id == iso.id, FrameworkRequirement.reference_code == "A.5.1"
            )
        )
        requirement.title = "Locally edited title"
        session.commit()

        reconcile_system_catalogs(session)
        session.commit()

        session.refresh(requirement)
        assert requirement.title == "Locally edited title"


def test_seed_maps_a_demo_control_to_both_iso_and_soc2():
    """The GitHub issue's own verification step: demonstrate one shared
    control mapped to both frameworks, not only a synthetic unit test."""
    import tempfile

    from app.main import create_app

    app = create_app(data_dir=tempfile.mkdtemp())
    with app.state.session_factory() as session:
        control = session.scalar(
            select(InternalControl).where(InternalControl.name == "Information security policy set")
        )
        assert control is not None
        mapped_frameworks = {m.requirement.framework.catalog_key for m in control.mappings}
        assert mapped_frameworks == {"iso27001-2022-sample", "soc2-2017-sample"}


def test_seed_does_not_rerun_after_demo_controls_deleted():
    """Regression guard: the seed gate must not be based on InternalControl
    emptiness (deletable by any user) or Framework emptiness (no longer
    ever empty once reconciliation always runs) — both would either
    silently reinject demo data after a deliberate deletion, or never seed
    demo data at all."""
    import tempfile

    from app.main import create_app
    from app.seed import seed_if_empty

    app = create_app(data_dir=tempfile.mkdtemp())
    with app.state.session_factory() as session:
        for control in session.scalars(select(InternalControl)).all():
            session.delete(control)
        session.commit()

        assert seed_if_empty(session) is False
        session.commit()

        assert session.scalar(select(func.count()).select_from(InternalControl)) == 0


def test_deactivating_framework_does_not_delete_requirements_mappings_or_occurrences():
    """Design doc §10 / gap analysis §2: Framework.is_active=False must
    never delete requirements, mappings, or any ControlOccurrence/evidence
    reachable through those mappings' controls."""
    import tempfile

    from app.control_occurrences import record_occurrence_manually
    from app.main import create_app
    from app.models import ControlOccurrenceEvidence, ControlRequirementMapping, EvidenceSnapshot

    app = create_app(data_dir=tempfile.mkdtemp())
    with app.state.session_factory() as session:
        soc2 = session.scalar(select(Framework).where(Framework.catalog_key == "soc2-2017-sample"))
        control = session.scalar(
            select(InternalControl).where(InternalControl.name == "Information security policy set")
        )
        occurrence = record_occurrence_manually(session, control, actor_type="system")
        snapshot = EvidenceSnapshot(
            source_type="test", check_key="test-check", title="Test evidence", raw_payload_sha256="a" * 64
        )
        session.add(snapshot)
        session.flush()
        session.add(ControlOccurrenceEvidence(occurrence_id=occurrence.id, evidence_snapshot_id=snapshot.id))
        session.commit()

        requirement_count_before = len(soc2.requirements)
        mapping_count_before = session.scalar(select(func.count()).select_from(ControlRequirementMapping))

        soc2.is_active = False
        session.commit()

        session.refresh(soc2)
        assert soc2.is_active is False
        assert len(soc2.requirements) == requirement_count_before
        assert (
            session.scalar(select(func.count()).select_from(ControlRequirementMapping))
            == mapping_count_before
        )
        assert session.get(type(occurrence), occurrence.id) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ControlOccurrenceEvidence)
                .where(ControlOccurrenceEvidence.occurrence_id == occurrence.id)
            )
            == 1
        )


def test_system_catalogs_have_no_duplicate_reference_codes_within_a_catalog():
    for catalog in SYSTEM_CATALOGS:
        codes = [r.reference_code for r in catalog.requirements]
        assert len(codes) == len(set(codes))


def test_catalog_requirement_and_system_catalog_are_usable_dataclasses():
    req = CatalogRequirement(reference_code="X.1", title="t", summary="s", display_order=0)
    assert req.reference_code == "X.1"
