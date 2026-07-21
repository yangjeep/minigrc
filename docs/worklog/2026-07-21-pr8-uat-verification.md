# PR #8 UAT and merge-readiness verification round

**Date:** 2026-07-21
**Author:** agent (claude)
**Type:** fix / test

## Summary

Final UAT round for PR #8 (Metabase-inspired Admin, Google OAuth, IAM,
and Connections consolidation — issue #7), using a real browser
(Claude Code's `Claude_Browser` tool, a Playwright-backed pane —
not the previously-broken `chrome-extension://` path) against a
freshly-provisioned clean environment. Found and fixed 9 real defects
that HTTP-level checks in the prior UAT round had missed, each with a
regression test. This round's own `chrome-extension://`-equivalent
problem was the browser tool's click/keypress delivery degrading
partway through a long interactive session — worked around by opening
fresh tabs, not by retrying the same broken path.

## Environment

- Clean checkout of `feat/admin-oauth-iam-consolidation` at a separate
  worktree (`~/orca/workspaces/minigrc/dev`), fresh venv, fresh SQLite
  DB, migrated from empty.
- Seed users: `admin@uat.local`, `admin2@uat.local`, `user@uat.local`,
  `disabled@uat.local` (status=disabled), `pending@uat.local`
  (status=pending, google_subject set) — all password
  `UatTest!2026`, created via a throwaway `scripts/uat_seed_users.py`
  (not committed).
- `GRC_ENCRYPTION_KEY` and `GRC_PUBLIC_BASE_URL` set in a local
  `.env` (gitignored) to exercise Google OAuth admin config and
  connection secret encryption end-to-end.
- Live Google OAuth: **blocked, no product failure.** No disposable
  Google OAuth test credentials/redirect URI were available in this
  session. The complete Admin configuration UI (unconfigured →
  configured → masked-secret states) was exercised end-to-end; the
  callback/negative-path logic is covered by the existing
  `tests/test_google_oidc*.py` suite (46 tests, all passing).

## Defects found and fixed (each: RED test → fix → GREEN → full suite/lint → commit → push → re-verify)

1. **`4fa115c`** — Back button after Sign out replayed a cached
   authenticated page (no `Cache-Control` header anywhere except the
   Trust Center public page). Added `Cache-Control: no-store` to the
   existing CSRF middleware for all non-static responses. Server-side
   session revocation was already correct (`test_logout_invalidates_session`
   passed); this was purely a client-cache replay.
2. **`a2308d8`** — Google OAuth admin page showed "Not configured"
   with zero explanation even when every visible field (Client ID,
   secret, domains) was filled in — the missing piece was
   `GRC_PUBLIC_BASE_URL`, an env var never surfaced in the UI. Added
   a reason line under the status badge.
