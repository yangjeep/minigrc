"""Vendor user-roster CSV snapshot import.

Fixed MVP format: `email,name,role,status,last_login_at`. Every import
creates one immutable VendorUserSnapshot + its rows; validates every row
before writing anything, so a malformed, oversized, or too-long file
changes nothing (see app/uploads.py for the byte-size bound applied by the
caller before this module ever sees the bytes).
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Person, VendorSystem, VendorUserSnapshot, VendorUserSnapshotRow
from app.security import normalize_email

REQUIRED_COLUMNS = {"email", "name", "role", "status", "last_login_at"}


class VendorRosterImportError(ValueError):
    """User-facing reason a roster import was rejected; nothing was written."""


def _parse_last_login(value: str) -> datetime.datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VendorRosterImportError(f"Invalid last_login_at value: '{value}'") from None


def parse_roster_csv(raw_bytes: bytes, *, max_rows: int) -> list[dict]:
    """Parse and validate every row. Raises VendorRosterImportError, writes nothing."""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VendorRosterImportError("File is not valid UTF-8 text.") from None

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = REQUIRED_COLUMNS - fieldnames
    if missing:
        raise VendorRosterImportError(f"Missing required column(s): {', '.join(sorted(missing))}")

    rows: list[dict] = []
    seen_emails: set[str] = set()

    for line_number, row in enumerate(reader, start=2):
        if line_number - 1 > max_rows:
            raise VendorRosterImportError(f"CSV file exceeds the maximum of {max_rows} rows.")

        email = (row.get("email") or "").strip()
        if not email:
            raise VendorRosterImportError(f"Row {line_number}: email is required")

        normalized = normalize_email(email)
        if normalized in seen_emails:
            raise VendorRosterImportError(f"Row {line_number}: duplicate normalized email '{normalized}'")
        seen_emails.add(normalized)

        rows.append(
            {
                "normalized_email": normalized,
                "imported_email": email,
                "imported_name": (row.get("name") or "").strip(),
                "imported_role": (row.get("role") or "").strip(),
                "imported_status": (row.get("status") or "").strip(),
                "imported_last_login_at": _parse_last_login(row.get("last_login_at") or ""),
            }
        )

    if not rows:
        raise VendorRosterImportError("CSV file contained no data rows.")

    return rows


def import_vendor_roster_snapshot(
    session: Session,
    vendor: VendorSystem,
    *,
    raw_bytes: bytes,
    original_filename: str,
    imported_by_user_id: str,
    max_rows: int,
) -> VendorUserSnapshot:
    """Validate the full file, then write one immutable snapshot + rows.

    Also updates `vendor.roster_last_confirmed_at` — only ever on a
    successful import, in the same transaction.
    """
    rows = parse_roster_csv(raw_bytes, max_rows=max_rows)  # raises before any write

    normalized_emails = {r["normalized_email"] for r in rows}
    people_by_email = {
        p.email: p.id
        for p in session.scalars(select(Person).where(Person.email.in_(normalized_emails))).all()
    }

    snapshot = VendorUserSnapshot(
        vendor_system_id=vendor.id,
        imported_by_user_id=imported_by_user_id,
        original_filename=original_filename,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        row_count=len(rows),
    )
    session.add(snapshot)
    session.flush()

    for row in rows:
        session.add(
            VendorUserSnapshotRow(
                snapshot_id=snapshot.id,
                matched_person_id=people_by_email.get(row["normalized_email"]),
                **row,
            )
        )

    vendor.roster_last_confirmed_at = datetime.datetime.now(datetime.UTC)
    session.flush()
    return snapshot


def latest_snapshot(session: Session, vendor_id: str) -> VendorUserSnapshot | None:
    return session.scalar(
        select(VendorUserSnapshot)
        .where(VendorUserSnapshot.vendor_system_id == vendor_id)
        .order_by(VendorUserSnapshot.imported_at.desc())
        .limit(1)
    )


def previous_snapshot(session: Session, vendor_id: str, before_snapshot_id: str) -> VendorUserSnapshot | None:
    current = session.get(VendorUserSnapshot, before_snapshot_id)
    if current is None:
        return None
    return session.scalar(
        select(VendorUserSnapshot)
        .where(
            VendorUserSnapshot.vendor_system_id == vendor_id,
            VendorUserSnapshot.imported_at < current.imported_at,
        )
        .order_by(VendorUserSnapshot.imported_at.desc())
        .limit(1)
    )


def compute_delta(
    previous_rows: list[VendorUserSnapshotRow], current_rows: list[VendorUserSnapshotRow]
) -> dict:
    """Diff two snapshots' rows by normalized email. `previous_rows` may be empty."""
    previous_by_email = {r.normalized_email: r for r in previous_rows}
    current_by_email = {r.normalized_email: r for r in current_rows}

    added = [r for email, r in current_by_email.items() if email not in previous_by_email]
    removed = [r for email, r in previous_by_email.items() if email not in current_by_email]

    role_changes = []
    status_changes = []
    newly_assigned_admins = []
    for email, current_row in current_by_email.items():
        previous_row = previous_by_email.get(email)
        if previous_row is None:
            continue
        if previous_row.imported_role != current_row.imported_role:
            role_changes.append(
                {"email": email, "before": previous_row.imported_role, "after": current_row.imported_role}
            )
            if current_row.imported_role.lower() == "admin" and previous_row.imported_role.lower() != "admin":
                newly_assigned_admins.append(current_row)
        if previous_row.imported_status != current_row.imported_status:
            status_changes.append(
                {
                    "email": email,
                    "before": previous_row.imported_status,
                    "after": current_row.imported_status,
                }
            )

    return {
        "added": added,
        "removed": removed,
        "role_changes": role_changes,
        "status_changes": status_changes,
        "newly_assigned_admins": newly_assigned_admins,
    }


def flag_inactive_matched_people(
    rows: list[VendorUserSnapshotRow], people_by_id: dict[str, Person]
) -> list[VendorUserSnapshotRow]:
    """Rows whose matched Person is departed/suspended — access that should be revoked."""
    return [
        r
        for r in rows
        if r.matched_person_id
        and people_by_id.get(r.matched_person_id) is not None
        and people_by_id[r.matched_person_id].employment_status in ("departed", "suspended")
    ]


def flag_unmatched_internal_emails(
    rows: list[VendorUserSnapshotRow], known_internal_domains: set[str]
) -> list[VendorUserSnapshotRow]:
    """Rows with no Person match whose email domain matches a known internal domain."""
    return [
        r
        for r in rows
        if r.matched_person_id is None and r.normalized_email.rsplit("@", 1)[-1] in known_internal_domains
    ]
