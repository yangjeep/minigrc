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
from app.config import get_settings
from app.crypto import DecryptionError, EncryptionNotConfiguredError, decrypt
from app.db import build_engine, init_db, make_session_factory, session_scope
from app.imports import run_import
from app.models import AwsConnection, User
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
        role = "admin" if is_first_user else "user"
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

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return migrate()
    if args.command == "create-user":
        return create_user(args.email)
    if args.command == "promote-admin":
        return promote_admin(args.email)
    if args.command == "aws-run-checks":
        return aws_run_checks()
    if args.command == "import-csv":
        return import_csv_command(args.importer, args.file, args.framework_id)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
