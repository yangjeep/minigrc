"""Self-hosted backup/restore/portability procedure (issue #46).

Covers three independently-reasoned-about things: the active relational
backend (SQLite snapshot or PostgreSQL dump — exactly one per #22's
exclusive-backend contract), the S3-compatible evidence object store
(#32), and a non-secret configuration checklist. Distinct from
`app/audit_package.py` (#34, a curated, period-scoped, secrets-stripped
auditor export — not a backup) and from issue #9's still-unimplemented
PaaS-specific ops runbook.

`GRC_ENCRYPTION_KEY` is deliberately never read, written, or logged by
this module — see `verify_restore`'s decryption sanity check for the
one place this module proves (without ever touching the key's value)
that the *correct* key was supplied post-restore.

See docs/superpowers/specs/2026-08-20-issue46-backup-restore-design.md
for the full design.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.orm import sessionmaker

from app.compliance_scope import rebuild_compliance_scope_projection
from app.config import Settings
from app.control_definition_lifecycle import rebuild_internal_control_version_projection
from app.control_occurrences import rebuild_control_occurrence_projection
from app.control_tests import rebuild_control_test_projection
from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt
from app.evidence_repository import rebuild_evidence_artifact_version_projection
from app.finding_lifecycle import rebuild_finding_projection
from app.framework_adoption import rebuild_framework_adoption_projection
from app.models import EvidenceArtifactVersion, GoogleDriveConnection, Secret
from app.object_storage import build_s3_client, download_object, upload_object
from app.policy_lifecycle import rebuild_policy_version_lifecycle_projection

MANIFEST_FILENAME = "manifest.json"
DB_BACKUP_FILENAME_SQLITE = "database.sqlite3"
DB_BACKUP_FILENAME_POSTGRES = "database.pgdump"
EVIDENCE_PREFIX = "evidence/"

# Explicit allow-list, not "every Settings field except a deny-list" —
# a newly-added secret-shaped Settings field in the future is invisible
# to the snapshot by default rather than silently included.
_NON_SECRET_CONFIG_FIELDS = (
    "app_env",
    "log_level",
    "public_base_url",
    "max_upload_mb",
    "max_vendor_roster_rows",
    "session_ttl_hours",
    "session_cookie_secure",
    "s3_bucket",
    "s3_region",
    "s3_endpoint_url",
    "oidc_issuer",
    "oidc_display_name",
    "oidc_role_claim_name",
    "oidc_allowed_domains",
    "google_oidc_allowed_domains",
    "google_workspace_directory_enabled",
)
# Names only, so a restore checklist can say "resupply these" without
# this module ever reading their value.
_SECRET_ENV_VAR_NAMES_BY_CONFIGURED_FLAG = {
    "google_oidc_configured": ("GRC_GOOGLE_OIDC_CLIENT_ID", "GRC_GOOGLE_OIDC_CLIENT_SECRET"),
    "oidc_configured": ("GRC_OIDC_CLIENT_ID", "GRC_OIDC_CLIENT_SECRET"),
    "google_drive_configured": ("GRC_GOOGLE_DRIVE_CLIENT_ID", "GRC_GOOGLE_DRIVE_CLIENT_SECRET"),
    "evidence_repository_configured": ("GRC_S3_ACCESS_KEY_ID", "GRC_S3_SECRET_ACCESS_KEY"),
}


class BackupError(RuntimeError):
    """A safe-to-display reason a backup/restore/verify step failed."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat()


def _is_postgres(settings: Settings) -> bool:
    return bool(settings.database_url)


