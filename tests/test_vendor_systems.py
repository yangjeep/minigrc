from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import AuditEvent, Person, VendorSystem
from app.vendor_flags import compute_flags
from tests.conftest import extract_csrf_token


def _create_vendor(client, **overrides) -> str:
    page = client.get("/vendors/new")
    csrf_token = extract_csrf_token(page.text)
    data = {
        "system_name": "GitHub",
        "vendor_name": "GitHub, Inc.",
        "lifecycle_status": "active",
        "billing_frequency": "monthly",
        "billing_amount": "49.99",
        "currency": "USD",
        "auto_renew": "unknown",
        "csrf_token": csrf_token,
    }
    data.update(overrides)
    response = client.post("/vendors", data=data, follow_redirects=False)
    assert response.status_code == 303, response.headers.get("location")
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def test_create_vendor_system(logged_in_client):
    vendor_id = _create_vendor(logged_in_client)
    detail = logged_in_client.get(f"/vendors/{vendor_id}")
    assert detail.status_code == 200
    assert b"GitHub" in detail.content


def test_required_fields_enforced(logged_in_client):
    page = logged_in_client.get("/vendors/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/vendors",
        data={"system_name": "", "vendor_name": "", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_invalid_billing_amount_rejected(logged_in_client):
    page = logged_in_client.get("/vendors/new")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        "/vendors",
        data={
            "system_name": "Slack",
            "vendor_name": "Slack Technologies",
            "billing_amount": "not-a-number",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_annualized_cost_computed_from_monthly(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client, billing_frequency="monthly", billing_amount="10.00")
    with app.state.session_factory() as session:
        vendor = session.get(VendorSystem, vendor_id)
        assert vendor.annualized_cost_minor == 1000 * 12


def test_annualized_cost_unknown_for_other_frequency(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client, billing_frequency="other", billing_amount="10.00")
    with app.state.session_factory() as session:
        vendor = session.get(VendorSystem, vendor_id)
        assert vendor.annualized_cost_minor is None


def test_currency_must_be_uppercase_at_db_level(app):
    with app.state.session_factory() as session:
        session.add(VendorSystem(system_name="X", vendor_name="Y", currency="usd"))
        try:
            session.commit()
            pytest.fail("expected a CHECK constraint violation")
        except IntegrityError:
            session.rollback()


def test_edit_vendor_is_audited(logged_in_client, app):
    vendor_id = _create_vendor(logged_in_client)
    page = logged_in_client.get(f"/vendors/{vendor_id}/edit")
    csrf_token = extract_csrf_token(page.text)
    response = logged_in_client.post(
        f"/vendors/{vendor_id}/edit",
        data={
            "system_name": "GitHub Enterprise",
            "vendor_name": "GitHub, Inc.",
            "lifecycle_status": "active",
            "billing_frequency": "annual",
            "billing_amount": "500.00",
            "currency": "USD",
            "auto_renew": "yes",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with app.state.session_factory() as session:
        vendor = session.get(VendorSystem, vendor_id)
        assert vendor.system_name == "GitHub Enterprise"
        assert vendor.annualized_cost_minor == 50000

        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "vendor_system", AuditEvent.entity_id == vendor_id
            )
        ).all()
    assert any(e.action == "update" for e in events)


def test_missing_admin_flags(app):
    vendor = VendorSystem(system_name="Datadog", vendor_name="Datadog Inc.")
    flags = compute_flags(vendor)
    assert "Missing primary admin" in flags
    assert "Missing secondary admin" in flags
    assert "Contract missing" in flags
    assert "Auto-renew status unknown" in flags
    assert "Support/escalation path missing" in flags
    assert "Roster not confirmed within 90 days" in flags


def test_inactive_admin_flag(app):
    with app.state.session_factory() as session:
        person = Person(email="left@example.com", employment_status="departed")
        session.add(person)
        session.flush()
        vendor = VendorSystem(
            system_name="Notion", vendor_name="Notion Labs", primary_admin_person_id=person.id
        )
        session.add(vendor)
        session.commit()
        session.refresh(vendor)
        flags = compute_flags(vendor)
    assert "Primary admin is departed" in flags


def test_renewal_and_cancellation_deadline_flags():
    today = datetime.date(2026, 7, 17)
    vendor = VendorSystem(
        system_name="Zoom",
        vendor_name="Zoom Video",
        renewal_date=today + datetime.timedelta(days=60),
        cancellation_notice_days=45,
        contract_url="https://drive.example.com/doc",
        support_email="support@zoom.example.com",
        primary_admin_person_id="x",
        secondary_admin_person_id="y",
        auto_renew="no",
        roster_last_confirmed_at=datetime.datetime.combine(today, datetime.time.min),
    )
    flags = compute_flags(vendor, today=today)
    assert "Renewal approaching" in flags
    assert "Cancellation deadline approaching" in flags
    assert vendor.cancellation_deadline == today + datetime.timedelta(days=60) - datetime.timedelta(days=45)


def test_no_flags_when_fully_configured():
    today = datetime.date(2026, 7, 17)
    vendor = VendorSystem(
        system_name="Fully Configured",
        vendor_name="Vendor Co",
        renewal_date=today + datetime.timedelta(days=200),
        cancellation_notice_days=30,
        contract_url="https://drive.example.com/doc",
        support_email="support@vendor.example.com",
        primary_admin_person_id="x",
        secondary_admin_person_id="y",
        auto_renew="yes",
        roster_last_confirmed_at=datetime.datetime.combine(today, datetime.time.min),
    )
    assert compute_flags(vendor, today=today) == []


def test_filter_by_status_and_flag(logged_in_client):
    _create_vendor(logged_in_client, system_name="ActiveSystem", lifecycle_status="active")
    _create_vendor(logged_in_client, system_name="CancelledSystem", lifecycle_status="cancelled")

    active_only = logged_in_client.get("/vendors?status=active")
    assert b"ActiveSystem" in active_only.content
    assert b"CancelledSystem" not in active_only.content

    missing_admin = logged_in_client.get("/vendors?flag=Missing+primary+admin")
    assert b"ActiveSystem" in missing_admin.content
    assert b"CancelledSystem" in missing_admin.content


def test_renewals_page_lists_vendors_with_renewal_dates(logged_in_client):
    _create_vendor(logged_in_client, system_name="HasRenewal", renewal_date="2027-01-01")
    _create_vendor(logged_in_client, system_name="NoRenewal")

    page = logged_in_client.get("/vendors/renewals")
    assert b"HasRenewal" in page.content
    assert b"NoRenewal" not in page.content


def test_annualized_spend_summary_grouped_by_currency(logged_in_client):
    _create_vendor(logged_in_client, system_name="USDVendor", billing_amount="10.00", currency="USD")
    _create_vendor(logged_in_client, system_name="EURVendor", billing_amount="10.00", currency="EUR")

    listing = logged_in_client.get("/vendors")
    assert b"USD" in listing.content
    assert b"EUR" in listing.content
