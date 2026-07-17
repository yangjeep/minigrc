# People directory and Vendor/System register

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Second slice of `feat/startup-compliance-operations`: a shared `Person`
identity table and a `VendorSystem` register (one record per system a
startup purchases or uses — GitHub, Slack, AWS, etc.), with computed
operational warnings (missing admin, contract missing, renewal
approaching...) rather than a security score. Both are needed before
vendor roster CSV import (next commit) and the Google/AWS connectors
(later commits) — roster rows and vendor admins need a shared "is this
still an employee?" reference, and admin/AWS connections will be gated by
the `require_admin` dependency added in the previous commit.

## Files Changed

- `app/models.py` — `Person` (`EMPLOYMENT_STATUSES`, `PERSON_SOURCES`),
  `VendorSystem` (`VENDOR_LIFECYCLE_STATUSES`, `BILLING_FREQUENCIES`,
  `TRI_STATE_VALUES`), computed `annualized_cost_minor` /
  `cancellation_deadline` properties, `User.person_id` (optional FK).
- `app/vendor_flags.py` — `compute_flags()`: operational warnings computed
  from live data, not stored.
- `app/routers/people.py`, `app/routers/vendor_systems.py` — list (with
  search/filter)/detail/create/edit routes; `/vendors/renewals`.
- `app/templates/people/*`, `app/templates/vendors/*` (incl. shared
  `_form.html` macro for the long vendor field set).
- `app/main.py` — registers both routers, adds `People`/`Vendors` nav items.
- `app/static/style.css` — badge colors for the new status values.
- `migrations/versions/872531f7b0cc_*.py` — `people`, `vendor_systems`
  tables, `users.person_id` FK (via `batch_alter_table` — SQLite can't
  `ALTER ... ADD CONSTRAINT` directly).
- `tests/test_people.py`, `tests/test_vendor_systems.py`,
  `tests/test_pages.py` — CRUD, email normalization/uniqueness, audit
  events, money/date computation, flag logic (including a fully-configured
  vendor asserting *no* flags), filters, renewals page, nav smoke tests.
- `docs/product-scope.md`, `docs/domain/domain-model.md`,
  `docs/decisions/architectural-decisions.md` (ADRs #13, #14).

## Verification

- [x] `pytest` — 94 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified on a fresh database and via upgrade from the
  admin-authorization migration (which itself upgrades from the
  `feat/initial-grc-foundation` schema head).

## Decisions & Alternatives Rejected

- See ADR #13 (single `VendorSystem` model) and #14 (shared `Person`
  table).
- Money stored as integer minor units (cents) with a single authoritative
  `billing_amount_minor` + `billing_frequency`, never separate
  manually-entered monthly/annual totals — `annualized_cost_minor` is
  always derived, so the two numbers can't silently disagree.
- No hard delete for `VendorSystem`/`Person` — consistent with every other
  entity in this app (Framework, Policy, Risk); `lifecycle_status` /
  `employment_status` model the relevant state transitions instead.
- Vendor CRUD is *not* gated behind `require_admin` — it's ordinary GRC
  data entry, consistent with how Risks/Frameworks/Policies already work
  for any authenticated user. Only credential/integration surfaces (later
  commits: Drive OAuth, AWS connection, manual sync) are admin-only.

## Known Gaps / Follow-ups

- "Former employee appears in latest vendor roster" flag exists in
  `compute_flags()`'s signature (`departed_roster_emails`) but nothing
  populates it yet — wired up in the next commit once
  `VendorUserSnapshot`/`VendorUserSnapshotRow` exist.
- No vendor list pagination — acceptable at this app's target scale (one
  organization's vendor list), matching the rest of the app's list views.
