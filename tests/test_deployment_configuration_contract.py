"""Verification for issue #48's unified deployment-configuration-contract
doc (docs/deployment/configuration-contract.md) — exercises the actual
`Settings` properties the doc describes, for representative combinations
of each axis, as a living proof that the doc matches current code rather
than a new feature under test. If any of these properties' semantics
change, this test (not just the doc) should fail.
"""

from __future__ import annotations

import pytest

from app.config import Settings, reject_ambiguous_database_config

# --- axis 1: database backend — the one genuine XOR ---------------------


def test_database_backend_neither_set_falls_back_to_local_sqlite():
    settings = Settings(database_path=None, database_url="")
    assert settings.resolved_database_path.endswith("grc.db")
    assert settings.resolved_engine_target == settings.resolved_database_path
    reject_ambiguous_database_config(settings)  # does not raise


def test_database_backend_only_database_url_set_is_valid():
    settings = Settings(database_url="postgresql+psycopg://user:pass@host/db", database_path=None)
    assert settings.resolved_engine_target == settings.database_url
    reject_ambiguous_database_config(settings)  # does not raise


def test_database_backend_only_database_path_set_is_valid():
    settings = Settings(database_path="/data/grc.db", database_url="")
    assert settings.resolved_engine_target == "/data/grc.db"
    reject_ambiguous_database_config(settings)  # does not raise


def test_database_backend_both_set_is_rejected():
    settings = Settings(database_url="postgresql+psycopg://user:pass@host/db", database_path="/data/grc.db")
    with pytest.raises(RuntimeError, match="Ambiguous database configuration"):
        reject_ambiguous_database_config(settings)


# --- axis 2a: evidence storage — all-or-nothing, never a startup crash --


def test_evidence_storage_fully_unset_is_not_configured():
    settings = Settings(s3_endpoint_url="", s3_bucket="", s3_access_key_id="", s3_secret_access_key="")
    assert settings.evidence_repository_configured is False


def test_evidence_storage_fully_set_is_configured():
    settings = Settings(
        s3_endpoint_url="https://s3.example.test",
        s3_bucket="bucket",
        s3_access_key_id="key",
        s3_secret_access_key="secret",
    )
    assert settings.evidence_repository_configured is True


def test_evidence_storage_partially_set_is_not_configured_not_a_crash():
    """The documented pattern: incomplete optional-feature config
    degrades to "disabled," it never raises at Settings-construction
    time — matching the doc's "no new ambiguous combination here"
    conclusion."""
    settings = Settings(
        s3_endpoint_url="https://s3.example.test", s3_bucket="", s3_access_key_id="", s3_secret_access_key=""
    )
    assert settings.evidence_repository_configured is False


# --- axis 2b: auth modes — independently stackable, not mutually exclusive


def test_google_oidc_and_generic_oidc_can_both_be_configured_simultaneously():
    """Auth modes are documented as stackable, not an XOR like the
    database axis — both being fully configured at once is valid."""
    settings = Settings(
        google_oidc_client_id="google-client",
        google_oidc_client_secret="google-secret",
        oidc_issuer="https://idp.example.test",
        oidc_client_id="generic-client",
        oidc_client_secret="generic-secret",
        public_base_url="https://minigrc.example.test",
    )
    assert settings.google_oidc_enabled is True
    assert settings.oidc_enabled is True


def test_partial_google_oidc_config_is_not_enabled():
    settings = Settings(
        google_oidc_client_id="google-client",
        google_oidc_client_secret="",
        public_base_url="https://minigrc.example.test",
    )
    assert settings.google_oidc_enabled is False


def test_no_auth_mode_configured_still_leaves_local_login_available():
    """Local login has no Settings-level gate at all — it is always
    available regardless of every other mode's configuration state."""
    settings = Settings()
    assert settings.google_oidc_enabled is False
    assert settings.oidc_enabled is False
    # No settings field disables local login; its route
    # (app/routers/auth.py) has no configuration precondition.


# --- axis 2c: BYOK/AI — confirmed not yet implemented --------------------


def test_no_byok_ai_settings_fields_exist_yet():
    """Issue #15 has not landed — this test documents that fact so it
    fails loudly (a reminder to update the contract doc) the moment
    BYOK/AI settings fields are actually added."""
    ai_related_fields = [name for name in Settings.model_fields if "ai_" in name or "byok" in name.lower()]
    assert ai_related_fields == []
