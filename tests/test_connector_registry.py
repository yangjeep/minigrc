"""Unit/integration tests for app/connector_registry.py (issue #26)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.connector_registry import (
    PermissionReview,
    RegistryError,
    build_permission_review,
    filter_by_capability,
    filter_compatible,
    install_from_registry,
    list_deprecated_installed_instances,
    load_registry,
    to_manifest,
)
from app.connectors.manifest import validate_manifest
from app.models import ConnectorInstance, User
from app.security import hash_password

TEST_KEY = Fernet.generate_key().decode()

# Resolved relative to this file, not hardcoded — matches
# app/db.py's/tests/test_framework_catalog_migration.py's PROJECT_ROOT
# convention. A hardcoded absolute path here would only work in
# whichever sandbox wrote it and fail against CI's checkout path.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_REGISTRY_PATH = str(PROJECT_ROOT / "registry" / "registry.json")


def _write_registry(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"registry_version": 1, "entries": entries}))
    return str(path)


def _fake_entry(**overrides) -> dict:
    entry = {
        "connector_id": "fake_reference",
        "display_name": "Fake Reference Connector",
        "publisher": "minigrc-tests",
        "version": "1.0.0",
        "capabilities": ["configuration.snapshot"],
        "required_permissions": [],
        "supported_auth_methods": [],
        "config_schema": [
            {"name": "endpoint", "type": "str", "required": True, "secret": False},
            {"name": "api_token", "type": "str", "required": True, "secret": True},
        ],
        "min_core_contract_version": 1,
        "max_core_contract_version": None,
        "verification_status": "unofficial",
        "package_location": "tests.test_connector_registry:_unused_fake_manifest_builder",
        "checksum_sha256": None,
        "deprecated": False,
    }
    entry.update(overrides)
    return entry


def _unused_fake_manifest_builder():
    """Never actually called by anything in this test file — its
    module:function path only proves that referencing it in a
    `package_location` string never causes it to be imported/executed
    during load_registry/filter_*."""
    raise RuntimeError("this must never be invoked by registry loading/filtering")


def test_load_valid_registry(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry()])
    entries = load_registry(path)
    assert len(entries) == 1
    assert entries[0].connector_id == "fake_reference"


def test_load_missing_file_raises_registry_error(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(str(tmp_path / "does-not-exist.json"))


def test_load_malformed_json_raises_registry_error(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not valid json")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(str(path))


def test_load_missing_required_field_raises(tmp_path):
    entry = _fake_entry()
    del entry["display_name"]
    path = _write_registry(tmp_path, [entry])
    with pytest.raises(RegistryError, match="display_name"):
        load_registry(path)


def test_load_invalid_verification_status_raises(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry(verification_status="definitely-legit")])
    with pytest.raises(RegistryError, match="verification_status"):
        load_registry(path)


def test_load_duplicate_connector_version_raises(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry(), _fake_entry()])
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(path)


def test_to_manifest_round_trips_and_passes_validate_manifest(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry()])
    entry = load_registry(path)[0]
    manifest = to_manifest(entry)
    validate_manifest(manifest)
    assert manifest.connector_id == "fake_reference"
    assert manifest.capabilities == ("configuration.snapshot",)
    assert len(manifest.config_schema) == 2


def test_filter_by_capability(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _fake_entry(connector_id="a", capabilities=["configuration.snapshot"]),
            _fake_entry(connector_id="b", version="1.0.1", capabilities=["identity.population"]),
            _fake_entry(
                connector_id="c", version="1.0.2", capabilities=["configuration.snapshot"], deprecated=True
            ),
        ],
    )
    entries = load_registry(path)
    matches = filter_by_capability(entries, "configuration.snapshot")
    assert {e.connector_id for e in matches} == {"a"}  # "c" excluded: deprecated


def test_filter_compatible_fails_closed_with_no_override(tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _fake_entry(connector_id="compatible", min_core_contract_version=1),
            _fake_entry(connector_id="too_new", version="1.0.1", min_core_contract_version=999),
        ],
    )
    entries = load_registry(path)
    compatible = filter_compatible(entries, core_contract_version=1)
    assert {e.connector_id for e in compatible} == {"compatible"}


def test_registry_loading_never_imports_package_location(tmp_path):
    """The fake entry's package_location points at a function that
    raises if ever called — proving load/filter never resolve it."""
    path = _write_registry(tmp_path, [_fake_entry()])
    entries = load_registry(path)
    filter_by_capability(entries, "configuration.snapshot")
    filter_compatible(entries)
    # No exception means _unused_fake_manifest_builder was never invoked.


def test_shipped_registry_json_loads_and_every_entry_validates():
    """Regression guard: the real, git-tracked registry/registry.json
    must always load and produce manifests that pass #24's own
    validate_manifest — catches a hand-edit mistake before it ships."""
    entries = load_registry(SHIPPED_REGISTRY_PATH)
    assert len(entries) >= 1
    for entry in entries:
        manifest = to_manifest(entry)
        validate_manifest(manifest)


@pytest.fixture
def actor(app):
    with app.state.session_factory() as session:
        user = User(email="registry-admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_install_from_registry_uses_the_real_lifecycle_boundary(app, actor, tmp_path):
    path = _write_registry(tmp_path, [_fake_entry()])
    entry = load_registry(path)[0]

    with app.state.session_factory() as session:
        instance = install_from_registry(
            session,
            entry,
            display_name="My Fake Connector",
            config={"endpoint": "https://fake.example.test"},
            secret_values={"api_token": "s3cr3t-token-value"},
            actor=actor,
            encryption_key=TEST_KEY,
        )
        session.commit()

        assert instance.connector_id == "fake_reference"
        assert instance.enabled is False  # same default install_connector_instance already enforces
        assert "s3cr3t-token-value" not in instance.config_json
        assert "s3cr3t-token-value" not in instance.secret_ref_json

        row = session.get(ConnectorInstance, instance.id)
        assert row is not None


# --- issue #27: registry integrity hardening -------------------------------


def test_load_identity_confusion_raises(tmp_path):
    """Same connector_id under two different publishers is exactly what
    package/identity squatting looks like — reject at load time."""
    path = _write_registry(
        tmp_path,
        [
            _fake_entry(connector_id="shared_id", publisher="trusted-vendor"),
            _fake_entry(connector_id="shared_id", version="1.0.1", publisher="different-vendor"),
        ],
    )
    with pytest.raises(RegistryError, match="identity confusion"):
        load_registry(path)


def test_load_malformed_checksum_raises(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry(checksum_sha256="not-a-real-checksum")])
    with pytest.raises(RegistryError, match="checksum_sha256"):
        load_registry(path)


def test_load_well_formed_checksum_accepted(tmp_path):
    valid_checksum = "a" * 64
    path = _write_registry(tmp_path, [_fake_entry(checksum_sha256=valid_checksum)])
    entries = load_registry(path)
    assert entries[0].checksum_sha256 == valid_checksum


def test_build_permission_review_flags_broad_permission(tmp_path):
    path = _write_registry(
        tmp_path, [_fake_entry(required_permissions=["admin.directory.user.readonly", "drive.readonly"])]
    )
    entry = load_registry(path)[0]
    review = build_permission_review(entry)
    assert isinstance(review, PermissionReview)
    assert review.broad_permission_warnings == ("admin.directory.user.readonly",)
    assert review.required_permissions == ("admin.directory.user.readonly", "drive.readonly")
    assert review.capabilities == (("configuration.snapshot", review.capabilities[0][1]),)


def test_build_permission_review_no_warnings_for_narrow_permissions(tmp_path):
    path = _write_registry(tmp_path, [_fake_entry(required_permissions=["drive.readonly"])])
    entry = load_registry(path)[0]
    review = build_permission_review(entry)
    assert review.broad_permission_warnings == ()


def test_list_deprecated_installed_instances_finds_matching_only(app, actor, tmp_path):
    path = _write_registry(
        tmp_path,
        [
            _fake_entry(connector_id="still_current"),
            _fake_entry(connector_id="now_deprecated", version="1.0.1", deprecated=True),
        ],
    )
    entries = load_registry(path)

    with app.state.session_factory() as session:
        current_instance = install_from_registry(
            session,
            [e for e in entries if e.connector_id == "still_current"][0],
            display_name="Current",
            config={"endpoint": "https://a.example.test"},
            secret_values={"api_token": "x"},
            actor=actor,
            encryption_key=TEST_KEY,
        )
        deprecated_instance = install_from_registry(
            session,
            [e for e in entries if e.connector_id == "now_deprecated"][0],
            display_name="Deprecated",
            config={"endpoint": "https://b.example.test"},
            secret_values={"api_token": "y"},
            actor=actor,
            encryption_key=TEST_KEY,
        )
        session.commit()

        flagged = list_deprecated_installed_instances(session, entries)
        flagged_ids = {instance.id for instance in flagged}
        assert deprecated_instance.id in flagged_ids
        assert current_instance.id not in flagged_ids
