"""Route-level tests for issue #33's readiness-stage banner and
top-priorities slice on the dashboard. Domain-level tests live in
tests/test_readiness_stage.py.
"""

from __future__ import annotations

from app.compliance_scope import define_or_revise_scope
from app.finding_lifecycle import open_finding


def test_fresh_deployment_shows_scope_incomplete_stage_and_full_onboarding_panel(logged_in_client):
    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Readiness stage: Scope incomplete." in response.text
    assert "Your compliance program isn't fully set up yet" in response.text


def test_stage_banner_reflects_real_state_not_a_stored_flag(logged_in_client, app):
    """Defining scope changes the rendered stage on the very next request
    with no other action — proving it's recomputed live, not cached."""
    before = logged_in_client.get("/")
    assert "Scope incomplete" in before.text

    with app.state.session_factory() as session:
        define_or_revise_scope(session, service_description="x")
        session.commit()

    after = logged_in_client.get("/")
    assert "Scope incomplete" not in after.text
    assert "Foundation incomplete" in after.text


def test_top_priorities_caps_at_five_with_remaining_items_still_present(logged_in_client, app):
    # Each unowned open finding produces two readiness items
    # (finding_needs_attention + finding_missing_owner) — 8 findings is
    # comfortably more than the 5-item top-priority cap either way.
    with app.state.session_factory() as session:
        for i in range(8):
            open_finding(session, title=f"Finding {i}", severity="low")
        session.commit()

    response = logged_in_client.get("/")
    assert response.status_code == 200
    assert "Top priorities" in response.text
    assert "11 more item(s)" in response.text
