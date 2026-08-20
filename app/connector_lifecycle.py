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
