"""Operational CLI, run as `python -m app.cli <command>`.

Kept to the two commands this app actually needs (applying migrations and
bootstrapping the first user) rather than a general admin CLI framework.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import sys

from sqlalchemy import func, select

from app.audit import record_audit_event
from app.aws_connector import (
    AwsConnectionError,
    build_evidence_snapshot,
    build_session,
    check_cloudtrail,
    check_iam,
)
from app.backup import BackupError, create_backup, restore_backup, verify_restore
from app.config import get_settings
from app.control_occurrences import generate_occurrences
from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt
from app.db import build_engine, init_db, make_session_factory, session_scope
from app.imports import run_import
from app.models import AwsConnection, ControlPeriod, InternalControl, User
from app.security import hash_password, normalize_email

MIN_PASSWORD_LENGTH = 8


def migrate() -> int:
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    # render_as_string(hide_password=True) — never echo a credential from
    # DATABASE_URL to stdout/logs.
    print(f"Database schema at '{engine.url.render_as_string(hide_password=True)}' is up to date.")
    return 0


def create_user(email: str) -> int:
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    normalized = normalize_email(email)

    with session_factory() as session:
        existing = session.scalar(select(User).where(User.email == normalized))
        if existing is not None:
            print(f"error: a user with email '{normalized}' already exists", file=sys.stderr)
            return 1

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("error: passwords do not match", file=sys.stderr)
        return 1
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"error: password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 1

    with session_scope(session_factory) as session:
        is_first_user = session.scalar(select(func.count()).select_from(User)) == 0
        role = "admin" if is_first_user else "operator"
        session.add(User(email=normalized, password_hash=hash_password(password), role=role))

    print(f"Created user '{normalized}'" + (" as the first (admin) user." if is_first_user else "."))
    return 0


def promote_admin(email: str) -> int:
    """Grant the admin role to an existing local user. Accepts no password."""
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    normalized = normalize_email(email)

    with session_scope(session_factory) as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            print(f"error: no user with email '{normalized}' exists", file=sys.stderr)
            return 1
        if user.role == "admin":
            print(f"'{normalized}' is already an admin.")
            return 0

        user.role = "admin"
        record_audit_event(
            session,
            entity_type="user",
            entity_id=user.id,
            action="promote_admin",
            detail=f"Promoted '{normalized}' to admin via CLI",
            actor="cli",
        )

    print(f"Promoted '{normalized}' to admin.")
    return 0


def aws_run_checks() -> int:
    """Run CloudTrail + IAM evidence checks against the configured AWS
    connection. Suitable for an external cron later — this command itself
    adds no scheduling infrastructure."""
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        connection = session.scalar(select(AwsConnection).order_by(AwsConnection.created_at.desc()).limit(1))
        if connection is None:
            print(
                "error: no AWS connection configured — set one up at /connectors/aws first", file=sys.stderr
            )
            return 1

        external_id = None
        if connection.encrypted_external_id:
            try:
                external_id = decrypt(connection.encrypted_external_id, key=settings.encryption_key)
            except (DecryptionError, EncryptionNotConfiguredError) as exc:
                print(f"error: could not decrypt stored external ID: {exc}", file=sys.stderr)
                return 1

        region = connection.regions.split(",")[0].strip() if connection.regions else None
        try:
            aws_session = build_session(role_arn=connection.role_arn, external_id=external_id, region=region)
        except AwsConnectionError as exc:
            connection.last_error_summary = str(exc)
            print(f"error: could not start an AWS session: {exc}", file=sys.stderr)
            return 1

        results = [check_cloudtrail(aws_session), check_iam(aws_session)]
        for result in results:
            session.add(build_evidence_snapshot(result, connection_id=connection.id))
            print(f"{result.check_key}: {result.status} — {result.summary}")

        connection.last_check_at = datetime.datetime.now(datetime.UTC)
        connection.last_error_summary = ""
        record_audit_event(
            session,
            entity_type="aws_connection",
            entity_id=connection.id,
            action="run_checks",
            detail="Ran AWS evidence checks via CLI: "
            + ", ".join(f"{r.check_key}={r.status}" for r in results),
            actor="cli",
        )

    return 0


def generate_control_occurrences_command(control_id: str | None, period_id: str | None) -> int:
    """Materialize expected occurrences for one calendar-cadence control
    (or, if control_id is omitted, every calendar-cadence control),
    optionally scoped to one control period. Idempotent — safe to run
    repeatedly (e.g. from cron later; this command adds no scheduling
    infrastructure itself, matching aws_run_checks's precedent)."""
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_scope(session_factory) as session:
        period = None
        if period_id:
            period = session.get(ControlPeriod, period_id)
            if period is None:
                print(f"error: no control period with id '{period_id}'", file=sys.stderr)
                return 1

        if control_id:
            control = session.get(InternalControl, control_id)
            if control is None:
                print(f"error: no control with id '{control_id}'", file=sys.stderr)
                return 1
            controls = [control]
        else:
            controls = session.scalars(
                select(InternalControl).where(InternalControl.cadence_type == "calendar")
            ).all()

        try:
            total = 0
            for control in controls:
                occurrences = generate_occurrences(session, control, period=period, actor_type="system")
                total += len(occurrences)
                print(f"{control.name}: {len(occurrences)} occurrence(s)")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(f"Generated/confirmed {total} occurrence(s) total.")
    return 0


def import_csv_command(importer_name: str, file_path: str, framework_id: str | None) -> int:
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    try:
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
    except OSError as exc:
        print(f"Could not read '{file_path}': {exc}")
        return 1

    target = {"framework_id": framework_id} if framework_id else {}
    with session_scope(session_factory) as session:
        job = run_import(
            session,
            importer_name=importer_name,
            raw_bytes=raw_bytes,
            filename=file_path.rsplit("/", 1)[-1],
            target=target,
            actor="cli",
            source="cli",
        )
        if job.status != "completed":
            errors = job.validation_errors_json or "[]"
            print(f"Import rejected: {errors}")
            return 1
        print(f"Imported {job.records_created} record(s) via '{importer_name}'.")

    return 0


def import_directory_command(directory: str, importer_name: str) -> int:
    """Run a single reconcile+claim+process pass over a watched import
    directory. For continuous watching, run this repeatedly (cron, a
    process-manager loop) or set GRC_IMPORT_WATCH_DIR/
    GRC_IMPORT_WATCH_IMPORTER so app/worker.py polls it automatically."""
    from pathlib import Path

    from app.import_directory import run_directory_once

    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    processed = run_directory_once(
        session_factory, root=Path(directory), importer_name=importer_name, actor="cli"
    )
    print("Processed one file." if processed else "Nothing to process.")
    return 0


def backup_command(dest: str) -> int:
    """Back up the active DB backend, evidence store, and non-secret
    config checklist to `dest` (issue #46). Never writes any secret
    value — see app/backup.py's module docstring."""
    settings = get_settings()
    try:
        manifest = create_backup(settings, dest)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Backed up {manifest.backend} database and {manifest.evidence_object_count} "
        f"evidence object(s) ({manifest.evidence_total_bytes} bytes) to '{dest}'."
    )
    if manifest.required_env_vars_not_included:
        print(
            "Required env vars to resupply before restore (values were never written to this "
            "backup): " + ", ".join(manifest.required_env_vars_not_included)
        )
    return 0


