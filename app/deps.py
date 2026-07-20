"""FastAPI dependency helpers."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserSession
from app.security import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, csrf_tokens_match, hash_session_token


def get_db(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _redirect_to_login(message: str | None = None) -> HTTPException:
    location = "/login"
    if message:
        location = f"/login?{urlencode({'flash': message, 'flash_kind': 'error'})}"
    return HTTPException(status_code=303, headers={"Location": location})


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    """Require a valid, unexpired, unrevoked session for an active user; redirect to /login otherwise.

    Checked on every request (not just at login) so disabling a user takes
    effect immediately even against an already-issued session cookie.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise _redirect_to_login()

    token_hash = hash_session_token(raw_token)
    user_session = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if user_session is None or user_session.revoked_at is not None:
        raise _redirect_to_login()

    now = datetime.datetime.now(datetime.UTC)
    expires_at = user_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.UTC)
    if expires_at < now:
        raise _redirect_to_login()

    user = db.get(User, user_session.user_id)
    if user is None:
        raise _redirect_to_login()
    if user.status != "active":
        raise _redirect_to_login("Your account is no longer active. Contact an administrator.")

    request.state.user = user
    request.state.user_session = user_session
    return user


def require_admin(user: User = Depends(require_login)) -> User:
    """Require the logged-in user to hold the `admin` role.

    Gates integration configuration, credential connections, manual syncs,
    and destructive vendor operations. Every other authenticated route
    stays available to any logged-in user — see
    docs/decisions/architectural-decisions.md.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return user


def verify_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_value, csrf_token):
        raise HTTPException(status_code=400, detail="CSRF validation failed")


def verify_csrf_header(request: Request) -> None:
    """CSRF check for JSON API endpoints (register grid), header instead of form field."""
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME)
    header_value = request.headers.get("X-CSRF-Token")
    if not csrf_tokens_match(cookie_value, header_value):
        raise HTTPException(status_code=400, detail="CSRF validation failed")