3. **`fc5e9c0`** (critical) — The login page's "Sign in with Google"
   link only checked the legacy env-var-derived
   `Settings.google_oidc_enabled`, never the DB-backed
   `GoogleOidcSettings` row the new Admin UI actually writes to. An
   admin configuring Google OAuth entirely through Admin > Authentication
   (the PR's stated "only first-class SSO path") got a login page with
   no Google link at all. Fixed to use the same `resolve_google_oidc_config()`
   already used everywhere else in the feature.
4. **`d9ac45d`** — Login page presented local email/password as the
   prominent default (full form, big button) and Google as a small
   text link below it — inverted from the PR's own stated design
   (Google primary, local is break-glass). Reordered and styled.
5. **`ddcc686`** (critical) — Admin > Users showed a red "Error" badge
   and an empty table in the browser: `GET /api/registers/admin_users`
   500'd. `app/registers/router.py::serialize()` unconditionally reads
   `row.updated_at` for optimistic concurrency, and `User` was the only
   model wired into the register-grid framework without that column
   (every other model has it). Added `users.updated_at` +
   Alembic migration (backfilled from `created_at`, batch-mode NOT NULL
   tightening — SQLite rejects a non-constant `ADD COLUMN` default).
   This class of bug is invisible to "hit every route, expect 200"
   checks since the page shell still returns 200.
6. **`81f145b`** — Users list Status column rendered as unstyled plain
   text; every other status column in the app renders `badge badge-{status}`.
   Added a Tabulator cell formatter plus `badge-pending`/`badge-disabled`
   CSS classes (no prior rule existed for those values).
7. **`772ece0`** — Connections index cards showed raw
   `datetime.isoformat()` timestamps with microseconds instead of the
   `%Y-%m-%d %H:%M UTC` format used elsewhere. Added a shared formatter
   reused by all three card builders (external DB, AWS, Google Drive).
8. **`d649df9`** — The newly-added Admin > Jobs list had the identical
   two defects as #6/#7 above (same register-grid framework): raw
   timestamps and an unstyled Status column. Same fixes applied,
   plus `badge-succeeded`/`badge-running`/`badge-failed` classes.
9. **`c45c3cc`** (security-relevant) — The Dashboard's pre-existing
   "Recent audit activity" widget queries the 10 most recent
   `AuditEvent` rows with no role check, shown to *any* logged-in
   user. This PR writes new, more sensitive event types into that
   same table (Google OAuth secret creation, user role/status
   changes) — so the PR's own stated "Audit Log moved to admin-only,
   a deliberate tightening" was undermined by a pre-existing,
   unfiltered path to the same data. Gated the query and the section
   to admin only.
10. **`a5fe648`** — The "Trust Center" nav item (`/trust-center/admin`,
    `require_admin`-gated) was shown to every logged-in user, unlike
    the "Admin" link right next to it which the PR itself already
    gates by role — a non-admin clicking it hit a 403 dead end.
    Applied the same role check.

## Verified working (no defects)

- Self-disable safeguard ("You cannot disable your own account.").
- Last-active-admin protection (self role-demotion when sole active
  admin: "At least one active admin must remain — promote another
  user first.").
- Mid-session disable revokes access immediately (verified via curl:
  200 → disable → 303 to login on the very next request with the same
  cookie).
- Disabled-user login rejected with a generic, non-sensitive message.
- Anonymous GETs to every Admin page redirect to `/login` (303); direct
  POST/PATCH mutation attempts correctly 403 once past CSRF.
- 403 error page leaks nothing (generic "Admin privileges required").
- Connection secret never appears in rendered HTML, audit log, or job
  history (spot-checked via `curl | grep` on the raw response body).
- Public Trust Center: completely separate shell, zero nav/user-menu/
  admin leakage, confirmed via full DOM dump (`read_page filter=all`)
  at desktop and 390×844 mobile widths, both while enabled with real
  content and while disabled (clean 404, no stack trace).
- Admin mobile offcanvas nav: opens, active state correct, closes
  cleanly, focus returns to the trigger button.
- Keyboard-only login: visible focus ring throughout, logical tab
  order (Email has `autofocus` when Google is unconfigured, so the
  first Tab correctly lands on Password, not a skip).
- Full main-app smoke test (Dashboard, Frameworks, Controls, Policies,
  Risks, Evidence, Actions, People, Vendors, all five new Admin pages)
  — every page 200s post-fixes.

## PostgreSQL verification

Not run locally (no local Postgres/Docker available in this session).
Per the task's explicit fallback, inspected GitHub Actions at the exact
final pushed head (`a5fe648`): `test-postgres` job passed in 48s, log
confirms 5 real (non-skipped) tests including
`test_migrations_apply_cleanly_against_postgres`. All 4 CI checks
(`docker`, `test`, `test-postgres`, `GitGuardian Security Checks`)
green at that head.

## Clean-checkout final gate

Second, independent worktree at the exact remote head (`a5fe648`),
fresh venv, `pip install -e ".[dev]"`, migrations applied from an
empty DB, full suite (400 passed, 1 skipped — the local-only Postgres
integration test, expected), `ruff check .` / `ruff format --check .`
clean, server boots and responds. Worktree had zero unexpected diff;
local HEAD == remote HEAD throughout.

## Known limitation: screenshot artifacts

Extensive live browser verification was performed (desktop 1280×800,
mobile 390×844) and visually reviewed in-session for every required
scenario, but the browser tool available in this session has no
mechanism to persist a screenshot to disk — only to return it inline
for inspection. No PNG files could be produced or attached to the PR
from this environment. This is a tooling gap in this session, not a
product defect; flagged explicitly per the task's instruction to
separate legitimate external blockers from product failures.

## Decisions & Alternatives Rejected

- Fixed the Dashboard audit-visibility leak (#9) even though
  `dashboard.py` predates this PR, because this PR is what makes the
  leaked data meaningfully more sensitive (OAuth secrets, role
  changes) and directly contradicts a headline claim in the PR's own
  description. Did not touch any other pre-existing dashboard
  behavior.
- Did not attempt a broader visual redesign of the (currently
  unstyled) login page beyond the primary/break-glass ordering fix —
  out of scope per explicit instruction against broad aesthetic
  changes.
- Chose `strftime("%Y-%m-%d %H:%M UTC")` for all new timestamp
  formatting to match the one existing precedent
  (`connections/form.html`'s "Last test" line) rather than inventing
  a new format.

## Known Gaps / Follow-ups

- Live Google OAuth end-to-end flow (real IdP round-trip) remains
  unverified — needs disposable test credentials and an approved
  redirect URI, tracked as an external blocker, not a product gap.
- AWS/Google Drive connector *add* forms were not exercised
  interactively in this round (the browser tool's click reliability
  degraded before reaching them); both are pre-existing features with
  58 combined passing tests (`test_aws_connector.py`,
  `test_google_drive.py`) and were not touched by this PR.
- Screenshot artifacts for the PR description/comment — see above.