def restore_command(source: str) -> int:
    """Restore the DB backend and evidence store from `source` (issue
    #46). Does not run migrations — run `migrate` next, then
    `verify-restore`."""
    settings = get_settings()
    try:
        manifest = restore_backup(settings, source)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Restored {manifest.backend} database and {manifest.evidence_object_count} "
        f"evidence object(s) from '{source}'."
    )
    if manifest.required_env_vars_not_included:
        print(
            "Before starting the app, ensure these env vars are set to their ORIGINAL values "
            "(not included in this backup): " + ", ".join(manifest.required_env_vars_not_included)
        )
    print("Next: run `python -m app.cli migrate` then `python -m app.cli verify-restore`.")
    return 0


def verify_restore_command() -> int:
    """Prove a restored instance's evidence integrity, projection-
    rebuild equivalence, and correct-encryption-key possession (issue
    #46). Runs migrations first, matching every other app/cli.py
    command's convention."""
    settings = get_settings()
    engine = build_engine(settings.resolved_engine_target)
    init_db(engine)
    session_factory = make_session_factory(engine)

    report = verify_restore(session_factory, settings)

    print(f"Evidence hashes verified: {report.evidence_verified_count}")
    if report.evidence_mismatched_keys:
        print(f"MISMATCHED evidence hashes: {', '.join(report.evidence_mismatched_keys)}", file=sys.stderr)
    if report.evidence_missing_keys:
        print(f"MISSING evidence objects: {', '.join(report.evidence_missing_keys)}", file=sys.stderr)
    print(f"Projections rebuilt: {', '.join(report.projections_rebuilt) or '(none)'}")
    if report.projection_errors:
        print(f"PROJECTION REBUILD ERRORS: {'; '.join(report.projection_errors)}", file=sys.stderr)
    print(f"Encryption key check: {report.decryption_check}")

    if not report.passed:
        print("error: verify-restore FAILED — see above", file=sys.stderr)
        return 1
    print("verify-restore passed.")
    return 0


