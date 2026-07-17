# Drive approvals and optional Workspace Directory sync

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Sixth slice of `feat/startup-compliance-operations`: best-effort mirroring
of Google Drive Approvals for a captured `PolicyVersion`, and an optional
Google Workspace Directory sync (piggybacked on the existing Drive
connection's OAuth grant) that updates `Person.employment_status`. Both
are explicitly optional per spec — neither blocks anything if unavailable
or not configured. See ADRs #19/#20.

## Files Changed

- `app/models.py` — `PolicyApprovalSnapshot` (append-only, FK to
  `PolicyVersion`), `PolicyVersion.approval_snapshots` relationship
  (deferred from the previous commit until this model existed).
- `app/google_drive_approvals.py` — `fetch_approvals` (raises one
  `ApprovalsUnavailableError` for any failure — 403/404/malformed/
  unsupported tenant), `parse_approval` (tolerant of field-name variance,
  computes `raw_payload_sha256` over the canonical JSON for dedup).
- `app/google_workspace_directory.py` — `fetch_directory_users` (paginated
  Admin SDK Directory API call, minimal fields only), `sync_directory_users`
  (create/update `Person` by normalized email; never deletes).
- `app/google_drive.py` — `build_authorization_url` gained `extra_scopes`
  so Directory sync can request its scope in the same consent grant.
- `app/routers/policies.py` — `POST /{id}/drive-approvals` (admin-only,
  dedupes unchanged re-syncs, associates with `policy.latest_version`);
  `view_policy` now passes sorted `approval_snapshots` to the template.
- `app/routers/people.py` — `POST /people/sync-workspace-directory`
  (admin-only, flashes an error if the sync flag is off, otherwise reuses
  the Drive connection's token).
- `app/routers/google_drive.py` — `connect` now includes the Directory
  scope when `GRC_GOOGLE_WORKSPACE_DIRECTORY_ENABLED=true`.
- `app/config.py` — `google_workspace_directory_enabled`.
- `app/templates/policies/detail.html` — approval history table + "Sync
  Drive approvals" button (admin-only).
- `app/templates/people/list.html` — "Sync Google Workspace Directory"
  button (admin-only, shown only when enabled).
- `app/routers/placeholders.py` — Connectors status text updated.
- `migrations/versions/fc8a454e9e0f_*.py` — `policy_approval_snapshots`
  table (plain add, no backfill needed).
- `.env.example` — `GRC_GOOGLE_WORKSPACE_DIRECTORY_ENABLED`.
- `tests/test_drive_approvals_and_directory.py` — approval parsing
  (extraction, missing-id rejection, tolerant of unknown shape), sync
  creates a snapshot + audits, re-syncing unchanged content doesn't
  duplicate, a real status change creates a new row without mutating the
  old one, an unavailable Approvals API shows the message without failing
  the request, admin-only gating; directory sync creates/updates People,
  never deletes an unmatched manual Person, requires the enabled flag,
  requires admin, and end-to-end route test with audit event.

## Verification

- [x] `pytest` — 161 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified on a fresh database (plain table add, no
  backfill required).

## Decisions & Alternatives Rejected

- See ADRs #19/#20.
- Approval snapshots are associated with `policy.latest_version` (the
  most recently captured version) rather than trying to match a specific
  historical version by inferred revision — Drive's approval payloads
  don't reliably expose which content revision they approved, so
  guessing a specific past version would be less honest than associating
  with the version an admin most recently confirmed as current.
- Directory sync reuses the Drive connection instead of a separate OAuth
  flow — see ADR #20. This does mean enabling the flag after Drive is
  already connected requires reconnecting (the new scope only appears in
  a fresh consent grant); documented in `.env.example`.

## Known Gaps / Follow-ups

- AWS CloudTrail/IAM evidence collection is the next commit on this
  branch.
- No UI indicator distinguishing "Directory sync has never run" from
  "ran but the tenant has zero users" — both show `total: 0`; low value
  to distinguish further given this is an admin-triggered, infrequent
  action.
