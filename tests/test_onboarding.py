"""Domain-level tests for issue #30's onboarding-step derivation
(app/onboarding.py).

Every test in this file runs against the `app` fixture's real ambient
state, which already includes app/framework_catalog.py's system catalogs
and app/seed.py's demo InternalControl/Risk rows (both run unconditionally
inside create_app — see app/main.py). Some steps are therefore already
"complete" on a bare fixture purely because of that pre-existing demo
content (documented in
docs/superpowers/specs/2026-08-19-issue30-compliance-scope-onboarding-design.md
§1/§8) — tests assert against that real starting point rather than
pretending the database starts empty.
"""

from __future__ import annotations

import datetime

from app.compliance_scope import define_or_revise_scope
from app.models import ControlPeriod, Framework, InternalControl, Person, VendorSystem
from app.onboarding import compute_onboarding_steps, get_primary_framework
from app.starter_controls import generate_starter_controls_for_framework


def _step(steps, key):
    return next(s for s in steps if s.key == key)


def test_define_scope_step_is_incomplete_until_scope_exists(app):
    with app.state.session_factory() as session:
        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "define_scope").is_complete is False

        define_or_revise_scope(session, service_description="x")
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "define_scope").is_complete is True


def test_scope_people_systems_step_reflects_in_scope_flags(app):
    with app.state.session_factory() as session:
        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "scope_people_systems").is_complete is False

        session.add(Person(email="a@example.com", in_scope=True))
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "scope_people_systems").is_complete is True


def test_scope_people_systems_step_true_for_an_in_scope_vendor_system(app):
    with app.state.session_factory() as session:
        session.add(VendorSystem(system_name="GitHub", vendor_name="GitHub Inc.", in_scope=True))
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "scope_people_systems").is_complete is True


def test_starter_controls_step_reflects_real_coverage_gap(app):
    """The demo seed only maps SOC 2's CC1.1, leaving CC2.1-CC9.1
    uncovered — this step must start incomplete on a fresh deployment,
    not merely because *some* InternalControl rows exist."""
    with app.state.session_factory() as session:
        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "starter_controls").is_complete is False

        framework = get_primary_framework(session)
        generate_starter_controls_for_framework(session, framework)
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "starter_controls").is_complete is True


def test_starter_controls_step_is_incomplete_with_no_primary_framework(app):
    with app.state.session_factory() as session:
        session.execute(Framework.__table__.update().values(is_primary=False))
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "starter_controls").is_complete is False


def test_assign_owners_step_is_false_once_an_unowned_control_exists(app):
    with app.state.session_factory() as session:
        # Demo seed controls all have an owner, so this step starts True —
        # adding one unowned control must flip it back to False.
        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "assign_owners").is_complete is True

        session.add(InternalControl(name="New unowned control"))
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "assign_owners").is_complete is False


def test_evidence_source_step_true_when_s3_repository_configured(app):
    with app.state.session_factory() as session:
        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "evidence_source").is_complete is False

    configured_settings = app.state.settings.model_copy(
        update={
            "s3_endpoint_url": "https://s3.example.com",
            "s3_bucket": "evidence",
            "s3_access_key_id": "key",
            "s3_secret_access_key": "secret",
        }
    )
    with app.state.session_factory() as session:
        steps = compute_onboarding_steps(session, configured_settings)
        assert _step(steps, "evidence_source").is_complete is True


def test_operating_period_step_true_only_for_an_active_period(app):
    with app.state.session_factory() as session:
        session.add(
            ControlPeriod(
                name="Planned period",
                starts_on=datetime.date(2026, 1, 1),
                ends_on=datetime.date(2026, 12, 31),
                status="planned",
                created_by="tester@example.com",
            )
        )
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "operating_period").is_complete is False

        session.execute(ControlPeriod.__table__.update().values(status="active"))
        session.commit()

        steps = compute_onboarding_steps(session, app.state.settings)
        assert _step(steps, "operating_period").is_complete is True
