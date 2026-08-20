"""Integration tests for app/connector_lifecycle.py (issue #25) — proves
install -> configure -> test -> execute -> ingest end to end with a
deterministic fake reference connector (test-only; no product value,
so it lives here rather than under app/, per RULES.md's "no speculative
infrastructure" — see design doc §8).
"""

from __future__ import annotations

import datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.connector_lifecycle import (
    ConnectorLifecycleError,
    apply_connector_instance_upgrade,
    install_connector_instance,
    plan_connector_instance_upgrade,
    remove_connector_instance,
    resolve_connector_config,
    run_connector_instance,
    set_connector_instance_enabled,
    update_connector_instance_config,
)
from app.connector_lifecycle import test_connector_instance as check_connector_instance
from app.connectors.manifest import ConfigFieldSpec, ConnectorContractError, ConnectorManifest
from app.connectors.result import ConnectorProvenance, ConnectorResult
from app.models import AuditEvent, ConnectorExecution, ConnectorInstance, EvidenceSnapshot, User
from app.security import hash_password

TEST_KEY = Fernet.generate_key().decode()
NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _fake_manifest(**overrides) -> ConnectorManifest:
    defaults = dict(
        connector_id="fake_reference",
        display_name="Fake Reference Connector",
        publisher="minigrc-tests",
        version="1.0.0",
        capabilities=("configuration.snapshot",),
        config_schema=(
            ConfigFieldSpec(name="endpoint", type="str"),
            ConfigFieldSpec(name="api_token", type="str", secret=True),
        ),
    )
    defaults.update(overrides)
    return ConnectorManifest(**defaults)


def _fake_connector_callable(should_fail: bool = False, capability: str = "configuration.snapshot"):
    def _call(config: dict) -> ConnectorResult:
        if should_fail:
            raise RuntimeError("fake provider is unreachable")
        assert config["endpoint"] == "https://fake.example.test"
        assert config["api_token"] == "s3cr3t-token-value"
        return ConnectorResult(
            capability=capability,
            check_key="fake_check",
            status="pass",
            title="Fake check passed",
            summary="Everything looks fine",
            provenance=ConnectorProvenance(source_connection_id="fake-conn-1", collected_at=NOW),
            normalized_payload={"checked": True},
        )

    return _call


@pytest.fixture
def actor(app):
    with app.state.session_factory() as session:
        user = User(email="connector-admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_install_validates_manifest_and_stores_secret_without_plaintext(app, actor):
    with app.state.session_factory() as session:
        instance = install_connector_instance(
            session,
            _fake_manifest(),
            display_name="My Fake Connector",
            config={"endpoint": "https://fake.example.test"},
            secret_values={"api_token": "s3cr3t-token-value"},
            actor=actor,
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert instance.enabled is False
        assert "s3cr3t-token-value" not in instance.config_json
        assert "s3cr3t-token-value" not in instance.secret_ref_json

        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "install", AuditEvent.entity_id == instance.id)
        ).all()
        assert len(events) == 1
        assert "s3cr3t-token-value" not in events[0].detail


def test_install_rejects_missing_required_config_field(app, actor):
    with app.state.session_factory() as session:
        with pytest.raises(ConnectorLifecycleError, match="endpoint"):
            install_connector_instance(
                session,
                _fake_manifest(),
                display_name="Missing config",
                config={},
                secret_values={"api_token": "x"},
                actor=actor,
                encryption_key=TEST_KEY,
            )


def test_install_rejects_missing_required_secret_field(app, actor):
    with app.state.session_factory() as session:
        with pytest.raises(ConnectorLifecycleError, match="api_token"):
            install_connector_instance(
                session,
                _fake_manifest(),
                display_name="Missing secret",
                config={"endpoint": "https://fake.example.test"},
                secret_values={},
                actor=actor,
                encryption_key=TEST_KEY,
            )


def test_install_rejects_incompatible_manifest_before_creating_anything(app, actor):
    manifest = _fake_manifest(min_core_contract_version=999)
    with app.state.session_factory() as session:
        with pytest.raises(ConnectorContractError):
            install_connector_instance(
                session,
                manifest,
                display_name="Incompatible",
                config={"endpoint": "https://fake.example.test"},
                secret_values={"api_token": "x"},
                actor=actor,
                encryption_key=TEST_KEY,
            )
        assert session.scalar(select(ConnectorInstance)) is None


