"""Versioned, auditable connector registry (issue #26) — discovery/
filtering over #24-compatible manifests, described as plain JSON so
browsing never has to import/execute any connector code. Install
handoff goes exclusively through #25's `install_connector_instance`.

`registry/registry.json` is the shipped, git-version-controlled
registry — every change to it is a normal, reviewable commit, which is
the audit trail this issue asks for (no new database table).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connector_lifecycle import install_connector_instance
from app.connectors.manifest import (
    CAPABILITIES,
    CORE_CONTRACT_VERSION,
    ConfigFieldSpec,
    ConnectorContractError,
    ConnectorManifest,
    check_compatibility,
)
from app.models import ConnectorInstance, User

VERIFICATION_STATUSES = ("unofficial", "community", "official")

_CHECKSUM_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# A narrow, documented heuristic — not an exhaustive risk classifier.
# Flags the permission-scope shapes this registry can recognize as
# broad/high-risk; a differently-named but equally dangerous
# permission would not be caught by this alone (see design doc §7).
_BROAD_PERMISSION_MARKERS = ("admin", "root", "owner", "full_access", "generatecredentialreport")


class RegistryError(ValueError):
    """A registry file or entry is malformed/tampered/duplicate."""


@dataclass(frozen=True)
class RegistryEntry:
    connector_id: str
    display_name: str
    publisher: str
    version: str
    capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...]
    supported_auth_methods: tuple[str, ...]
    config_schema: tuple[ConfigFieldSpec, ...]
    min_core_contract_version: int
    max_core_contract_version: int | None
    verification_status: str
    package_location: str
    checksum_sha256: str | None
    deprecated: bool


def _entry_from_dict(raw: dict) -> RegistryEntry:
    try:
        verification_status = raw["verification_status"]
        if verification_status not in VERIFICATION_STATUSES:
            raise RegistryError(
                f"invalid verification_status {verification_status!r}, must be one of {VERIFICATION_STATUSES}"
            )
        checksum_sha256 = raw.get("checksum_sha256")
        if checksum_sha256 is not None:
            checksum_sha256 = checksum_sha256.lower()
            if not _CHECKSUM_SHA256_RE.match(checksum_sha256):
                raise RegistryError(
                    f"checksum_sha256 for {raw.get('connector_id')!r} must be a 64-char hex sha256 "
                    f"digest, got {raw['checksum_sha256']!r}"
                )
        config_schema = tuple(
            ConfigFieldSpec(
                name=field["name"],
                type=field["type"],
                required=field.get("required", True),
                secret=field.get("secret", False),
            )
            for field in raw.get("config_schema", [])
        )
        return RegistryEntry(
            connector_id=raw["connector_id"],
            display_name=raw["display_name"],
            publisher=raw["publisher"],
            version=raw["version"],
            capabilities=tuple(raw["capabilities"]),
            required_permissions=tuple(raw.get("required_permissions", [])),
            supported_auth_methods=tuple(raw.get("supported_auth_methods", [])),
            config_schema=config_schema,
            min_core_contract_version=raw.get("min_core_contract_version", CORE_CONTRACT_VERSION),
            max_core_contract_version=raw.get("max_core_contract_version"),
            verification_status=verification_status,
            package_location=raw["package_location"],
            checksum_sha256=checksum_sha256,
            deprecated=raw.get("deprecated", False),
        )
    except KeyError as exc:
        raise RegistryError(f"registry entry is missing required field {exc.args[0]!r}") from exc


def load_registry(path: str) -> list[RegistryEntry]:
    """Parse and validate a registry file. Never imports/executes
    anything a `package_location` field points at — pure data parsing.
    Raises `RegistryError` on a missing file, malformed JSON, an
    invalid entry, or a duplicate `(connector_id, version)` pair.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as exc:
        raise RegistryError(f"registry file not found: {path!r}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry file is not valid JSON: {exc}") from exc

    entries = [_entry_from_dict(item) for item in raw.get("entries", [])]

    seen: set[tuple[str, str]] = set()
    publisher_by_connector_id: dict[str, str] = {}
    for entry in entries:
        key = (entry.connector_id, entry.version)
        if key in seen:
            raise RegistryError(f"duplicate registry entry for {key!r}")
        seen.add(key)

        known_publisher = publisher_by_connector_id.get(entry.connector_id)
        if known_publisher is None:
            publisher_by_connector_id[entry.connector_id] = entry.publisher
        elif known_publisher != entry.publisher:
            raise RegistryError(
                f"connector_id {entry.connector_id!r} is claimed by more than one publisher "
                f"({known_publisher!r} and {entry.publisher!r}) — possible identity confusion"
            )

    return entries


