# Feature 11: Trust Center domain and administration

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Eleventh phase of the platform pivot (umbrella issue #5, PR #6),
"checkpoint 5" group (Trust Center administration alone). A publication
domain model (`TrustCenterSettings`, `TrustCenterSection`) plus an
admin-only administration UI: settings form, a register grid for the
section list, a dedicated editor for body/dates/linked records, a
preview using a new shared safe-Markdown renderer, and publish/unpublish
actions with full audit history. The public, unauthenticated route is
deliberately out of scope here — that's Feature 12.

## Files Changed

- `app/models.py` — `TrustCenterSettings`, `TrustCenterSection`,
  `TRUST_CENTER_SECTION_VISIBILITY`, `TRUST_CENTER_SECTION_STATUSES`.
- `migrations/versions/86f108a23aed_...py`.
- `app/trust_center.py` — `get_or_create_settings`, `publish_section`,
  `unpublish_section`, `is_stale`.
- `app/markdown_render.py` — shared `render_markdown_safe` (markdown +
  nh3), also used by the later public route.
- `app/routers/trust_center.py` — admin router + sections register
  config (`require_admin_for`).
- `app/templates/trust_center/` — `admin.html`, `section_detail.html`,
  `section_preview.html`.
- `app/routers/placeholders.py`, `app/main.py` — removed the
  "trust-center" placeholder entry, wired the new router, moved the
  nav link to `/trust-center/admin`.
- `pyproject.toml` — added `markdown`, `nh3`.
- `tests/test_markdown_render.py` (6), `tests/test_trust_center.py` (8),
  `tests/test_trust_center_router.py` (11), `tests/test_pages.py`
  (updated placeholder list + Bootstrap-shell check).

## Verification

- [x] Tests pass (`pytest` — 313 passed, 1 skipped)
- [x] Lint/format clean
- [x] Browser-verified the full admin flow against a live dev server
      (isolated scratch `GRC_DATA_DIR`): toggled settings and saved,
      created a section via the register grid, edited its Markdown
      body/links, opened Preview (confirmed sanitized HTML output —
      heading, bold, and link rendered; no raw Markdown or script
      content leaked), published it (status badge + "Last published"
      timestamp appeared), unpublished it, and confirmed the full
      create → update → publish → unpublish trail on `/audit-log`
      with the correct actor. No console errors.

## Decisions & Alternatives Rejected

- **Single latest-published snapshot, not a full version history.**
  Unlike `Policy`/`PolicyVersion`, a Trust Center section keeps only
  its current draft plus the most recent published snapshot on the
  same row; unpublishing never clears that snapshot, so re-publishing
  is a one-click restore. Full history is still visible via
  `AuditEvent` rows. This is lightweight CMS copy, not immutable
  compliance evidence — a second versioned table would be premature
  generalization for the one thing this feature needs.
- **Sections are admin-only end to end** (`require_admin` on the page
  router, `require_admin_for={"create","edit","delete"}` on the
  register config), matching the Connections precedent — publishing
  external-facing content is governance-sensitive even without a
  credential involved.
- **Body/dates/links are edited on a dedicated page, not grid cells.**
  The generic register PATCH endpoint (`app/registers/router.py`)
  has no date-parsing step — `_validate` only checks
  required/max_length/choices, not format — so a raw `date` FieldSpec
  round-tripping a string into a `Mapped[datetime.date]` column would
  be an untested path in the shared framework. Rather than extend that
  framework speculatively, dates/body/links go through a classic Form
  POST reusing the existing `_parse_date` pattern from
  `app/routers/vendor_systems.py`. Grid fields are limited to plain
  scalars (title/visibility/display_order) that the framework already
  handles safely.
- **Publish/unpublish are dedicated POST actions, not a raw `status`
  field edit** — so a snapshot is always taken correctly and audited,
  and an admin can never "fake publish" by editing a grid cell.
- **`markdown` + `nh3` (Rust/ammonia bindings) added as new
  dependencies** for safe rendering — no custom HTML parsing or
  sanitization regex written. `nh3` has no invented allowlist logic
  beyond an explicit safe tag/attribute/URL-scheme list.
- **Visibility enum includes "restricted"** in the schema now (per the
  Feature 11/12 spec) even though no gated-access workflow exists —
  only "public" will ever be shown to unauthenticated visitors once
  Feature 12 builds that route; "restricted" is reserved, not wired to
  any special access control yet.
- **`linked_policy_id` accepts any policy**, not just `status="approved"`
  ones — the UI documents that only approved policies are eligible for
  public download, but the actual enforcement of that constraint
  belongs to Feature 12's public rendering/download path, not the
  admin editor (an admin should be able to link a policy before it's
  approved, in preparation).

## Known Gaps / Follow-ups

- No public, unauthenticated route yet — Feature 12.
- No enforcement (yet) that only `visibility="public"` + `status=
  "published"` + non-stale sections would actually appear publicly —
  that logic belongs to Feature 12's read path, tested there.
- No drag-and-drop section reordering in the grid UI — `display_order`
  is a plain editable number column; multi-row bulk reordering could
  use the register framework's existing bulk-update endpoint later if
  needed.