@dataclasses.dataclass(frozen=True)
class EvidenceObjectRecord:
    object_key: str
    byte_size: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class BackupManifest:
    created_at: str
    backend: str  # "sqlite" | "postgresql"
    db_backup_filename: str
    evidence_objects: tuple[EvidenceObjectRecord, ...]
    config_snapshot: dict
    required_env_vars_not_included: tuple[str, ...]

    @property
    def evidence_object_count(self) -> int:
        return len(self.evidence_objects)

    @property
    def evidence_total_bytes(self) -> int:
        return sum(o.byte_size for o in self.evidence_objects)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> BackupManifest:
        payload = json.loads(raw)
        payload["evidence_objects"] = tuple(EvidenceObjectRecord(**o) for o in payload["evidence_objects"])
        payload["required_env_vars_not_included"] = tuple(payload["required_env_vars_not_included"])
        return cls(**payload)


def _build_config_snapshot(settings: Settings) -> tuple[dict, tuple[str, ...]]:
    snapshot = {field: getattr(settings, field) for field in _NON_SECRET_CONFIG_FIELDS}
    required_env_vars: list[str] = ["GRC_ENCRYPTION_KEY"]
    configured_flags = {
        "google_oidc_configured": settings.google_oidc_enabled,
        "oidc_configured": settings.oidc_enabled,
        "google_drive_configured": settings.google_drive_configured,
        "evidence_repository_configured": settings.evidence_repository_configured,
    }
    snapshot.update(configured_flags)
    for flag_name, was_configured in configured_flags.items():
        if was_configured:
            required_env_vars.extend(_SECRET_ENV_VAR_NAMES_BY_CONFIGURED_FLAG[flag_name])
    return snapshot, tuple(required_env_vars)


def _backup_sqlite(source_path: str, dest_path: str) -> None:
    """A live, WAL-safe snapshot via SQLite's own backup API — not a
    raw file copy, which risks capturing a mid-write, inconsistent file
    if the app is running concurrently."""
    if not os.path.exists(source_path):
        raise BackupError(f"SQLite database file not found: {source_path!r}")
    source = sqlite3.connect(source_path)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _libpq_url_and_env(database_url: str) -> tuple[str, dict]:
    """`database_url` is a SQLAlchemy URL (e.g.
    `postgresql+psycopg://user:pass@host/db`) — pg_dump/pg_restore
    speak plain libpq URLs (`postgresql://...`, no `+driver` suffix)
    and would fail to parse the SQLAlchemy form directly. Also strips
    the password out of the URL and passes it via the `PGPASSWORD`
    environment variable instead of a command-line argument — a
    command-line argument is visible to any other user on the same
    host with process-list access (`ps aux`, `/proc/<pid>/cmdline`);
    an env var is only visible via `/proc/<pid>/environ`, which
    requires higher privilege on most systems."""
    url_obj = make_url(database_url)
    env = os.environ.copy()
    if url_obj.password:
        env["PGPASSWORD"] = url_obj.password
    # URL.set(password=None) does NOT clear an existing password —
    # SQLAlchemy treats an explicit None there as "leave unchanged,"
    # not "clear this field" (confirmed directly against this
    # SQLAlchemy version; verified via test_backup_postgres_libpq_url_strips_driver_and_password).
    # URL.create with the field simply omitted is what actually drops it.
    password_free_url = URL.create(
        drivername="postgresql",
        username=url_obj.username,
        host=url_obj.host,
        port=url_obj.port,
        database=url_obj.database,
        query=url_obj.query,
    )
    return password_free_url.render_as_string(hide_password=False), env


def _backup_postgres(database_url: str, dest_path: str) -> None:
    if shutil.which("pg_dump") is None:
        raise BackupError(
            "pg_dump is not installed/available on PATH — cannot back up PostgreSQL from this "
            "host. See docs/deployment/backup-restore.md for the manual procedure."
        )
    safe_url, env = _libpq_url_and_env(database_url)
    subprocess.run(
        ["pg_dump", "--format=custom", f"--file={dest_path}", safe_url],
        check=True,
        capture_output=True,
        env=env,
    )


def _restore_sqlite(source_path: str, dest_path: str) -> None:
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    shutil.copy2(source_path, dest_path)


