# Feature 13: Final hardening and integration

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** fix/test/docs

## Summary

Final phase of the platform pivot (umbrella issue #5, PR #6) — the
second half of "checkpoint 6" (Public Trust Center and final
hardening). Four parallel specialist reviews (security, database/
migrations, general code quality, accessibility) ran against the full
`feat/platform-trust-center-pivot` diff. Every CRITICAL/HIGH finding
that survived cross-checking against the actual code was fixed with a
regression test; MEDIUM/LOW findings were triaged and either fixed
(when cheap) or explicitly documented as an accepted gap below.

## Review process

Four independent review agents ran in parallel against `main...HEAD`:
security (secrets, IDOR, XSS, CSRF, SQL), database (SQLite/Postgres
portability, migrations, indexes, the job queue's concurrency
guarantees), general code quality (dead code, silent failures, worker
retry/idempotency correctness), and accessibility (WCAG 2.2, focused
on the new public-facing page). Two of the four independently
surfaced the same CRITICAL job-claim race from different angles,
which is the strongest signal in this pass — cross-verified findings
were prioritized over single-source ones.

## Findings fixed

1. **CRITICAL — TOCTOU race in stale-running job reclaim**
   (`app/jobs.py`). The guarded UPDATE that claims a job only checked
   `status IN ('pending', 'running')`, not the staleness predicate the
   candidate SELECT used. Under PostgreSQL with concurrent workers, a
   second worker's blocked UPDATE could still match after the first
   worker's commit refreshed `claimed_at`, letting both workers run
   the same job's handler. Fixed by repeating the full candidate
   predicate in the UPDATE's WHERE clause (`_try_claim`/
   `_candidate_predicate`). SQLite's single-writer locking had masked
   this in every prior test run — the regression test exercises the
   UPDATE predicate directly rather than through real concurrency,
   since SQLite can't reproduce the Postgres MVCC re-evaluation
   behavior that triggers the bug.

2. **HIGH — register-grid list endpoint had no admin gate**
   (`app/registers/router.py`, `app/registers/config.py`). Any
   authenticated non-admin user could `GET /api/registers/connections`
   or `/api/registers/trust-center-sections` and read
   host/database_name/owner/status or section metadata, contradicting
   both routers' "admin-only end to end" docstrings — `require_admin_for`
   was only ever checked for create/edit/delete, never list. Extended
   `RegisterAction` to include `"list"` and wired it into `list_rows`;
   set it on both configs.

3. **HIGH — missing CHECK constraints and hot-path indexes**
   (`app/models.py`, new migration `363c1c5fe38b`). `trust_center_sections`
   was the only enum-like-column table added this pivot without CHECK
   constraints on its enum columns. Added them, plus a composite index
   on `(visibility, status)` (the exact predicate the new
   unauthenticated public route filters on every request) and
   composite indexes on `jobs(status, available_at)` /
   `jobs(status, claimed_at)` (the predicates the worker's claim query
   runs on every poll cycle). Verified the migration applies and
   reverses cleanly (upgrade → downgrade -1 → upgrade) against a
   scratch database.

4. **HIGH — Postgres CI coverage gap** (`tests/test_postgres_compat.py`).
   The live-Postgres migration test only ever asserted the original
   MVP schema's tables existed and inserted into exactly one of them.
   None of the 5 tables added by this pivot (Secret, ExternalConnection,
   Job, ImportJob, TrustCenterSettings, TrustCenterSection) were ever
   created against a real Postgres server in CI — a dialect-specific
   CHECK constraint or default that failed to compile there would have
   gone undetected. Added a table-existence assertion plus an
   ORM-level insert through all 5 tables, including confirming the new
   visibility CHECK constraint rejects an invalid value there too.

