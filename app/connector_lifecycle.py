"""Connector installation, configuration, secrets, health, and execution
lifecycle (issue #25), built on #24's manifest/capability/result
contract (`app.connectors.manifest`/`app.connectors.result`/
`app.connectors.ingestion`).

Backend service layer only — no HTTP route/UI in this slice, matching
#24's own accepted scope (see design doc §1). `run_connector_instance`
is the sole authorized execution/ingestion boundary: a connector result
is validated against its manifest and dispatched to the matching
`app.connectors.ingestion` function only after that validation passes.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.connectors.ingestion import ingest_configuration_snapshot, ingest_identity_population
from app.connectors.manifest import (
    ConnectorContractError,
    ConnectorManifest,
    check_compatibility,
    validate_manifest,
)
from app.connectors.result import ConnectorResult, validate_result
from app.crypto import DecryptionError, EncryptionNotConfiguredError
from app.models import ConnectorExecution, ConnectorInstance, Secret, User, new_id
from app.secrets import SecretNotResolvableError, create_encrypted_secret, resolve_secret

# Dispatch by capability — the only path from a validated ConnectorResult
# into domain state. file.capture is deliberately absent: it needs file
# bytes, not a JSON ConnectorResult, so it is invoked directly via
# app.connectors.ingestion.ingest_file_capture, not through this table.
_INGESTION_DISPATCH: dict[str, Callable] = {
    "configuration.snapshot": ingest_configuration_snapshot,
    "evidence.collect": ingest_configuration_snapshot,
    "identity.population": ingest_identity_population,
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


class ConnectorLifecycleError(ValueError):
    """A safe-to-display reason an install/configure/test/execute
    action could not proceed."""


def _validate_config_against_schema(
    manifest: ConnectorManifest,
    config: dict,
    secret_values: dict,
    *,
    existing_secret_fields: frozenset[str] = frozenset(),
) -> None:
    """`existing_secret_fields` is only ever non-empty on an update: a
    blank secret value there means "keep the existing one" (the same
    convention every other secret-holding settings form in this app
    uses), not "still missing"."""
    for field_spec in manifest.config_schema:
        if field_spec.secret:
            has_value = bool(secret_values.get(field_spec.name)) or field_spec.name in existing_secret_fields
            if field_spec.required and not has_value:
                raise ConnectorLifecycleError(f"missing required secret config field {field_spec.name!r}")
        elif field_spec.required and not config.get(field_spec.name):
            raise ConnectorLifecycleError(f"missing required config field {field_spec.name!r}")


def _store_secret_refs(
    session: Session, manifest: ConnectorManifest, secret_values: dict, *, actor: User, encryption_key: str
) -> dict[str, str]:
    secret_ref: dict[str, str] = {}
    for field_spec in manifest.config_schema:
        if field_spec.secret and secret_values.get(field_spec.name):
            secret = create_encrypted_secret(
                session,
                name=f"connector:{manifest.connector_id}:{field_spec.name}:{new_id()}",
                plaintext=secret_values[field_spec.name],
                actor=actor.email,
                key=encryption_key,
            )
            secret_ref[field_spec.name] = secret.id
    return secret_ref


def install_connector_instance(
    session: Session,
    manifest: ConnectorManifest,
    *,
    display_name: str,
    config: dict,
    secret_values: dict,
    actor: User,
    encryption_key: str,
) -> ConnectorInstance:
    """Install a new configured instance of `manifest`. Validates the
    manifest itself, manifest/core-contract compatibility, and
    `config`/`secret_values` against `manifest.config_schema` before
    creating anything."""
    validate_manifest(manifest)
    check_compatibility(manifest)
    _validate_config_against_schema(manifest, config, secret_values)

    secret_ref = _store_secret_refs(
        session, manifest, secret_values, actor=actor, encryption_key=encryption_key
    )

    instance = ConnectorInstance(
        connector_id=manifest.connector_id,
        manifest_version=manifest.version,
        display_name=display_name,
        enabled=False,
        config_json=json.dumps(config, sort_keys=True),
        secret_ref_json=json.dumps(secret_ref, sort_keys=True),
        created_by_user_id=actor.id,
    )
    session.add(instance)
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance.id,
        action="install",
        detail=f"Installed connector '{manifest.connector_id}' as '{display_name}'",
        actor=actor.email,
    )
    return instance


def update_connector_instance_config(
    session: Session,
    instance: ConnectorInstance,
    manifest: ConnectorManifest,
    *,
    config: dict,
    secret_values: dict,
    actor: User,
    encryption_key: str,
) -> ConnectorInstance:
    """Update an installed instance's configuration in place. A blank/
    omitted secret value for a field leaves that field's existing
    stored secret untouched (same "blank keeps existing" convention
    used by every other secret-holding settings form in this app)."""
    existing_secret_ref = json.loads(instance.secret_ref_json)
    _validate_config_against_schema(
        manifest, config, secret_values, existing_secret_fields=frozenset(existing_secret_ref)
    )

    new_secret_ref = _store_secret_refs(
        session, manifest, secret_values, actor=actor, encryption_key=encryption_key
    )
    existing_secret_ref.update(new_secret_ref)

    instance.config_json = json.dumps(config, sort_keys=True)
    instance.secret_ref_json = json.dumps(existing_secret_ref, sort_keys=True)
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance.id,
        action="update_config",
        detail=f"Updated configuration for connector instance '{instance.display_name}'",
        actor=actor.email,
    )
    return instance


def set_connector_instance_enabled(
    session: Session, instance: ConnectorInstance, enabled: bool, *, actor: User
) -> None:
    instance.enabled = enabled
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance.id,
        action="enable" if enabled else "disable",
        detail=f"{'Enabled' if enabled else 'Disabled'} connector instance '{instance.display_name}'",
        actor=actor.email,
    )


def remove_connector_instance(session: Session, instance: ConnectorInstance, *, actor: User) -> None:
    """Delete the instance row and its own execution/idempotency
    ledger (`ConnectorExecution` has a real FK to this table, since it
    is operational history, not compliance evidence — RULES.md §1's
    "connector runtime/configuration state is separate from compliance
    truth" applies to it directly). Every *ingestion* table's
    `source_connection_id` is a plain string, not a foreign key to this
    table (see design doc §1), so historical evidence already captured
    is never touched by this."""
    detail = f"Removed connector instance '{instance.display_name}' ({instance.connector_id})"
    instance_id = instance.id
    for execution in session.scalars(
        select(ConnectorExecution).where(ConnectorExecution.connector_instance_id == instance_id)
    ).all():
        session.delete(execution)
    session.delete(instance)
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance_id,
        action="remove",
        detail=detail,
        actor=actor.email,
    )


def resolve_connector_config(session: Session, instance: ConnectorInstance, *, encryption_key: str) -> dict:
    """Merge non-secret config with just-in-time-resolved secret
    values, for a connector callable's own use only — never return
    this to a client/UI/log."""
    config = dict(json.loads(instance.config_json))
    secret_ref = json.loads(instance.secret_ref_json)
    for field_name, secret_id in secret_ref.items():
        secret = session.get(Secret, secret_id)
        if secret is None:
            continue
        try:
            config[field_name] = resolve_secret(secret, key=encryption_key)
        except (SecretNotResolvableError, EncryptionNotConfiguredError, DecryptionError):
            config[field_name] = None
    return config


def test_connector_instance(
    session: Session,
    instance: ConnectorInstance,
    *,
    test_callable: Callable[[dict], None],
    encryption_key: str,
    actor: User,
) -> bool:
    """Invoke `test_callable(config)` (raises on failure, returns
    normally on success) and record the outcome. Never raises past this
    function — a broken connector/config must never crash the caller."""
    config = resolve_connector_config(session, instance, encryption_key=encryption_key)
    instance.last_test_at = _now()
    try:
        test_callable(config)
        instance.last_test_status = "success"
        instance.last_test_error = ""
        success = True
    except Exception as exc:  # noqa: BLE001 - a connector's test hook can raise anything
        instance.last_test_status = "failure"
        instance.last_test_error = str(exc)[:2000]
        success = False
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance.id,
        action="test_connection",
        detail=f"Connection test {'succeeded' if success else 'failed'} for '{instance.display_name}'",
        actor=actor.email,
    )
    return success


def run_connector_instance(
    session: Session,
    instance: ConnectorInstance,
    manifest: ConnectorManifest,
    connector_callable: Callable[[dict], ConnectorResult],
    *,
    idempotency_key: str,
    encryption_key: str,
    triggered_by: str = "manual",
    actor_id: str | None = None,
) -> ConnectorExecution:
    """The execution/ingestion boundary: invoke `connector_callable`,
    validate its result against `manifest`, and dispatch to the
    matching `app.connectors.ingestion` function — the only way a
    connector's output can become domain state.

    A repeated `idempotency_key` for the same instance returns the
    existing execution unchanged, without re-invoking the connector or
    re-ingesting a fact. A connector failure or a contract violation
    never partially mutates compliance state: this function only ever
    writes an ingestion row *before* writing a success execution row,
    and never touches the session again after catching a failure — a
    failure that occurs *while* writing the outcome propagates to the
    caller, whose own transaction rollback (the same convention every
    other domain function in this codebase relies on) removes any
    partial writes.
    """
    if not instance.enabled:
        raise ConnectorLifecycleError(f"connector instance '{instance.display_name}' is disabled")

    existing = session.scalar(
        select(ConnectorExecution).where(
            ConnectorExecution.connector_instance_id == instance.id,
            ConnectorExecution.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    started_at = _now()
    config = resolve_connector_config(session, instance, encryption_key=encryption_key)

    try:
        result = connector_callable(config)
        validate_result(result, manifest)
        ingest = _INGESTION_DISPATCH.get(result.capability)
        if ingest is None:
            raise ConnectorLifecycleError(
                f"no ingestion path registered for capability {result.capability!r}"
            )
        ingest(session, result, manifest)
    except Exception as exc:  # noqa: BLE001 - any connector/contract failure lands here
        # Roll back anything an ingestion function may have already
        # written before it failed (e.g. session.add(snapshot) followed
        # by a later error) — without this, the caller's eventual
        # commit would persist that partial write alongside the
        # failure execution row below. No prior commit happened in
        # this function, so this rollback is safe (see #55/#57's
        # separate, unrelated nested-rollback-after-a-commit residual,
        # which does not apply here).
        session.rollback()
        execution = ConnectorExecution(
            connector_instance_id=instance.id,
            idempotency_key=idempotency_key,
            # "error" = the request/result itself was invalid (a contract
            # violation this app or the connector caused); "failure" = the
            # provider/connector genuinely failed. Both are recorded, but
            # kept distinct for readability/triage.
            status="error"
            if isinstance(exc, (ConnectorLifecycleError, ConnectorContractError))
            else "failure",
            error_summary=str(exc)[:2000],
            triggered_by=triggered_by,
            actor_id=actor_id,
            started_at=started_at,
            finished_at=_now(),
        )
        session.add(execution)
        instance.last_failure_at = execution.finished_at
        instance.last_error_summary = execution.error_summary
        session.flush()
        return execution

    execution = ConnectorExecution(
        connector_instance_id=instance.id,
        idempotency_key=idempotency_key,
        status="success",
        result_summary=f"{result.capability}: {result.title}"[:2000],
        triggered_by=triggered_by,
        actor_id=actor_id,
        started_at=started_at,
        finished_at=_now(),
    )
    session.add(execution)
    instance.last_success_at = execution.finished_at
    session.flush()
    return execution


def _semver_tuple(version: str) -> tuple[int, int, int]:
    """`version` must already be validate_manifest-shaped (`X.Y.Z`) by
    the time it reaches here — both ConnectorManifest.version and
    ConnectorInstance.manifest_version are only ever set from a
    validated manifest."""
    major, minor, patch = version.split(".")
    return (int(major), int(minor), int(patch))


@dataclass(frozen=True)
class ConnectorUpgradePlan:
    """A no-mutation preview of moving `instance` from `from_manifest`
    to `to_manifest` (issue #27) — mirrors the existing
    plan-then-apply shape `test_connector_instance` already uses for a
    safe preview step. `direction` is purely informational for
    "upgrade"/"same"; `apply_connector_instance_upgrade` is what
    actually enforces the no-silent-downgrade rule."""

    connector_id: str
    from_version: str
    to_version: str
    direction: str  # "upgrade" | "downgrade" | "same"
    new_capabilities: tuple[str, ...]
    new_required_permissions: tuple[str, ...]
    # Full (not just new) capabilities/permissions of the reviewed
    # to_manifest — apply_connector_instance_upgrade verifies whatever
    # to_manifest it's given still matches these exactly, so a manifest
    # with the same version string but different capabilities can't be
    # substituted at apply time (see apply's docstring).
    to_capabilities: tuple[str, ...]
    to_required_permissions: tuple[str, ...]


def plan_connector_instance_upgrade(
    instance: ConnectorInstance, from_manifest: ConnectorManifest, to_manifest: ConnectorManifest
) -> ConnectorUpgradePlan:
    """Pure, no I/O. Always raises for identity confusion or core-
    contract incompatibility — those are never valid, with no override.
    Never raises for a downgrade: that is a valid plan outcome the
    caller must explicitly acknowledge via
    `apply_connector_instance_upgrade(..., allow_downgrade=True)`."""
    if to_manifest.connector_id != instance.connector_id:
        raise ConnectorLifecycleError(
            f"cannot upgrade connector instance for {instance.connector_id!r} to a manifest for "
            f"{to_manifest.connector_id!r} — identity confusion"
        )
    validate_manifest(to_manifest)
    check_compatibility(to_manifest)

    from_version = _semver_tuple(from_manifest.version)
    to_version = _semver_tuple(to_manifest.version)
    if to_version > from_version:
        direction = "upgrade"
    elif to_version < from_version:
        direction = "downgrade"
    else:
        direction = "same"

    new_capabilities = tuple(c for c in to_manifest.capabilities if c not in from_manifest.capabilities)
    new_required_permissions = tuple(
        p for p in to_manifest.required_permissions if p not in from_manifest.required_permissions
    )
    return ConnectorUpgradePlan(
        connector_id=instance.connector_id,
        from_version=from_manifest.version,
        to_version=to_manifest.version,
        direction=direction,
        new_capabilities=new_capabilities,
        new_required_permissions=new_required_permissions,
        to_capabilities=to_manifest.capabilities,
        to_required_permissions=to_manifest.required_permissions,
    )


def apply_connector_instance_upgrade(
    session: Session,
    instance: ConnectorInstance,
    plan: ConnectorUpgradePlan,
    to_manifest: ConnectorManifest,
    *,
    actor: User,
    allow_downgrade: bool = False,
) -> ConnectorInstance:
    """Mutates `instance.manifest_version` to `to_manifest.version`.
    Raises ConnectorLifecycleError if `plan.direction == "downgrade"`
    and `allow_downgrade` is not explicitly set — a downgrade is never
    silent/automatic. Audits as `"rollback"` when an acknowledged
    downgrade was actually applied, `"upgrade"` otherwise, and records
    the new capabilities/permissions the plan surfaced so the audit
    trail preserves exactly what was shown before the change.

    Also raises if `instance.manifest_version` no longer matches
    `plan.from_version` — the same stale-plan/optimistic-concurrency
    guard `app.registers`'s PATCH API already uses via
    `expected_updated_at`. Without this, a plan computed against an old
    version, applied after someone else already moved the instance
    to a newer version, could silently move it *backward* from that
    newer version while `plan.direction` still reads "upgrade" (it was
    computed against the stale starting point, not the instance's
    current state).

    Also raises if `to_manifest`'s version/capabilities/permissions
    don't exactly match what `plan` reviewed — a caller must not
    substitute a different manifest at apply time than the one
    `plan_connector_instance_upgrade` actually checked compatibility/
    capabilities/permissions against. Matching on version alone would
    not be enough: two manifest objects could share a version string
    while differing in capabilities.
    """
    if instance.manifest_version != plan.from_version:
        raise ConnectorLifecycleError(
            f"connector instance '{instance.display_name}' is at version "
            f"{instance.manifest_version!r}, not the plan's expected {plan.from_version!r} — "
            "reload and re-plan before applying"
        )
    if plan.direction == "downgrade" and not allow_downgrade:
        raise ConnectorLifecycleError(
            f"refusing to silently downgrade connector instance '{instance.display_name}' from "
            f"{plan.from_version} to {plan.to_version} — pass allow_downgrade=True for an explicit rollback"
        )
    if plan.connector_id != instance.connector_id or to_manifest.connector_id != instance.connector_id:
        raise ConnectorLifecycleError("upgrade plan/manifest do not match this connector instance")
    if (
        to_manifest.version != plan.to_version
        or to_manifest.capabilities != plan.to_capabilities
        or to_manifest.required_permissions != plan.to_required_permissions
    ):
        raise ConnectorLifecycleError(
            "to_manifest does not match the plan's reviewed version/capabilities/permissions — "
            "a different manifest must not be substituted at apply time"
        )

    action = "rollback" if plan.direction == "downgrade" else "upgrade"
    instance.manifest_version = to_manifest.version
    session.flush()
    record_audit_event(
        session,
        entity_type="connector_instance",
        entity_id=instance.id,
        action=action,
        detail=(
            f"{action.capitalize()}d connector instance '{instance.display_name}' from "
            f"{plan.from_version} to {plan.to_version}; new capabilities: "
            f"{list(plan.new_capabilities)}; new required permissions: "
            f"{list(plan.new_required_permissions)}"
        ),
        actor=actor.email,
    )
    return instance
