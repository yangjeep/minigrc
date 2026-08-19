"""Unit tests for app/connectors/manifest.py (issue #24)."""

from __future__ import annotations

import pytest

from app.connectors.manifest import (
    CORE_CONTRACT_VERSION,
    ConfigFieldSpec,
    ConnectorContractError,
    ConnectorManifest,
    check_compatibility,
    validate_manifest,
)


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


def test_valid_manifest_passes():
    validate_manifest(_manifest())


def test_empty_connector_id_rejected():
    with pytest.raises(ConnectorContractError, match="connector_id"):
        validate_manifest(_manifest(connector_id=""))


def test_missing_version_rejected():
    with pytest.raises(ConnectorContractError, match="semver"):
        validate_manifest(_manifest(version=""))


def test_malformed_version_rejected():
    with pytest.raises(ConnectorContractError, match="semver"):
        validate_manifest(_manifest(version="v1"))


def test_empty_capabilities_rejected():
    with pytest.raises(ConnectorContractError, match="capability"):
        validate_manifest(_manifest(capabilities=()))


def test_unknown_capability_rejected():
    with pytest.raises(ConnectorContractError, match="unknown capability"):
        validate_manifest(_manifest(capabilities=("soc2.pass_the_audit",)))


def test_duplicate_config_field_name_rejected():
    manifest = _manifest(
        config_schema=(
            ConfigFieldSpec(name="api_key", type="str"),
            ConfigFieldSpec(name="api_key", type="str"),
        )
    )
    with pytest.raises(ConnectorContractError, match="duplicate config field"):
        validate_manifest(manifest)


def test_unsupported_config_field_type_rejected():
    manifest = _manifest(config_schema=(ConfigFieldSpec(name="weird", type="list"),))
    with pytest.raises(ConnectorContractError, match="unsupported type"):
        validate_manifest(manifest)


def test_compatible_version_accepted():
    check_compatibility(_manifest(), core_contract_version=CORE_CONTRACT_VERSION)


def test_below_min_core_version_rejected():
    manifest = _manifest(min_core_contract_version=CORE_CONTRACT_VERSION + 1)
    with pytest.raises(ConnectorContractError, match="requires core contract version"):
        check_compatibility(manifest, core_contract_version=CORE_CONTRACT_VERSION)


def test_above_max_core_version_rejected():
    manifest = _manifest(max_core_contract_version=CORE_CONTRACT_VERSION - 1)
    with pytest.raises(ConnectorContractError, match="requires core contract version"):
        check_compatibility(manifest, core_contract_version=CORE_CONTRACT_VERSION)


def test_no_max_core_version_means_no_upper_bound():
    manifest = _manifest(max_core_contract_version=None)
    check_compatibility(manifest, core_contract_version=CORE_CONTRACT_VERSION + 100)
