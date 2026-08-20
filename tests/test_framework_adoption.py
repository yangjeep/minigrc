"""Unit/integration tests for issue #43's framework/catalog version
pinning (app/framework_adoption.py).

The `app` fixture already runs reconcile_system_catalogs on creation, so
both "iso27001" and "soc2" catalog families are already auto-adopted by
the time these tests run (see test_reconcile_system_catalogs_auto_adopts_
first_version below for a direct assertion of that). Everything else uses
a synthetic "test-family" so these tests stay independent of the exact
real system-catalog content.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app.framework_adoption import (
    FrameworkAdoptionError,
    adopt_framework,
    get_adoption,
    list_adoptions,
    list_upgrade_candidates,
    rebuild_framework_adoption_projection,
    upgrade_framework_adoption,
)
from app.models import (
    ControlPeriod,
    ControlRequirementMapping,
    Framework,
    InternalControl,
)


def _make_framework(session, *, family, version, name=None):
    framework = Framework(
        name=name or f"Test Catalog v{version}",
        version=version,
        catalog_family=family,
        is_placeholder_content=True,
    )
    session.add(framework)
    session.flush()
    return framework


def test_reconcile_system_catalogs_auto_adopts_first_version(app):
    with app.state.session_factory() as session:
        assert get_adoption(session, "iso27001") is not None
        assert get_adoption(session, "soc2") is not None


def test_adopt_framework_requires_catalog_family(app):
    with app.state.session_factory() as session:
        custom = Framework(name="Custom Framework", version="1.0")
        session.add(custom)
        session.flush()

        try:
            adopt_framework(session, custom)
            raised = False
        except FrameworkAdoptionError:
            raised = True
        assert raised


def test_adopt_framework_is_idempotent_for_the_same_framework(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="test-family", version="v1")
        first = adopt_framework(session, v1)
        session.commit()

        second = adopt_framework(session, v1)
        session.commit()

        assert first.id == second.id
        assert second.framework_id == v1.id


def test_adopt_framework_rejects_second_adoption_in_same_family(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="test-family-2", version="v1")
        v2 = _make_framework(session, family="test-family-2", version="v2")
        adopt_framework(session, v1)
        session.commit()

        try:
            adopt_framework(session, v2)
            raised = False
        except FrameworkAdoptionError:
            raised = True
        assert raised


def test_upgrade_framework_adoption_moves_current_version(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="test-family-3", version="v1")
        v2 = _make_framework(session, family="test-family-3", version="v2")
        adoption = adopt_framework(session, v1)
        session.commit()

        upgraded = upgrade_framework_adoption(session, adoption, v2)
        session.commit()

        assert upgraded.framework_id == v2.id
        assert upgraded.previous_framework_id == v1.id
        assert upgraded.upgraded_at is not None
        assert upgraded.adopted_at == adoption.adopted_at  # original adoption timestamp preserved


def test_upgrade_framework_adoption_rejects_cross_family(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-a", version="v1")
        other_family = _make_framework(session, family="family-b", version="v1")
        adoption = adopt_framework(session, v1)
        session.commit()

        try:
            upgrade_framework_adoption(session, adoption, other_family)
            raised = False
        except FrameworkAdoptionError:
            raised = True
        assert raised


def test_upgrade_framework_adoption_rejects_noop_upgrade(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-c", version="v1")
        adoption = adopt_framework(session, v1)
        session.commit()

        try:
            upgrade_framework_adoption(session, adoption, v1)
            raised = False
        except FrameworkAdoptionError:
            raised = True
        assert raised


def test_upgrade_framework_adoption_rejects_inactive_target(app):
    """Adversarial-review finding: list_upgrade_candidates' dropdown
    filters out inactive frameworks, but the service function itself
    must also reject one — a crafted request must not be able to bypass
    the UI's own filtering."""
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-inactive", version="v1")
        v2 = _make_framework(session, family="family-inactive", version="v2")
        v2.is_active = False
        adoption = adopt_framework(session, v1)
        session.commit()

        try:
            upgrade_framework_adoption(session, adoption, v2)
            raised = False
        except FrameworkAdoptionError:
            raised = True
        assert raised

        reloaded = get_adoption(session, "family-inactive")
        assert reloaded.framework_id == v1.id  # unchanged


