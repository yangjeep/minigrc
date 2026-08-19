"""Unit tests for app/connectors/result.py (issue #24)."""

from __future__ import annotations

import datetime

import pytest

from app.connectors.manifest import ConnectorContractError, ConnectorManifest
from app.connectors.result import ConnectorProvenance, ConnectorResult, validate_result

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _manifest(**overrides) -> ConnectorManifest:
    defaults = dict(
        connector_id="test_connector",
        display_name="Test Connector",
        publisher="minigrc",
        version="1.0.0",
        capabilities=("configuration.snapshot",),
    )
    defaults.update(overrides)
    return ConnectorManifest(**defaults)


def _result(**overrides) -> ConnectorResult:
    defaults = dict(
        capability="configuration.snapshot",
        check_key="check1",
        status="pass",
        title="Title",
        summary="Summary",
        provenance=ConnectorProvenance(source_connection_id="conn-1", collected_at=NOW),
        normalized_payload={},
    )
    defaults.update(overrides)
    return ConnectorResult(**defaults)


def test_valid_result_passes():
    validate_result(_result(), _manifest())


def test_capability_overclaiming_rejected():
    manifest = _manifest(capabilities=("identity.population",))
    with pytest.raises(ConnectorContractError, match="undeclared"):
        validate_result(_result(capability="configuration.snapshot"), manifest)


def test_invalid_status_rejected():
    with pytest.raises(ConnectorContractError, match="invalid result status"):
        validate_result(_result(status="definitely_compliant"), _manifest())


def test_oversized_payload_rejected():
    huge_payload = {"data": "x" * 30000}
    with pytest.raises(ConnectorContractError, match="exceeds"):
        validate_result(_result(normalized_payload=huge_payload), _manifest())


def test_secret_shaped_key_rejected():
    with pytest.raises(ConnectorContractError, match="secret-shaped"):
        validate_result(_result(normalized_payload={"access_key": "AKIA..."}), _manifest())


def test_nested_secret_shaped_key_rejected():
    with pytest.raises(ConnectorContractError, match="secret-shaped"):
        validate_result(_result(normalized_payload={"details": {"refresh_token": "abc"}}), _manifest())


def test_benign_payload_keys_accepted():
    validate_result(_result(normalized_payload={"trail_count": 3, "region": "us-east-1"}), _manifest())


def test_password_policy_present_is_not_secret_shaped():
    """Regression: an earlier substring-based scan flagged the real,
    already-shipped AWS field name `password_policy_present` (whether a
    password policy exists, not a password value) as secret-shaped."""
    validate_result(
        _result(normalized_payload={"password_policy_present": True, "mfa_enabled_count": 4}), _manifest()
    )
