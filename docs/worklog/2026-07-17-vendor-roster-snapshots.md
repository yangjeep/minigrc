# Immutable vendor roster snapshots

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Third slice of `feat/startup-compliance-operations`: append-only vendor
user-roster CSV import (`VendorUserSnapshot` / `VendorUserSnapshotRow`),
with delta-vs-previous-snapshot, `Person` matching, departed/suspended
flagging, and admin-only manual linking. Also extracted the bounded-read
upload helper (`app/uploads.py`) out of `app/csv_import.py` now that a
second caller (this feature) needs the identical byte-cap pattern.

## Files Changed

- `app/models.py` — `VendorUserSnapshot`, `VendorUserSnapshotRow`
  (`UniqueConstraint(snapshot_id, normalized_email)`).
- `app/uploads.py` — `read_upload_bounded()` / `UploadTooLargeError`,
  extracted from `app/csv_import.py` (which now re-exports the same names
  for backward compatibility with `app/routers/frameworks.py`).
- `app/vendor_roster_import.py` — fixed CSV format
  (`email,name,role,status,last_login_at`); `parse_roster_csv` validates
  everything before `import_vendor_roster_snapshot` writes anything;
  `compute_delta` (added/removed/role changes/status changes/newly
  assigned admins); `flag_inactive_matched_people`;
  `flag_unmatched_internal_emails` (heuristic: email domain matches an
  existing `Person`'s domain but has no match).
- `app/routers/vendor_systems.py` — `/vendors/{id}/roster` (view + delta +
  flags), `/vendors/{id}/roster/new` (upload form), `POST
  /vendors/{id}/roster` (import, any authenticated user), `POST
  /vendors/{id}/roster/rows/{row_id}/link` (admin-only, sets
  `matched_person_id` only — imported columns never change). Wired the
  "Former employee appears in latest vendor roster" flag (deferred in the
  previous commit) into `view_vendor`.
- `app/config.py` / `.env.example` — `GRC_MAX_VENDOR_ROSTER_ROWS` (default
  5000), reusing the existing `GRC_MAX_UPLOAD_MB` for the byte cap.
- `app/templates/vendors/roster.html`, `roster_new.html`.
- `migrations/versions/1c2a027ce561_*.py` — `vendor_user_snapshots`,
  `vendor_user_snapshot_rows` tables.
- `tests/test_vendor_roster.py` — snapshot creation, non-overwrite on
  re-import, duplicate-email rejection, missing-column rejection,
  oversize/too-many-rows rejection (all proving zero rows written), delta
  display, Person matching, departed-person flagging (both roster page and
  vendor detail page), admin link (audited, doesn't touch imported values),
  403 for non-admin link attempts.

## Verification

- [x] `pytest` — 106 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified fresh and via upgrade from the prior two
  migrations on this branch.

## Decisions & Alternatives Rejected

- See ADR #15.
- Roster CSV import itself is *not* admin-gated (any authenticated user
  can import a snapshot) — only the manual Person-linking action is
  admin-only, per the spec's explicit requirement. Importing a roster is
  additive/immutable evidence, not a credential or destructive operation.
- "Unmatched internal email" detection uses a plain heuristic (does the
  roster email's domain match an existing Person's domain?) rather than a
  configured org-domain setting — avoids a new config surface for a
  same-branch MVP; can be revisited if it produces false positives/negatives
  in practice.

## Known Gaps / Follow-ups

- No pagination on the roster table — fine at expected vendor roster
  sizes (rows are capped at `GRC_MAX_VENDOR_ROSTER_ROWS`, default 5000);
  revisit if that proves too large to render usefully.
- `last_login_at` parsing accepts ISO 8601 with a trailing `Z`; other
  vendor export date formats would need a per-vendor format hint — out of
  scope for the fixed MVP CSV format specified.