def test_list_upgrade_candidates_excludes_current_version(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-d", version="v1")
        v2 = _make_framework(session, family="family-d", version="v2")
        v3 = _make_framework(session, family="family-d", version="v3")
        adoption = adopt_framework(session, v1)
        session.commit()

        candidates = list_upgrade_candidates(session, adoption)
        candidate_ids = {f.id for f in candidates}

    assert candidate_ids == {v2.id, v3.id}
    assert v1.id not in candidate_ids


def test_list_adoptions_returns_every_family(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-e", version="v1")
        adopt_framework(session, v1)
        session.commit()

        adoptions = list_adoptions(session)
        families = {a.catalog_family for a in adoptions}

    assert "family-e" in families
    assert "iso27001" in families
    assert "soc2" in families


def test_rebuild_framework_adoption_projection_reproduces_state(app):
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-f", version="v1")
        v2 = _make_framework(session, family="family-f", version="v2")
        adoption = adopt_framework(session, v1)
        session.commit()
        upgrade_framework_adoption(session, adoption, v2)
        session.commit()
        adoption_id = adoption.id

        rebuild_framework_adoption_projection(session)
        session.commit()

        rebuilt = session.get(type(adoption), adoption_id)
        assert rebuilt.framework_id == v2.id
        assert rebuilt.previous_framework_id == v1.id


def test_upgrade_never_touches_historical_mappings_or_periods(app):
    """The issue's own required verification: an org on catalog v1 with a
    completed audit period/mapping -> catalog v2 released -> org
    explicitly upgrades -> the v1 mapping/period data is unchanged and
    still resolves through the original v1 Framework/FrameworkRequirement
    rows; v1 itself is never deleted/renamed."""
    with app.state.session_factory() as session:
        v1 = _make_framework(session, family="family-g", version="v1")
        from app.requirements import add_requirement

        requirement_v1 = add_requirement(
            session, v1, reference_code="R1", title="Req 1", summary="", display_order=0
        )
        control = InternalControl(name="Access Review")
        session.add(control)
        session.flush()
        mapping = ControlRequirementMapping(control_id=control.id, requirement_id=requirement_v1.id)
        session.add(mapping)
        period = ControlPeriod(
            name="H1 2026",
            starts_on=datetime.date(2026, 1, 1),
            ends_on=datetime.date(2026, 6, 30),
            created_by="admin@example.com",
        )
        session.add(period)
        session.commit()

        mapping_id = mapping.id
        requirement_v1_id = requirement_v1.id
        v1_id = v1.id
        period_id = period.id

        adoption = adopt_framework(session, v1)
        session.commit()

        v2 = _make_framework(session, family="family-g", version="v2")
        upgraded = upgrade_framework_adoption(session, adoption, v2)
        session.commit()

        # The v1 mapping is completely untouched.
        reloaded_mapping = session.get(ControlRequirementMapping, mapping_id)
        assert reloaded_mapping.requirement_id == requirement_v1_id

        # v1 itself still exists, unrenamed, still active.
        reloaded_v1 = session.get(Framework, v1_id)
        assert reloaded_v1 is not None
        assert reloaded_v1.version == "v1"
        assert reloaded_v1.is_active is True

        # The adoption now points at v2, but v1's own data is unaffected.
        assert upgraded.framework_id == v2.id
        assert (
            session.scalar(
                select(ControlRequirementMapping).where(
                    ControlRequirementMapping.requirement_id == requirement_v1_id
                )
            )
            is not None
        )

        # The period from before the upgrade is untouched — framework
        # adoption changes never mutate any ControlPeriod row (periods
        # are deliberately framework-neutral; see the design doc).
        reloaded_period = session.get(ControlPeriod, period_id)
        assert reloaded_period.name == "H1 2026"
