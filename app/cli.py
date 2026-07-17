"""Operational CLI, run as `python -m app.cli <command>`.

Kept to the two commands this app actually needs (applying migrations and
bootstrapping the first user) rather than a general admin CLI framework.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

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
        session.add(User(email=normalized, password_hash=hash_password(password)))

    print(f"Created user '{normalized}'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="Apply database migrations up to head")

    create_user_parser = subparsers.add_parser("create-user", help="Create a local login user")
    create_user_parser.add_argument("--email", required=True)

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return migrate()
    if args.command == "create-user":
        return create_user(args.email)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