def _install(session, actor, **config_overrides):
    config = {"endpoint": "https://fake.example.test"}
    config.update(config_overrides)
    return install_connector_instance(
        session,
        _fake_manifest(),
        display_name="Fake Connector",
        config=config,
        secret_values={"api_token": "s3cr3t-token-value"},
        actor=actor,
        encryption_key=TEST_KEY,
    )


def test_update_config_in_place_keeps_secret_when_blank(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()
        original_secret_ref = instance.secret_ref_json

        update_connector_instance_config(
            session,
            instance,
            _fake_manifest(),
            config={"endpoint": "https://updated.example.test"},
            secret_values={},
            actor=actor,
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert "updated.example.test" in instance.config_json
        assert instance.secret_ref_json == original_secret_ref


def test_enable_disable_toggles_and_audits(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()
        assert instance.enabled is True

        set_connector_instance_enabled(session, instance, False, actor=actor)
        session.commit()
        assert instance.enabled is False

        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == instance.id, AuditEvent.action.in_(["enable", "disable"])
            )
        ).all()
        assert len(events) == 2


def test_remove_deletes_instance_but_not_historical_evidence(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()
        instance_id = instance.id

        execution = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()
        assert execution.status == "success"
        snapshot_count_before = len(session.scalars(select(EvidenceSnapshot)).all())
        assert snapshot_count_before == 1

        remove_connector_instance(session, instance, actor=actor)
        session.commit()

        assert session.get(ConnectorInstance, instance_id) is None
        # Historical evidence survives removal.
        assert len(session.scalars(select(EvidenceSnapshot)).all()) == snapshot_count_before


def test_resolve_connector_config_merges_secret_just_in_time(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        config = resolve_connector_config(session, instance, encryption_key=TEST_KEY)
        assert config["endpoint"] == "https://fake.example.test"
        assert config["api_token"] == "s3cr3t-token-value"


def test_resolve_connector_config_degrades_safely_on_broken_encryption_key(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        config = resolve_connector_config(session, instance, encryption_key=Fernet.generate_key().decode())
        assert config["api_token"] is None  # never crashes, never returns garbage as if valid


def test_connection_test_success_and_failure_never_raise(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        ok = check_connector_instance(
            session, instance, test_callable=lambda cfg: None, encryption_key=TEST_KEY, actor=actor
        )
        session.commit()
        assert ok is True
        assert instance.last_test_status == "success"

        def _fail(cfg):
            raise RuntimeError("cannot reach fake provider")

        ok = check_connector_instance(
            session, instance, test_callable=_fail, encryption_key=TEST_KEY, actor=actor
        )
        session.commit()
        assert ok is False
        assert instance.last_test_status == "failure"
        assert "cannot reach fake provider" in instance.last_test_error


def test_run_disabled_instance_rejected(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        with pytest.raises(ConnectorLifecycleError, match="disabled"):
            run_connector_instance(
                session,
                instance,
                _fake_manifest(),
                _fake_connector_callable(),
                idempotency_key="run-1",
                encryption_key=TEST_KEY,
            )
        assert session.scalar(select(ConnectorExecution)) is None


def test_run_success_ingests_snapshot_and_records_execution(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        execution = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert execution.status == "success"
        snapshots = session.scalars(select(EvidenceSnapshot)).all()
        assert len(snapshots) == 1
        assert snapshots[0].check_key == "fake_check"
        assert instance.last_success_at is not None


def test_repeated_idempotency_key_does_not_reingest(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        first = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="same-key",
            encryption_key=TEST_KEY,
        )
        session.commit()
        second = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="same-key",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert first.id == second.id
        assert len(session.scalars(select(EvidenceSnapshot)).all()) == 1
        assert len(session.scalars(select(ConnectorExecution)).all()) == 1


def test_different_idempotency_key_runs_again(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()
        run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="run-2",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert len(session.scalars(select(EvidenceSnapshot)).all()) == 2
        assert len(session.scalars(select(ConnectorExecution)).all()) == 2


def test_provider_failure_records_failure_execution_without_ingesting(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        execution = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(should_fail=True),
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert execution.status == "failure"
        assert "unreachable" in execution.error_summary
        assert session.scalar(select(EvidenceSnapshot)) is None
        assert instance.last_failure_at is not None
        assert "unreachable" in instance.last_error_summary


def test_capability_overclaiming_result_records_error_without_ingesting(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        overclaiming_manifest = _fake_manifest(capabilities=("identity.population",))
        execution = run_connector_instance(
            session,
            instance,
            overclaiming_manifest,
            _fake_connector_callable(),  # returns capability="configuration.snapshot"
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert execution.status == "error"
        assert session.scalar(select(EvidenceSnapshot)) is None


def test_unregistered_ingestion_path_records_error_without_ingesting(app, actor):
    """A capability the manifest declares but the ingestion dispatch
    table has no function for is rejected before anything is written."""

    def _weird_capability_callable(config: dict) -> ConnectorResult:
        return ConnectorResult(
            capability="asset.inventory",  # declared by the manifest, but no ingestion path registered
            check_key="k",
            status="pass",
            title="t",
            summary="s",
            provenance=ConnectorProvenance(source_connection_id="c", collected_at=NOW),
        )

    manifest = _fake_manifest(capabilities=("configuration.snapshot", "asset.inventory"))
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        execution = run_connector_instance(
            session,
            instance,
            manifest,
            _weird_capability_callable,
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert execution.status == "error"
        assert "no ingestion path" in execution.error_summary
        assert session.scalar(select(EvidenceSnapshot)) is None


def test_partial_ingestion_write_is_rolled_back_on_later_failure(app, actor, monkeypatch):
    """A genuine "ingestion partially wrote, then something downstream
    failed" scenario: the ingestion function itself successfully adds
    an EvidenceSnapshot to the session and then raises. Proves
    run_connector_instance's rollback removes that partial write rather
    than letting the caller's eventual commit persist it alongside the
    failure execution row."""
    import app.connector_lifecycle as lifecycle_module

    def _ingest_then_fail(session, result, manifest):
        session.add(
            EvidenceSnapshot(
                source_type=manifest.connector_id,
                check_key=result.check_key,
                status=result.status,
                title=result.title,
                collected_at=NOW,
                raw_payload_sha256="0" * 64,
            )
        )
        session.flush()
        raise RuntimeError("downstream failure after a partial write")

    monkeypatch.setitem(lifecycle_module._INGESTION_DISPATCH, "configuration.snapshot", _ingest_then_fail)

    with app.state.session_factory() as session:
        instance = _install(session, actor)
        set_connector_instance_enabled(session, instance, True, actor=actor)
        session.commit()

        execution = run_connector_instance(
            session,
            instance,
            _fake_manifest(),
            _fake_connector_callable(),
            idempotency_key="run-1",
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert execution.status == "failure"
        assert "downstream failure after a partial write" in execution.error_summary
        assert session.scalar(select(EvidenceSnapshot)) is None


# --- issue #27: upgrade/rollback lifecycle ----------------------------------


def test_upgrade_plan_surfaces_new_capabilities_and_permissions(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        from_manifest = _fake_manifest()
        to_manifest = _fake_manifest(
            version="1.1.0",
            capabilities=("configuration.snapshot", "evidence.collect"),
            required_permissions=("new.readonly.scope",),
        )
        plan = plan_connector_instance_upgrade(instance, from_manifest, to_manifest)

        assert plan.direction == "upgrade"
        assert plan.new_capabilities == ("evidence.collect",)
        assert plan.new_required_permissions == ("new.readonly.scope",)


def test_apply_upgrade_moves_version_and_audits(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        from_manifest = _fake_manifest()
        to_manifest = _fake_manifest(version="1.1.0")
        plan = plan_connector_instance_upgrade(instance, from_manifest, to_manifest)
        apply_connector_instance_upgrade(session, instance, plan, to_manifest, actor=actor)
        session.commit()

        assert instance.manifest_version == "1.1.0"
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == instance.id, AuditEvent.action == "upgrade")
        ).all()
        assert len(audit) == 1
        assert "1.0.0" in audit[0].detail and "1.1.0" in audit[0].detail


def test_plan_rejects_identity_confusion(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        other_connector_manifest = _fake_manifest(connector_id="a_different_connector")
        with pytest.raises(ConnectorLifecycleError, match="identity confusion"):
            plan_connector_instance_upgrade(instance, _fake_manifest(), other_connector_manifest)


def test_plan_rejects_incompatible_core_contract_version(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        incompatible_manifest = _fake_manifest(version="1.1.0", min_core_contract_version=999)
        with pytest.raises(ConnectorContractError):
            plan_connector_instance_upgrade(instance, _fake_manifest(), incompatible_manifest)


def test_apply_rejects_silent_downgrade(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        instance.manifest_version = "1.5.0"
        session.commit()

        from_manifest = _fake_manifest(version="1.5.0")
        to_manifest = _fake_manifest(version="1.0.0")
        plan = plan_connector_instance_upgrade(instance, from_manifest, to_manifest)
        assert plan.direction == "downgrade"

        with pytest.raises(ConnectorLifecycleError, match="downgrade"):
            apply_connector_instance_upgrade(session, instance, plan, to_manifest, actor=actor)
        assert instance.manifest_version == "1.5.0"  # unchanged


def test_apply_allows_explicit_acknowledged_downgrade_and_audits_as_rollback(app, actor):
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        instance.manifest_version = "1.5.0"
        session.commit()

        from_manifest = _fake_manifest(version="1.5.0")
        to_manifest = _fake_manifest(version="1.0.0")
        plan = plan_connector_instance_upgrade(instance, from_manifest, to_manifest)
        apply_connector_instance_upgrade(
            session, instance, plan, to_manifest, actor=actor, allow_downgrade=True
        )
        session.commit()

        assert instance.manifest_version == "1.0.0"
        audit = session.scalars(
            select(AuditEvent).where(AuditEvent.entity_id == instance.id, AuditEvent.action == "rollback")
        ).all()
        assert len(audit) == 1


def test_apply_rejects_stale_plan_after_concurrent_version_change(app, actor):
    """A plan computed against an old starting version must not apply
    if the instance has since moved on — otherwise a stale "upgrade"
    plan (relative to its own stale starting point) could silently move
    the instance *backward* from its real current version while
    plan.direction still reads "upgrade"."""
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        stale_plan = plan_connector_instance_upgrade(
            instance, _fake_manifest(version="1.0.0"), _fake_manifest(version="1.1.0")
        )

        # Someone else applies a different, newer upgrade first.
        other_plan = plan_connector_instance_upgrade(
            instance, _fake_manifest(version="1.0.0"), _fake_manifest(version="1.2.0")
        )
        apply_connector_instance_upgrade(
            session, instance, other_plan, _fake_manifest(version="1.2.0"), actor=actor
        )
        session.commit()
        assert instance.manifest_version == "1.2.0"

        with pytest.raises(ConnectorLifecycleError, match="reload and re-plan"):
            apply_connector_instance_upgrade(
                session, instance, stale_plan, _fake_manifest(version="1.1.0"), actor=actor
            )
        assert instance.manifest_version == "1.2.0"  # unchanged, not silently moved backward


def test_apply_rejects_substituted_manifest_not_matching_reviewed_plan(app, actor):
    """A plan's checks (compatibility/capabilities/permissions) were
    only ever run against the manifest that produced it. Applying with
    a *different* to_manifest — e.g. one with extra capabilities the
    plan never surfaced — must be rejected, not silently accepted."""
    with app.state.session_factory() as session:
        instance = _install(session, actor)
        session.commit()

        plan = plan_connector_instance_upgrade(
            instance, _fake_manifest(version="1.0.0"), _fake_manifest(version="1.1.0")
        )
        substituted_manifest = _fake_manifest(
            version="1.1.0", capabilities=("configuration.snapshot", "identity.population")
        )

        with pytest.raises(ConnectorLifecycleError, match="does not match the plan's reviewed version"):
            apply_connector_instance_upgrade(session, instance, plan, substituted_manifest, actor=actor)
        assert instance.manifest_version == "1.0.0"  # unchanged
