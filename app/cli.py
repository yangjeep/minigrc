"""Operational CLI, run as `python -m app.cli <command>`.

Kept to the two commands this app actually needs (applying migrations and
bootstrapping the first user) rather than a general admin CLI framework.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from app.audit import record_audit_event
from app.config import get_settings
from app.db import build_engine, init_db, make_session_factory, session_scope
from app.models import User
from app.security import hash_password, normalize_email

MIN_PASSWORD_LENGTH = 8


def migrate() -> int:
    settings = get_settings()
    engine = build_engine(settings.resolved_database_path)
    init_db(engine)
    print(f"Database schema at '{settings.resolved_database_path}' is up to date.")
    return 0


def create_user(email: str) -> int:
    settings = get_settings()
    engine = build_engine(settings.resolved_database_path)
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
    engine = build_engine(settings.resolved_database_path)
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

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return migrate()
    if args.command == "create-user":
        return create_user(args.email)
    if args.command == "promote-admin":
        return promote_admin(args.email)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
