"""The provider-neutral connector manifest contract (issue #24).

A manifest is a plain, versioned Python object a connector module
declares — the same "declarative object, not a DB row" shape this
codebase already uses for `app.registers.config.RegisterConfig`.
Connector *installation* state (issue #25) and a *registry* (issue #26)
don't exist yet, so a DB-backed manifest would be premature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CORE_CONTRACT_VERSION = 1

CAPABILITIES = {
    "file.capture": "Capture a specific file's bytes/metadata as durable evidence.",
    "evidence.collect": "Collect a fact/check result as evidence (generic).",
    "configuration.snapshot": "Snapshot a provider configuration/posture check.",
    "identity.population": "Sync a population of identities (e.g. directory users).",
    "identity.groups": "Sync group/role membership for identities.",
    "asset.inventory": "Enumerate provider-managed assets.",
    "vulnerability.findings": "Report vulnerability/security findings.",
    "notification.send": "Send an outbound notification.",
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_CONFIG_FIELD_TYPES = ("str", "bool", "int")


class ConnectorContractError(ValueError):
    """A manifest or result violates the connector contract."""


@dataclass(frozen=True)
class ConfigFieldSpec:
    """One field a connector instance must be configured with.

    `secret` only *declares* the field as secret-shaped for a future
    runtime (issue #25) to route through an encrypted-storage
    mechanism — this contract never carries a resolved secret value.
    """

    name: str
    type: str  # "str" | "bool" | "int"
    required: bool = True
    secret: bool = False


@dataclass(frozen=True)
class ConnectorManifest:
    connector_id: str
    display_name: str
    publisher: str
    version: str  # semver, e.g. "1.0.0"
    capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...] = ()
    supported_auth_methods: tuple[str, ...] = ()
    config_schema: tuple[ConfigFieldSpec, ...] = field(default_factory=tuple)
    min_core_contract_version: int = CORE_CONTRACT_VERSION
    max_core_contract_version: int | None = None


def validate_manifest(manifest: ConnectorManifest) -> None:
    """Raise ConnectorContractError if `manifest` is malformed.

    Deliberately structural/static checks only — no I/O, no provider
    calls. Capability *overclaiming* against an actual result is
    `validate_result`'s job (needs a result to compare against).
    """
    if not manifest.connector_id:
        raise ConnectorContractError("connector_id is required")
    if not manifest.version or not _SEMVER_RE.match(manifest.version):
        raise ConnectorContractError(f"version must be a semver string (X.Y.Z), got {manifest.version!r}")
    if not manifest.capabilities:
        raise ConnectorContractError("a manifest must declare at least one capability")
    unknown = [c for c in manifest.capabilities if c not in CAPABILITIES]
    if unknown:
        raise ConnectorContractError(f"unknown capability/capabilities: {unknown}")

    seen_field_names: set[str] = set()
    for field_spec in manifest.config_schema:
        if field_spec.name in seen_field_names:
            raise ConnectorContractError(f"duplicate config field name: {field_spec.name!r}")
        seen_field_names.add(field_spec.name)
        if field_spec.type not in _CONFIG_FIELD_TYPES:
            raise ConnectorContractError(
                f"config field {field_spec.name!r} has unsupported type {field_spec.type!r}"
            )


def check_compatibility(
    manifest: ConnectorManifest, core_contract_version: int = CORE_CONTRACT_VERSION
) -> None:
    """Raise ConnectorContractError if `manifest` isn't compatible with
    `core_contract_version` — a pure version-bounds check, no implicit
    "latest wins" fallback."""
    if core_contract_version < manifest.min_core_contract_version:
        raise ConnectorContractError(
            f"connector {manifest.connector_id!r} requires core contract version "
            f">= {manifest.min_core_contract_version}, running {core_contract_version}"
        )
    if (
        manifest.max_core_contract_version is not None
        and core_contract_version > manifest.max_core_contract_version
    ):
        raise ConnectorContractError(
            f"connector {manifest.connector_id!r} requires core contract version "
            f"<= {manifest.max_core_contract_version}, running {core_contract_version}"
        )