def _restore_postgres(database_url: str, source_path: str) -> None:
    if shutil.which("pg_restore") is None:
        raise BackupError(
            "pg_restore is not installed/available on PATH — cannot restore PostgreSQL on this "
            "host. See docs/deployment/backup-restore.md for the manual procedure."
        )
    safe_url, env = _libpq_url_and_env(database_url)
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", f"--dbname={safe_url}", source_path],
        check=True,
        capture_output=True,
        env=env,
    )


def _list_evidence_object_keys(client, bucket: str) -> list[str]:
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=EVIDENCE_PREFIX):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def create_backup(settings: Settings, dest_dir: str) -> BackupManifest:
    """Back up the active DB backend, the S3-compatible evidence store,
    and a non-secret configuration checklist into `dest_dir`. Never
    writes any secret value (encryption key, client secrets, S3
    credentials) — only records which env vars must be resupplied at
    restore time."""
    os.makedirs(dest_dir, exist_ok=True)

    backend = "postgresql" if _is_postgres(settings) else "sqlite"
    if backend == "postgresql":
        db_backup_filename = DB_BACKUP_FILENAME_POSTGRES
        _backup_postgres(settings.database_url, str(Path(dest_dir) / db_backup_filename))
    else:
        db_backup_filename = DB_BACKUP_FILENAME_SQLITE
        _backup_sqlite(settings.resolved_database_path, str(Path(dest_dir) / db_backup_filename))

    evidence_objects: list[EvidenceObjectRecord] = []
    if settings.evidence_repository_configured:
        client = build_s3_client(settings)
        for object_key in _list_evidence_object_keys(client, settings.s3_bucket):
            local_path = Path(dest_dir) / object_key
            os.makedirs(local_path.parent, exist_ok=True)
            client.download_file(settings.s3_bucket, object_key, str(local_path))
            byte_size = local_path.stat().st_size
            sha256 = hashlib.sha256(local_path.read_bytes()).hexdigest()
            evidence_objects.append(
                EvidenceObjectRecord(object_key=object_key, byte_size=byte_size, sha256=sha256)
            )

    config_snapshot, required_env_vars = _build_config_snapshot(settings)
    manifest = BackupManifest(
        created_at=_now_iso(),
        backend=backend,
        db_backup_filename=db_backup_filename,
        evidence_objects=tuple(evidence_objects),
        config_snapshot=config_snapshot,
        required_env_vars_not_included=required_env_vars,
    )
    (Path(dest_dir) / MANIFEST_FILENAME).write_text(manifest.to_json())
    return manifest


def restore_backup(settings: Settings, backup_dir: str) -> BackupManifest:
    """Reconstitute the DB backend and evidence store from `backup_dir`.
    Does not run migrations (the restored DB is already at whatever
    schema revision it was backed up at — bringing it forward to the
    currently-deployed app version is the caller's own `init_db`/
    `migrate` step, same as every other app/cli.py command already
    does first)."""
    manifest_path = Path(backup_dir) / MANIFEST_FILENAME
    if not manifest_path.exists():
        raise BackupError(f"No {MANIFEST_FILENAME} found in {backup_dir!r} — not a backup directory.")
    manifest = BackupManifest.from_json(manifest_path.read_text())

    db_backup_path = str(Path(backup_dir) / manifest.db_backup_filename)
    if manifest.backend == "postgresql":
        _restore_postgres(settings.database_url, db_backup_path)
    else:
        _restore_sqlite(db_backup_path, settings.resolved_database_path)

    if manifest.evidence_objects:
        if not settings.evidence_repository_configured:
            raise BackupError(
                "Backup contains evidence objects but the evidence repository is not configured "
                "on this instance — set GRC_S3_* before restoring."
            )
        client = build_s3_client(settings)
        for record in manifest.evidence_objects:
            local_path = Path(backup_dir) / record.object_key
            media_type = "application/octet-stream"
            upload_object(
                client,
                bucket=settings.s3_bucket,
                object_key=record.object_key,
                tmp_path=str(local_path),
                media_type=media_type,
            )

    return manifest


