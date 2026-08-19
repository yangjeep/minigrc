"""Unit tests for app/oidc_role_mapping.py (issue #18) — claim/group-to
-role mapping for the generic OIDC login path.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import AuditEvent, OidcRoleMapping, User
from app.oidc_role_mapping import (
    _MAX_CLAIM_VALUES,
    LEAST_PRIVILEGED_ROLE,
    apply_role_mapping,
    compute_mapped_role,
    extract_claim_values,
    load_role_mappings,
)


def test_extract_claim_values_from_a_list_claim():
    assert extract_claim_values({"groups": ["a", "b"]}, "groups") == ["a", "b"]


def test_extract_claim_values_from_a_string_claim_is_a_single_value():
    """Never guesses a delimiter — a comma/space-separated string claim
    stays exactly one value, not split into several."""
    assert extract_claim_values({"groups": "security-admins, finance"}, "groups") == [
        "security-admins, finance"
    ]


def test_extract_claim_values_missing_claim_is_empty():
    assert extract_claim_values({}, "groups") == []


def test_extract_claim_values_coerces_non_string_items():
    assert extract_claim_values({"groups": ["a", 1, True]}, "groups") == ["a", "1", "True"]


def test_extract_claim_values_caps_oversized_list():
    huge = [f"group-{i}" for i in range(_MAX_CLAIM_VALUES + 500)]
    result = extract_claim_values({"groups": huge}, "groups")
    assert len(result) == _MAX_CLAIM_VALUES


def test_extract_claim_values_handles_a_dict_shaped_claim_without_crashing():
    """A malformed/unexpected claim shape (e.g. a nested object instead
    of the normal list/string) never raises — it just becomes one
    opaque value that won't match any real mapping."""
    values = extract_claim_values({"groups": {"unexpected": "shape"}}, "groups")
    assert values == ["{'unexpected': 'shape'}"]
    assert compute_mapped_role(values, {"finance": "auditor"}) == LEAST_PRIVILEGED_ROLE


def test_compute_mapped_role_single_match():
    assert compute_mapped_role(["finance"], {"finance": "auditor"}) == "auditor"


def test_compute_mapped_role_highest_precedence_wins():
    mapping = {"eng": "operator", "sec-admins": "admin"}
    assert compute_mapped_role(["eng", "sec-admins"], mapping) == "admin"


def test_compute_mapped_role_no_match_is_least_privileged():
    assert compute_mapped_role(["unmapped-group"], {"finance": "auditor"}) == LEAST_PRIVILEGED_ROLE


def test_compute_mapped_role_empty_mapping_is_least_privileged():
    assert compute_mapped_role(["anything"], {}) == LEAST_PRIVILEGED_ROLE


def test_load_role_mappings(app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        session.add(OidcRoleMapping(claim_value="eng", role="operator", updated_by="admin@example.com"))
        session.commit()

        assert load_role_mappings(session) == {"finance": "auditor", "eng": "operator"}


def test_apply_role_mapping_no_op_when_unconfigured(app):
    with app.state.session_factory() as session:
        user = User(email="a@example.com", password_hash="", role="operator", role_source="oidc_mapped")
        session.add(user)
        session.commit()

        apply_role_mapping(session, user, {"groups": ["finance"]}, role_claim_name="groups", actor="system")
        session.commit()

        assert user.role == "operator"
        assert user.role_source == "oidc_mapped"
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oidc_role_mapping_applied")
        ).all()
        assert events == []


def test_apply_role_mapping_no_op_when_role_source_is_local(app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        user = User(email="a@example.com", password_hash="", role="admin", role_source="local")
        session.add(user)
        session.commit()

        apply_role_mapping(session, user, {"groups": ["finance"]}, role_claim_name="groups", actor="system")
        session.commit()

        assert user.role == "admin"
        assert user.role_source == "local"


def test_apply_role_mapping_promotes_and_audits(app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="sec-admins", role="admin", updated_by="admin@example.com"))
        user = User(email="a@example.com", password_hash="", role="operator", role_source="oidc_mapped")
        session.add(user)
        session.commit()

        apply_role_mapping(
            session, user, {"groups": ["sec-admins"]}, role_claim_name="groups", actor="system"
        )
        session.commit()

        assert user.role == "admin"
        assert user.role_source == "oidc_mapped"
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oidc_role_mapping_applied")
        ).all()
        assert len(events) == 1
        assert "operator" in events[0].detail
        assert "admin" in events[0].detail


def test_apply_role_mapping_demotes_to_least_privileged_on_unmapped_group(app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="sec-admins", role="admin", updated_by="admin@example.com"))
        user = User(email="a@example.com", password_hash="", role="admin", role_source="oidc_mapped")
        session.add(user)
        session.commit()

        apply_role_mapping(
            session, user, {"groups": ["some-other-group"]}, role_claim_name="groups", actor="system"
        )
        session.commit()

        assert user.role == LEAST_PRIVILEGED_ROLE


def test_apply_role_mapping_is_idempotent_no_duplicate_audit(app):
    with app.state.session_factory() as session:
        session.add(OidcRoleMapping(claim_value="finance", role="auditor", updated_by="admin@example.com"))
        user = User(email="a@example.com", password_hash="", role="operator", role_source="oidc_mapped")
        session.add(user)
        session.commit()

        apply_role_mapping(session, user, {"groups": ["finance"]}, role_claim_name="groups", actor="system")
        apply_role_mapping(session, user, {"groups": ["finance"]}, role_claim_name="groups", actor="system")
        session.commit()

        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "oidc_role_mapping_applied")
        ).all()
        assert len(events) == 1