def uat_command(node_filter: str | None, postgres: bool) -> int:
    """Run the headless UAT suite (issue #38) — one documented command
    for CLI agents/CI, in place of remembering the underlying pytest
    invocation. Lazily imports pytest so a minimal production install
    (`pip install .`, no `[dev]` extras — see Dockerfile) never needs
    pytest importable for any other CLI command."""
    import os

    import pytest

    os.environ["GRC_UAT_MODE"] = "1"
    k_expressions = [expr for expr in (node_filter, "postgres" if postgres else None) if expr]

    args = ["-m", "uat", "tests/uat", "-v"]
    if k_expressions:
        args += ["-k", " and ".join(f"({expr})" for expr in k_expressions)]
    return pytest.main(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="Apply database migrations up to head")

    create_user_parser = subparsers.add_parser(
        "create-user", help="Create a local login user (the first user becomes admin)"
    )
    create_user_parser.add_argument("--email", required=True)

    promote_admin_parser = subparsers.add_parser(
        "promote-admin", help="Grant the admin role to an existing local user"
    )
    promote_admin_parser.add_argument("--email", required=True)

    subparsers.add_parser(
        "aws-run-checks", help="Run AWS CloudTrail/IAM evidence checks against the configured connection"
    )

    generate_occurrences_parser = subparsers.add_parser(
        "generate-control-occurrences",
        help="Materialize expected occurrences for calendar-cadence controls",
    )
    generate_occurrences_parser.add_argument(
        "--control-id", default=None, help="Limit to one control (default: every calendar-cadence control)"
    )
    generate_occurrences_parser.add_argument(
        "--period-id",
        default=None,
        help="Generate against this control period (default: period-less rolling)",
    )

    import_csv_parser = subparsers.add_parser(
        "import-csv", help="Import a CSV file through the native import subsystem"
    )
    import_csv_parser.add_argument(
        "--importer",
        required=True,
        help="Importer name, e.g. risk_register_csv or framework_requirements_csv",
    )
    import_csv_parser.add_argument("--file", required=True, help="Path to the CSV file")
    import_csv_parser.add_argument(
        "--framework-id", default=None, help="Target framework id (required for framework_requirements_csv)"
    )

    import_dir_parser = subparsers.add_parser(
        "import-directory", help="Run one reconcile+claim+process pass over a watched import directory"
    )
    import_dir_parser.add_argument("directory", help="Path to the watched directory root")
    import_dir_parser.add_argument(
        "--importer", required=True, help="Importer name to apply to claimed files"
    )

    backup_parser = subparsers.add_parser(
        "backup", help="Back up the active DB backend, evidence store, and config checklist (issue #46)"
    )
    backup_parser.add_argument("--dest", required=True, help="Destination directory for the backup")

    restore_parser = subparsers.add_parser(
        "restore", help="Restore the DB backend and evidence store from a backup (issue #46)"
    )
    restore_parser.add_argument("--source", required=True, help="Source backup directory")

    subparsers.add_parser(
        "verify-restore",
        help="Verify a restored instance's evidence hashes, projection rebuilds, and encryption key",
    )

    uat_parser = subparsers.add_parser(
        "uat", help="Run the headless UAT suite (issue #38) — see .agent/TESTING.md"
    )
    uat_parser.add_argument(
        "-k", dest="node_filter", default=None, help="pytest -k expression to select scenarios"
    )
    uat_parser.add_argument(
        "--postgres", action="store_true", help="Only run scenarios parametrized against Postgres"
    )

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return migrate()
    if args.command == "create-user":
        return create_user(args.email)
    if args.command == "promote-admin":
        return promote_admin(args.email)
    if args.command == "aws-run-checks":
        return aws_run_checks()
    if args.command == "generate-control-occurrences":
        return generate_control_occurrences_command(args.control_id, args.period_id)
    if args.command == "import-csv":
        return import_csv_command(args.importer, args.file, args.framework_id)
    if args.command == "import-directory":
        return import_directory_command(args.directory, args.importer)
    if args.command == "backup":
        return backup_command(args.dest)
    if args.command == "restore":
        return restore_command(args.source)
    if args.command == "verify-restore":
        return verify_restore_command()
    if args.command == "uat":
        return uat_command(args.node_filter, args.postgres)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
