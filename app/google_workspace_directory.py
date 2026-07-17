"""Optional Google Workspace Directory sync.

Determines whether internal people are still active employees, for
vendor-admin and roster-matching purposes — not a general HR sync. Uses
the read-only `admin.directory.user.readonly` scope; admin-only sync. If
this scope isn't granted, manual `Person` records remain fully usable —
see app/routers/people.py. Never confuse this with Google OIDC login
(app/google_oidc.py), which authenticates a session, not a directory.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Person

DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
DIRECTORY_API_BASE = "https://admin.googleapis.com/admin/directory/v1"
_PAGE_SIZE = 200


class DirectorySyncError(ValueError):
    """User-facing reason a Workspace Directory sync failed."""


@dataclass(frozen=True)
class DirectoryUser:
    external_id: str
    primary_email: str
    display_name: str
    suspended: bool
    archived: bool


def fetch_directory_users(*, access_token: str, timeout: float = 30.0) -> list[DirectoryUser]:
    """Sync only the minimal fields needed: stable id, primary email,
    display name, and suspended/archived status — no other profile data."""
    users: list[DirectoryUser] = []
    page_token: str | None = None

    while True:
        params = {
            "customer": "my_customer",
            "maxResults": _PAGE_SIZE,
            "fields": "nextPageToken,users(id,primaryEmail,name/fullName,suspended,archived)",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            response = httpx.get(
                f"{DIRECTORY_API_BASE}/users",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                raise DirectorySyncError(
                    "Directory scope not granted or insufficient permissions. Reconnect Google "
                    "Drive with Workspace Directory sync enabled."
                ) from exc
            raise DirectorySyncError(
                f"Google Workspace Directory returned an unexpected error ({status})."
            ) from exc
        except httpx.HTTPError as exc:
            raise DirectorySyncError("Could not reach Google Workspace Directory.") from exc

        payload = response.json()
        for raw in payload.get("users", []):
            users.append(
                DirectoryUser(
                    external_id=str(raw.get("id", "")),
                    primary_email=str(raw.get("primaryEmail", "")),
                    display_name=(raw.get("name") or {}).get("fullName", ""),
                    suspended=bool(raw.get("suspended")),
                    archived=bool(raw.get("archived")),
                )
            )

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return users


def sync_directory_users(session: Session, users: list[DirectoryUser]) -> dict[str, int]:
    """Create/update Person records from directory users.

    Never deletes: a Person missing from this sync (e.g. a manually
    added contractor) is simply not touched, preserving history rather
    than guessing why they're absent.
    """
    created = 0
    updated = 0
    now = datetime.datetime.now(datetime.UTC)

    for directory_user in users:
        normalized_email = directory_user.primary_email.strip().lower()
        if not normalized_email:
            continue

        status = "suspended" if (directory_user.suspended or directory_user.archived) else "active"
        person = session.scalar(select(Person).where(Person.email == normalized_email))
        if person is None:
            session.add(
                Person(
                    email=normalized_email,
                    display_name=directory_user.display_name,
                    employment_status=status,
                    source="google_workspace",
                    external_id=directory_user.external_id,
                    last_synced_at=now,
                )
            )
            created += 1
        else:
            person.display_name = directory_user.display_name or person.display_name
            person.employment_status = status
            person.source = "google_workspace"
            person.external_id = directory_user.external_id
            person.last_synced_at = now
            updated += 1

    return {"created": created, "updated": updated, "total": len(users)}
