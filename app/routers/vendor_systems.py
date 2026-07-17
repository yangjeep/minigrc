from __future__ import annotations

import datetime
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit_event
from app.deps import get_db, require_login, verify_csrf
from app.flash import redirect_with_flash
from app.models import (
    BILLING_FREQUENCIES,
    TRI_STATE_VALUES,
    VENDOR_LIFECYCLE_STATUSES,
    Person,
    VendorSystem,
)
from app.vendor_flags import compute_flags

router = APIRouter(prefix="/vendors", tags=["vendors"], dependencies=[Depends(require_login)])


def _parse_minor_units(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int((Decimal(value) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{value}' is not a valid amount") from None


def _parse_date(value: str) -> datetime.date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _people_options(db: Session) -> list[Person]:
    return list(db.scalars(select(Person).order_by(Person.display_name, Person.email)).all())


@router.get("")
def list_vendors(
    request: Request,
    status: str = "",
    department: str = "",
    owner: str = "",
    flag: str = "",
    db: Session = Depends(get_db),
):
    query = select(VendorSystem)
    if status in VENDOR_LIFECYCLE_STATUSES:
        query = query.where(VendorSystem.lifecycle_status == status)
    if department:
        query = query.where(VendorSystem.primary_department == department)
    if owner:
        query = query.where(VendorSystem.business_owner_person_id == owner)
    vendors = list(db.scalars(query.order_by(VendorSystem.system_name)).all())

    flags_by_id = {v.id: compute_flags(v) for v in vendors}
    if flag:
        vendors = [v for v in vendors if flag in flags_by_id[v.id]]

    spend_by_currency: dict[str, int] = defaultdict(int)
    for vendor in vendors:
        if vendor.annualized_cost_minor is not None:
            spend_by_currency[vendor.currency] += vendor.annualized_cost_minor

    departments = sorted(
        {v.primary_department for v in db.scalars(select(VendorSystem)).all() if v.primary_department}
    )

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "vendors/list.html",
        {
            "vendors": vendors,
            "flags_by_id": flags_by_id,
            "spend_by_currency": dict(spend_by_currency),
            "lifecycle_statuses": VENDOR_LIFECYCLE_STATUSES,
            "departments": departments,
            "people": _people_options(db),
            "selected_status": status,
            "selected_department": department,
            "selected_owner": owner,
            "selected_flag": flag,
        },
    )


@router.get("/renewals")
def upcoming_renewals(request: Request, db: Session = Depends(get_db)):
    vendors = list(
        db.scalars(
            select(VendorSystem)
            .where(VendorSystem.renewal_date.is_not(None))
            .order_by(VendorSystem.renewal_date)
        ).all()
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "vendors/renewals.html", {"vendors": vendors})


@router.get("/new")
def new_vendor_form(request: Request, db: Session = Depends(get_db)):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "vendors/new.html",
        {
            "lifecycle_statuses": VENDOR_LIFECYCLE_STATUSES,
            "billing_frequencies": BILLING_FREQUENCIES,
            "tri_state_values": TRI_STATE_VALUES,
            "people": _people_options(db),
        },
    )


def _extract_vendor_fields(form: dict) -> dict:
    return {
        "system_name": form["system_name"].strip(),
        "vendor_name": form["vendor_name"].strip(),
        "lifecycle_status": form.get("lifecycle_status")
        if form.get("lifecycle_status") in VENDOR_LIFECYCLE_STATUSES
        else "active",
        "access_url": form.get("access_url", "").strip(),
        "admin_console_url": form.get("admin_console_url", "").strip() or None,
        "primary_department": form.get("primary_department", "").strip(),
        "business_owner_person_id": form.get("business_owner_person_id") or None,
        "uses_shared_login": form.get("uses_shared_login") == "on",
        "shared_credential_reference_url": form.get("shared_credential_reference_url", "").strip() or None,
        "primary_admin_person_id": form.get("primary_admin_person_id") or None,
        "secondary_admin_person_id": form.get("secondary_admin_person_id") or None,
        "billing_frequency": form.get("billing_frequency")
        if form.get("billing_frequency") in BILLING_FREQUENCIES
        else "other",
        "currency": (form.get("currency", "USD") or "USD").strip().upper()[:3],
        "seat_count": _parse_int(form.get("seat_count", "")),
        "contract_url": form.get("contract_url", "").strip() or None,
        "contract_start_date": _parse_date(form.get("contract_start_date", "")),
        "renewal_date": _parse_date(form.get("renewal_date", "")),
        "auto_renew": form.get("auto_renew") if form.get("auto_renew") in TRI_STATE_VALUES else "unknown",
        "cancellation_notice_days": _parse_int(form.get("cancellation_notice_days", "")),
        "renewal_owner_person_id": form.get("renewal_owner_person_id") or None,
        "support_portal_url": form.get("support_portal_url", "").strip() or None,
        "support_email": form.get("support_email", "").strip() or None,
        "support_phone": form.get("support_phone", "").strip() or None,
        "account_manager_name": form.get("account_manager_name", "").strip() or None,
        "account_manager_email": form.get("account_manager_email", "").strip() or None,
        "emergency_escalation_instructions": form.get("emergency_escalation_instructions", "").strip(),
        "customer_account_reference": form.get("customer_account_reference", "").strip() or None,
    }


@router.post("")
async def create_vendor(request: Request, db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    form = {k: v for k, v in (await request.form()).items()}

    system_name = (form.get("system_name") or "").strip()
    vendor_name = (form.get("vendor_name") or "").strip()
    if not system_name or not vendor_name:
        return redirect_with_flash("/vendors/new", "System name and vendor name are required.", kind="error")

    try:
        billing_amount_minor = _parse_minor_units(form.get("billing_amount", ""))
        cost_per_seat_minor = _parse_minor_units(form.get("cost_per_seat", ""))
    except ValueError as exc:
        return redirect_with_flash("/vendors/new", str(exc), kind="error")

    fields = _extract_vendor_fields(form)
    vendor = VendorSystem(
        **fields,
        billing_amount_minor=billing_amount_minor,
        cost_per_seat_minor=cost_per_seat_minor,
    )
    db.add(vendor)
    db.flush()
    record_audit_event(
        db,
        entity_type="vendor_system",
        entity_id=vendor.id,
        action="create",
        detail=f"Added vendor system '{vendor.system_name}' ({vendor.vendor_name})",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/vendors/{vendor.id}", "Vendor system added.")


@router.get("/{vendor_id}")
def view_vendor(vendor_id: str, request: Request, db: Session = Depends(get_db)):
    vendor = db.get(VendorSystem, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor system not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "vendors/detail.html", {"vendor": vendor, "flags": compute_flags(vendor)}
    )


@router.get("/{vendor_id}/edit")
def edit_vendor_form(vendor_id: str, request: Request, db: Session = Depends(get_db)):
    vendor = db.get(VendorSystem, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor system not found")
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "vendors/edit.html",
        {
            "vendor": vendor,
            "lifecycle_statuses": VENDOR_LIFECYCLE_STATUSES,
            "billing_frequencies": BILLING_FREQUENCIES,
            "tri_state_values": TRI_STATE_VALUES,
            "people": _people_options(db),
        },
    )


@router.post("/{vendor_id}/edit")
async def update_vendor(
    vendor_id: str, request: Request, db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)
):
    vendor = db.get(VendorSystem, vendor_id)
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor system not found")

    form = {k: v for k, v in (await request.form()).items()}
    system_name = (form.get("system_name") or "").strip()
    vendor_name = (form.get("vendor_name") or "").strip()
    if not system_name or not vendor_name:
        return redirect_with_flash(
            f"/vendors/{vendor_id}/edit", "System name and vendor name are required.", kind="error"
        )

    try:
        billing_amount_minor = _parse_minor_units(form.get("billing_amount", ""))
        cost_per_seat_minor = _parse_minor_units(form.get("cost_per_seat", ""))
    except ValueError as exc:
        return redirect_with_flash(f"/vendors/{vendor_id}/edit", str(exc), kind="error")

    fields = _extract_vendor_fields(form)
    for key, value in fields.items():
        setattr(vendor, key, value)
    vendor.billing_amount_minor = billing_amount_minor
    vendor.cost_per_seat_minor = cost_per_seat_minor

    record_audit_event(
        db,
        entity_type="vendor_system",
        entity_id=vendor.id,
        action="update",
        detail=f"Updated vendor system '{vendor.system_name}'",
        actor=request.state.user.email,
    )
    return redirect_with_flash(f"/vendors/{vendor_id}", "Vendor system updated.")