def to_manifest(entry: RegistryEntry) -> ConnectorManifest:
    """Build a real `ConnectorManifest` from a registry entry — no
    import/execution of `entry.package_location` at all."""
    return ConnectorManifest(
        connector_id=entry.connector_id,
        display_name=entry.display_name,
        publisher=entry.publisher,
        version=entry.version,
        capabilities=entry.capabilities,
        required_permissions=entry.required_permissions,
        supported_auth_methods=entry.supported_auth_methods,
        config_schema=entry.config_schema,
        min_core_contract_version=entry.min_core_contract_version,
        max_core_contract_version=entry.max_core_contract_version,
    )


def filter_by_capability(entries: list[RegistryEntry], capability: str) -> list[RegistryEntry]:
    return [entry for entry in entries if capability in entry.capabilities and not entry.deprecated]


def filter_compatible(
    entries: list[RegistryEntry], core_contract_version: int = CORE_CONTRACT_VERSION
) -> list[RegistryEntry]:
    """Fails closed by default: an entry outside the running core
    contract version is excluded, with no override in this slice."""
    compatible = []
    for entry in entries:
        try:
            check_compatibility(to_manifest(entry), core_contract_version=core_contract_version)
        except ConnectorContractError:
            continue
        compatible.append(entry)
    return compatible


def install_from_registry(
    session: Session,
    entry: RegistryEntry,
    *,
    display_name: str,
    config: dict,
    secret_values: dict,
    actor: User,
    encryption_key: str,
) -> ConnectorInstance:
    """The only path from a registry entry into an installed instance —
    converts to a manifest, then calls #25's `install_connector_instance`
    exclusively, reusing its lifecycle/security boundaries rather than
    bypassing them."""
    manifest = to_manifest(entry)
    return install_connector_instance(
        session,
        manifest,
        display_name=display_name,
        config=config,
        secret_values=secret_values,
        actor=actor,
        encryption_key=encryption_key,
    )


def _flag_broad_permissions(required_permissions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        permission
        for permission in required_permissions
        if any(marker in permission.lower() for marker in _BROAD_PERMISSION_MARKERS)
    )


@dataclass(frozen=True)
class PermissionReview:
    """A reviewable summary of what installing `entry` would grant —
    issue #27's "operators can see capabilities/permissions before
    activation." Data only; rendering this before an install/upgrade
    call is left to whatever surface consumes it (see design doc §5)."""

    connector_id: str
    display_name: str
    verification_status: str
    deprecated: bool
    capabilities: tuple[tuple[str, str], ...]
    required_permissions: tuple[str, ...]
    broad_permission_warnings: tuple[str, ...]
    supported_auth_methods: tuple[str, ...]


def build_permission_review(entry: RegistryEntry) -> PermissionReview:
    """Never imports/executes `entry.package_location` — pure data
    transform over the entry's own declared fields, same discipline as
    `to_manifest`."""
    return PermissionReview(
        connector_id=entry.connector_id,
        display_name=entry.display_name,
        verification_status=entry.verification_status,
        deprecated=entry.deprecated,
        capabilities=tuple((cap, CAPABILITIES.get(cap, "")) for cap in entry.capabilities),
        required_permissions=entry.required_permissions,
        broad_permission_warnings=_flag_broad_permissions(entry.required_permissions),
        supported_auth_methods=entry.supported_auth_methods,
    )


def list_deprecated_installed_instances(
    session: Session, entries: list[RegistryEntry]
) -> list[ConnectorInstance]:
    """Installed instances whose (connector_id, manifest_version) now
    matches a `deprecated=True` registry entry. Reporting only — never
    disables/removes an instance; an operator decides whether/when to
    act, the same way #25's health fields are surfaced without an
    automatic disable."""
    deprecated_keys = {(entry.connector_id, entry.version) for entry in entries if entry.deprecated}
    if not deprecated_keys:
        return []
    instances = session.scalars(select(ConnectorInstance)).all()
    return [
        instance
        for instance in instances
        if (instance.connector_id, instance.manifest_version) in deprecated_keys
    ]
