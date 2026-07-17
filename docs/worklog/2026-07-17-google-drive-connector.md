# Google Drive connector and policy sources

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Fifth slice of `feat/startup-compliance-operations`: one org-level,
admin-managed, encrypted-at-rest Google Drive OAuth connection, plus
optional Policy-to-Drive-file association and a "Capture current version"
action that reuses the existing validated storage pipeline. See ADRs
#17/#18.

## Files Changed

- `app/crypto.py` — `encrypt`/`decrypt` (Fernet, requires
  `GRC_ENCRYPTION_KEY`), `EncryptionNotConfiguredError`/`DecryptionError`.
- `app/google_drive.py` — `parse_drive_file_id` (SSRF-safe: extracts an ID,
  never fetches the input as a URL), OAuth authorization
  URL/token-exchange/access-token-refresh (via
  `google.oauth2.credentials.Credentials` + `google.auth.transport.
  requests`), `get_file_metadata`, `list_revisions` (best-effort),
  `download_file_content` (blob download or Google Workspace export to
  PDF), `captured_filename` (guarantees a recognized extension even when
  Drive's own `name` lacks one — caught by a real test failure, not just
  inferred).
- `app/routers/google_drive.py` — `/connectors/google-drive` (view),
  `/connect` + `/callback` (admin-only, state-cookie CSRF-equivalent for
  the OAuth redirect), `/disconnect` (admin-only; erases the credential,
  keeps the row, best-effort revokes at Google).
  `get_access_token_for_active_connection` shared with the policy routes.
- `app/routers/policies.py` — `/{id}/drive-link` (admin-only, associates
  metadata only), `/{id}/drive-refresh` (admin-only, re-checks the current
  Drive revision without capturing), `/{id}/drive-capture` (admin-only,
  downloads/exports content, stores it through
  `save_policy_version_from_bytes`, records source provenance).
- `app/storage.py` — refactored `save_policy_version_upload` around a
  shared `_save_policy_version` core; added `save_policy_version_from_bytes`
  for Drive-sourced content (2nd caller justified the extraction).
- `app/models.py` — `Policy.source_type`/`drive_*`;
  `PolicyVersion.source_type`/`source_file_id`/`source_revision_id`/
  `source_modified_at`/`captured_at`; `GoogleDriveConnection`
  (append-only history, like `PolicyVersion`).
- `app/config.py` — `public_base_url`, `google_drive_client_id/secret`,
  `encryption_key` (+ `google_drive_configured`/`_redirect_uri`
  properties).
- `app/templates/connectors/google_drive.html`,
  `app/templates/policies/detail.html` (Drive source section, drift
  banner, admin-only link/refresh/capture forms, version table now shows
  source + revision).
- `migrations/versions/a1d7da5c4a94_*.py` — `google_drive_connections`
  table; `policies`/`policy_versions` provenance columns
  (`server_default='manual'` backfill; `captured_at` backfilled from
  `created_at` via an explicit `UPDATE`, verified against a seeded
  pre-migration row, not just an empty-database run).
- `pyproject.toml` — added `cryptography`.
- `tests/test_google_drive.py` — crypto roundtrip/misconfigured/wrong-key,
  Drive ID/URL parsing (valid forms + SSRF-style rejections), connector
  page states, admin-only connect/disconnect, encrypted storage on
  connect, missing-refresh-token rejection, disconnect erases token but
  keeps history + audits, admin-only Drive-link/capture, link without an
  active connection fails cleanly, capture writes an immutable version
  with provenance, spoofed content rejected + temp files cleaned up, a
  Drive failure creates no partial version, a second capture creates a
  new version without mutating the first, Google Doc export to PDF,
  manual upload still works alongside the new Drive fields.

## Verification

- [x] `pytest` — 147 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified fresh and via a manually seeded pre-migration
  `policy_versions` row (confirms the `captured_at` backfill, not just
  schema creation on an empty database).

## Decisions & Alternatives Rejected

- See ADRs #17/#18.
- Drive-link/capture/refresh, and the connector connect/disconnect, are
  all admin-only per the spec's explicit "An admin can associate a policy
  with a Drive file ID" wording — unlike ordinary GRC CRUD (vendors,
  risks, frameworks), these touch or exercise the org's shared Drive
  credential.
- `captured_filename` derives an extension from the Drive file's MIME type
  when its own `name` doesn't already carry a recognized one — found via
  a genuinely failing test (`test_capture_creates_immutable_version_with_
  provenance`), not a hypothetical edge case.

## Known Gaps / Follow-ups

- Drive Approvals API and optional Workspace Directory sync are the next
  commit on this branch.
- No scheduled/background Drive polling — capture and drift-check are
  both explicit admin actions ("Capture current version" /
  "Check Drive for changes"), matching the "explicit Sync now actions, no
  new queue" constraint.
- `list_revisions` is best-effort (returns `[]` on any HTTP error) per
  Google's own documented revision-history limitations — never presented
  as complete history.
