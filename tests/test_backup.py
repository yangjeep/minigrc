"""Tests for issue #46's backup/restore/portability procedure
(app/backup.py).

Unit tests for the config-snapshot allow-list and manifest
round-tripping; integration tests for a real SQLite + moto-backed S3
backup -> restore -> verify-restore cycle, matching this repository's
established real-boundary-testing convention (tests/test_connector_ingestion.py,
tests/test_evidence_artifacts_routes.py).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import boto3
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from app.backup import (
    BackupError,
    BackupManifest,
    EvidenceObjectRecord,
    _build_config_snapshot,
    _libpq_url_and_env,
    create_backup,
    restore_backup,
    verify_restore,
)
from app.config import Settings
from app.crypto import encrypt
from app.evidence_repository import capture_evidence_version
from app.models import EvidenceArtifact, GoogleDriveConnection, User
from app.object_storage import capture_raw_bytes
from app.security import hash_password

TEST_KEY = Fernet.generate_key().decode()
BUCKET = "minigrc-backup-test"


# --- unit: config snapshot allow-list ---------------------------------------


def test_config_snapshot_never_includes_secret_fields():
    settings = Settings(
        database_path="/tmp/grc.db",
        encryption_key=TEST_KEY,
        s3_endpoint_url="https://s3.example.test",
        s3_bucket="fixture-bucket",
        s3_access_key_id="AKIA-fixture",
        s3_secret_access_key="s3cr3t-fixture",
        oidc_client_secret="oidc-secret-fixture",
        google_drive_client_secret="drive-secret-fixture",
        google_oidc_client_secret="google-oidc-secret-fixture",
    )
    snapshot, required_env_vars = _build_config_snapshot(settings)

    serialized = repr(snapshot) + repr(required_env_vars)
    for secret_value in (
        TEST_KEY,
        "AKIA-fixture",
        "s3cr3t-fixture",
        "oidc-secret-fixture",
        "drive-secret-fixture",
        "google-oidc-secret-fixture",
    ):
        assert secret_value not in serialized
    assert "GRC_ENCRYPTION_KEY" in required_env_vars
    assert "GRC_S3_ACCESS_KEY_ID" in required_env_vars  # evidence_repository_configured is True


def test_config_snapshot_omits_env_vars_for_unconfigured_integrations():
    settings = Settings()
    _snapshot, required_env_vars = _build_config_snapshot(settings)
    assert required_env_vars == ("GRC_ENCRYPTION_KEY",)


# --- unit: manifest round-trip -----------------------------------------------


def test_manifest_json_round_trips():
    manifest = BackupManifest(
        created_at="2026-08-20T00:00:00",
        backend="sqlite",
        db_backup_filename="database.sqlite3",
        evidence_objects=(
            EvidenceObjectRecord(object_key="evidence/a/b/content.txt", byte_size=5, sha256="x"),
        ),
        config_snapshot={"app_env": "production"},
        required_env_vars_not_included=("GRC_ENCRYPTION_KEY",),
    )
    round_tripped = BackupManifest.from_json(manifest.to_json())
    assert round_tripped == manifest
    assert round_tripped.evidence_object_count == 1
    assert round_tripped.evidence_total_bytes == 5


# --- unit: missing pg_dump/pg_restore binaries -------------------------------


def test_backup_postgres_without_pg_dump_raises_clear_error(monkeypatch):
    monkeypatch.setattr("app.backup.shutil.which", lambda _name: None)
    settings = Settings(database_url="postgresql+psycopg://user:pass@host/db")
    with pytest.raises(BackupError, match="pg_dump"):
        create_backup(settings, "/tmp/does-not-matter")


def test_libpq_url_strips_driver_suffix_and_moves_password_to_env():
    """pg_dump/pg_restore speak plain libpq URLs, not SQLAlchemy's
    `+psycopg`-suffixed form, and the password must never appear as a
    command-line argument (visible via `ps aux`/`/proc/<pid>/cmdline`
    to any other user on the host with process-list access)."""
    url, env = _libpq_url_and_env("postgresql+psycopg://user:s3cr3t-fixture@host:5432/db")
    assert "s3cr3t-fixture" not in url
    assert url == "postgresql://user@host:5432/db"
    assert env["PGPASSWORD"] == "s3cr3t-fixture"


# --- integration: real SQLite + moto-backed S3 round trip -------------------


@pytest.fixture
def s3_settings(tmp_path):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        settings = Settings(
            database_path=str(tmp_path / "source" / "grc.db"),
            encryption_key=TEST_KEY,
            s3_endpoint_url="https://s3.amazonaws.com",
            s3_bucket=BUCKET,
            s3_access_key_id="fake-key",
            s3_secret_access_key="fake-secret",
        )
        yield settings


def _seed_source_db(settings: Settings) -> tuple[str, str]:
    """Build a real, migrated SQLite DB with one captured evidence
    artifact and one encrypted GoogleDriveConnection, returning
    (artifact_id, expected_plaintext) for later verification."""
    from app.db import build_engine, init_db, make_session_factory

    engine = build_engine(settings.resolved_database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        user = User(email="backup-admin@example.com", password_hash=hash_password("x"), role="admin")
        session.add(user)
        session.flush()

        connection = GoogleDriveConnection(
            connected_by_user_id=user.id,
            granted_scopes="https://www.googleapis.com/auth/drive.readonly",
            encrypted_refresh_token=encrypt("real-refresh-token-fixture", key=settings.encryption_key),
        )
        session.add(connection)

        artifact = EvidenceArtifact(title="Backup test evidence")
        session.add(artifact)
        session.flush()

        from app.object_storage import build_s3_client

        client = build_s3_client(settings)
        captured = capture_raw_bytes(b"evidence content for backup test", max_bytes=1024 * 1024)
        capture_evidence_version(
            session,
            artifact,
            client=client,
            bucket=settings.s3_bucket,
            tmp_path=captured.tmp_path,
            sha256=captured.sha256,
            byte_size=captured.byte_size,
            media_type="text/plain",
            original_filename="evidence.txt",
            extension="txt",
            actor_id=user.id,
        )
        session.commit()
        return artifact.id, "real-refresh-token-fixture"


def test_full_backup_restore_verify_cycle(tmp_path, s3_settings):
    _seed_source_db(s3_settings)

    backup_dir = str(tmp_path / "backup")
    manifest = create_backup(s3_settings, backup_dir)

    assert manifest.backend == "sqlite"
    assert manifest.evidence_object_count == 1
    assert manifest.required_env_vars_not_included == (
        "GRC_ENCRYPTION_KEY",
        "GRC_S3_ACCESS_KEY_ID",
        "GRC_S3_SECRET_ACCESS_KEY",
    )
    # No secret value ever written to the backup directory.
    manifest_text = (Path(backup_dir) / "manifest.json").read_text()
    assert s3_settings.encryption_key not in manifest_text
    assert s3_settings.s3_secret_access_key not in manifest_text

    # Simulate a fresh instance: wipe the source DB file and delete the
    # evidence object from the (still-mocked-active) bucket — proving
    # restore re-uploads it rather than relying on it still being there.
    db_path = Path(s3_settings.resolved_database_path)
    db_path.unlink()
    shutil.rmtree(db_path.parent, ignore_errors=True)

    from app.object_storage import build_s3_client

    client = build_s3_client(s3_settings)
    client.delete_object(Bucket=BUCKET, Key=manifest.evidence_objects[0].object_key)

    restore_backup(s3_settings, backup_dir)

    from app.db import build_engine, init_db, make_session_factory

    engine = build_engine(s3_settings.resolved_database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    report = verify_restore(session_factory, s3_settings)

    assert report.evidence_verified_count == 1
    assert report.evidence_mismatched_keys == ()
    assert report.evidence_missing_keys == ()
    assert set(report.projections_rebuilt) == {
        "policy_version_lifecycle",
        "control_occurrence",
        "internal_control_version",
        "evidence_artifact_version",
        "control_test",
        "compliance_scope",
        "finding",
    }
    assert report.projection_errors == ()
    assert report.decryption_check == "ok"
    assert report.passed is True


def test_verify_restore_catches_wrong_encryption_key(tmp_path, s3_settings):
    _seed_source_db(s3_settings)
    backup_dir = str(tmp_path / "backup")
    create_backup(s3_settings, backup_dir)
    restore_backup(s3_settings, backup_dir)

    from app.db import build_engine, init_db, make_session_factory

    engine = build_engine(s3_settings.resolved_database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    wrong_key_settings = Settings(
        database_path=s3_settings.database_path,
        encryption_key=Fernet.generate_key().decode(),  # a validly-shaped but WRONG key
        s3_endpoint_url=s3_settings.s3_endpoint_url,
        s3_bucket=s3_settings.s3_bucket,
        s3_access_key_id=s3_settings.s3_access_key_id,
        s3_secret_access_key=s3_settings.s3_secret_access_key,
    )
    report = verify_restore(session_factory, wrong_key_settings)

    assert report.decryption_check == "failed"
    assert report.passed is False


def test_verify_restore_catches_corrupted_evidence(tmp_path, s3_settings):
    _seed_source_db(s3_settings)
    backup_dir = str(tmp_path / "backup")
    manifest = create_backup(s3_settings, backup_dir)
    restore_backup(s3_settings, backup_dir)

    from app.object_storage import build_s3_client

    client = build_s3_client(s3_settings)
    # Corrupt the restored object in place.
    client.put_object(
        Bucket=BUCKET, Key=manifest.evidence_objects[0].object_key, Body=b"corrupted after restore"
    )

    from app.db import build_engine, init_db, make_session_factory

    engine = build_engine(s3_settings.resolved_database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    report = verify_restore(session_factory, s3_settings)

    assert manifest.evidence_objects[0].object_key in report.evidence_mismatched_keys
    assert report.passed is False
