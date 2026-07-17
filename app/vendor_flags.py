"""Computed operational warnings for a VendorSystem — never a security score.

Plain functions over an already-loaded VendorSystem (with its Person
relationships eager enough to read), not a stored column: every flag is
derivable from current data, so persisting it would just be a cache that
could drift stale.
"""

from __future__ import annotations

import datetime

from app.models import VendorSystem

ROSTER_CONFIRMATION_STALE_DAYS = 90
RENEWAL_APPROACHING_DAYS = 60
CANCELLATION_DEADLINE_APPROACHING_DAYS = 30

INACTIVE_EMPLOYMENT_STATUSES = ("suspended", "departed")


def compute_flags(
    vendor: VendorSystem,
    *,
    today: datetime.date | None = None,
    departed_roster_emails: frozenset[str] = frozenset(),
) -> list[str]:
    """Return human-readable operational warnings for one vendor system.

    `departed_roster_emails` is the set of normalized emails appearing in
    the vendor's *latest* roster snapshot whose matched Person (or an
    internal Person sharing that email) is departed/suspended — passed in
    by the caller since computing it requires the roster snapshot tables.
    """
    today = today or datetime.date.today()
    flags: list[str] = []

    if vendor.uses_shared_login:
        flags.append("Shared login in use")

    if vendor.primary_admin_person_id is None:
        flags.append("Missing primary admin")
    elif (
        vendor.primary_admin is not None
        and vendor.primary_admin.employment_status in INACTIVE_EMPLOYMENT_STATUSES
    ):
        flags.append(f"Primary admin is {vendor.primary_admin.employment_status}")

    if vendor.secondary_admin_person_id is None:
        flags.append("Missing secondary admin")
    elif (
        vendor.secondary_admin is not None
        and vendor.secondary_admin.employment_status in INACTIVE_EMPLOYMENT_STATUSES
    ):
        flags.append(f"Secondary admin is {vendor.secondary_admin.employment_status}")

    if not vendor.contract_url:
        flags.append("Contract missing")

    if vendor.renewal_date is not None:
        days_until_renewal = (vendor.renewal_date - today).days
        if 0 <= days_until_renewal <= RENEWAL_APPROACHING_DAYS:
            flags.append("Renewal approaching")

    deadline = vendor.cancellation_deadline
    if deadline is not None:
        days_until_deadline = (deadline - today).days
        if 0 <= days_until_deadline <= CANCELLATION_DEADLINE_APPROACHING_DAYS:
            flags.append("Cancellation deadline approaching")

    if vendor.auto_renew in (None, "unknown"):
        flags.append("Auto-renew status unknown")

    if not (vendor.support_portal_url or vendor.support_email or vendor.support_phone):
        flags.append("Support/escalation path missing")

    if vendor.roster_last_confirmed_at is None:
        flags.append("Roster not confirmed within 90 days")
    else:
        confirmed_at = vendor.roster_last_confirmed_at
        if confirmed_at.tzinfo is not None:
            confirmed_at = confirmed_at.replace(tzinfo=None)
        if (
            datetime.datetime.combine(today, datetime.time.min) - confirmed_at
        ).days > ROSTER_CONFIRMATION_STALE_DAYS:
            flags.append("Roster not confirmed within 90 days")

    if departed_roster_emails:
        flags.append("Former employee appears in latest vendor roster")

    return flags