@dataclasses.dataclass(frozen=True)
class VerificationReport:
    evidence_verified_count: int
    evidence_mismatched_keys: tuple[str, ...]
    evidence_missing_keys: tuple[str, ...]
    projections_rebuilt: tuple[str, ...]
    projection_errors: tuple[str, ...]
    decryption_check: str  # "ok" | "no_encrypted_data_found" | "failed"

    @property
    def passed(self) -> bool:
        return (
            not self.evidence_mismatched_keys
            and not self.evidence_missing_keys
            and not self.projection_errors
            and self.decryption_check != "failed"
        )


_PROJECTION_REBUILDERS = {
    "policy_version_lifecycle": rebuild_policy_version_lifecycle_projection,
    "control_occurrence": rebuild_control_occurrence_projection,
    "internal_control_version": rebuild_internal_control_version_projection,
    "evidence_artifact_version": rebuild_evidence_artifact_version_projection,
    "control_test": rebuild_control_test_projection,
    "compliance_scope": rebuild_compliance_scope_projection,
    "finding": rebuild_finding_projection,
    "framework_adoption": rebuild_framework_adoption_projection,
}


def _verify_evidence_hashes(session, settings: Settings) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    if not settings.evidence_repository_configured:
        return 0, (), ()
    client = build_s3_client(settings)
    verified = 0
    mismatched: list[str] = []
    missing: list[str] = []
    for version in session.scalars(select(EvidenceArtifactVersion)).all():
        try:
            content = download_object(client, bucket=settings.s3_bucket, object_key=version.object_key)
        except Exception:  # noqa: BLE001 - any download failure means the object is missing/unreachable
            missing.append(version.object_key)
            continue
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != version.sha256:
            mismatched.append(version.object_key)
        else:
            verified += 1
    return verified, tuple(mismatched), tuple(missing)


def _rebuild_all_projections(session) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rebuilt: list[str] = []
    errors: list[str] = []
    for name, rebuild_fn in _PROJECTION_REBUILDERS.items():
        try:
            rebuild_fn(session)
            session.commit()
            rebuilt.append(name)
        except Exception as exc:  # noqa: BLE001 - collect every projection's own failure, don't abort the rest
            session.rollback()
            errors.append(f"{name}: {exc}")
    return tuple(rebuilt), tuple(errors)


def _check_decryption(session, settings: Settings) -> str:
    secret = session.scalar(select(Secret).where(Secret.kind == "encrypted"))
    ciphertext = secret.ciphertext if secret is not None else None
    if ciphertext is None:
        connection = session.scalar(
            select(GoogleDriveConnection).where(GoogleDriveConnection.revoked_at.is_(None))
        )
        if connection is not None and connection.encrypted_refresh_token:
            ciphertext = connection.encrypted_refresh_token
    if ciphertext is None:
        return "no_encrypted_data_found"
    try:
        decrypt(ciphertext, key=settings.encryption_key)
    except (DecryptionError, EncryptionNotConfiguredError):
        return "failed"
    return "ok"


def verify_restore(session_factory: sessionmaker, settings: Settings) -> VerificationReport:
    """Run against an already-restored-and-migrated instance. Proves
    (1) evidence bytes match the authoritative EvidenceArtifactVersion.sha256,
    not just the backup-time hash, (2) every domain's event-sourced
    projection rebuilds without error, and (3) the currently-configured
    GRC_ENCRYPTION_KEY is the *correct* one, not merely a validly-shaped
    one — without this function ever reading/logging the key's value."""
    with session_factory() as session:
        verified, mismatched, missing = _verify_evidence_hashes(session, settings)
        rebuilt, projection_errors = _rebuild_all_projections(session)
        decryption_check = _check_decryption(session, settings)

    return VerificationReport(
        evidence_verified_count=verified,
        evidence_mismatched_keys=mismatched,
        evidence_missing_keys=missing,
        projections_rebuilt=rebuilt,
        projection_errors=projection_errors,
        decryption_check=decryption_check,
    )