5. **Accessibility — unclamped Markdown heading levels** (real
   WCAG 1.3.1 issue). Admin-authored `# Heading` in Trust Center
   content had no knowledge of where it would be embedded, producing
   a competing top-level `<h1>` inside the page's own heading
   structure — a broken outline for screen readers navigating by
   heading list. `render_markdown_safe` now accepts a `heading_offset`
   that shifts h1-h6 down (clamped at h6); wired to +1 for
   intro/preview content (sitting directly under a page `<h1>`) and +2
   for section bodies (sitting under both a page `<h1>` and the
   section's own `<h2>`).

6. **Accessibility — register-grid ambiguity and silence** (shared by
   every Tabulator grid in the app: controls, requirements,
   connections, Trust Center sections). The actions column header was
   blank and every row's delete button read identically "Delete" with
   no way to distinguish rows for screen-reader users; successful
   edits/adds/deletes had no status announcement at all. Delete
   buttons now get a per-row `aria-label`, the header reads "Actions",
   and a shared polite `aria-live` region announces outcomes.
   Browser-verified: correct `aria-label` per row, visible header
   text, and the live region updates on a real cell edit with no
   console errors.

## Findings triaged and accepted (not fixed)

- **MEDIUM — `ImportJob` idempotency has no DB-level unique
  constraint**, only an application-level SELECT-then-check before
  insert. A true fix requires a partial/filtered unique index scoped
  to `status='completed'` (syntax differs enough between SQLite and
  Postgres to add real migration complexity) for a narrow race window
  (two near-simultaneous imports of the identical file). Given low
  probability and low impact (a redundant completed import record, not
  a security bypass), documented here as an accepted gap rather than
  fixed under time pressure — worth revisiting if watched-directory +
  manual-upload races are ever observed in practice.
- **MEDIUM — `run_import` (`app/imports.py`) is a ~127-line function**
  handling job-row creation, validation, checksum/duplicate detection,
  dispatch, and exception-driven rollback in one place. Flagged as
  worth splitting for maintainability; no correctness issue, so left
  alone per "don't refactor unless required for the current task."
- **Missing indexes on other new foreign-key columns**
  (`external_connections.secret_id`, `trust_center_sections`'s
  `linked_framework_id`/`linked_policy_id`/`published_by_user_id`,
  `trust_center_settings.updated_by_user_id`) — consistent with the
  same unindexed-FK pattern already present throughout the
  pre-existing schema (e.g. `FrameworkRequirement.framework_id`,
  `ControlRequirementMapping`'s FKs). Fixing only the new tables'
  low-traffic FKs while leaving identical older ones alone would be
  inconsistent without a concrete performance signal; the two indexes
  that were added target the specific new hot paths (an unauthenticated
  public route, a continuously-polling worker), which is where an
  unindexed scan actually matters at this app's scale.
- **Register-grid Tabulator a11y limitations** (div-based grid, not a
  native `<table>`; primarily mouse-oriented default cell editors) —
  flagged by the accessibility review as a structural limitation of
  the underlying library, not something any template change resolves.
  Recommend a manual VoiceOver/NVDA + keyboard-only pass before calling
  any register grid AA-compliant; out of scope to replace the grid
  library in this pass.

## Verification

- [x] `pytest` — 335 passed, 1 skipped (up from 291 at the start of
      Feature 10)
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] `bandit -r app -ll` — 0 medium/high findings (4 pre-existing low/
      false-positive findings reviewed: OAuth token URL and empty-string
      defaults heuristically matched as "hardcoded passwords" — not
      related to this pivot, no action needed)
- [x] `pip-audit` — no known dependency vulnerabilities
- [x] `helm lint`/`helm template` — clean against both default and
      production values (re-verified after all Feature 11-13 commits)
- [x] `docker compose -f compose.yaml config` — validates cleanly
- [ ] `docker build`/full container run — Docker daemon not available
      in this environment; CI's own `docker` job has passed on every
      push this session, which is the actual build verification
- [x] Alembic migration upgrade/downgrade/upgrade cycle verified
      against a scratch database for the new hardening migration
- [x] Browser-verified the register-grid accessibility fixes (aria-label,
      header text, live-region announcement) against a live dev server

## Known Gaps / Follow-ups (repo-wide, at PR close)

- ImportJob idempotency race window (see above).
- `run_import`'s length (see above).
- Broader FK indexing across the whole schema, old and new (see above).
- No live Kubernetes cluster deployment test (carried over from
  Feature 10 — `helm lint`/`helm template` only).
- No pinned per-publish policy version for Trust Center downloads
  (carried over from Feature 12).
- Register-grid Tabulator's inherent a11y ceiling (see above) — a
  manual assistive-tech pass is recommended before external release.
