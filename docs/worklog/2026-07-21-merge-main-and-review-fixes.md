# Merge origin/main into PR #8 branch + address review findings

**Date:** 2026-07-21
**Author:** agent (claude)
**Type:** chore/fix

## Summary

Prepared PR #8 (`feat/admin-oauth-iam-consolidation`) for merge: confirmed
`origin/main` had not advanced since the branch diverged (merge was a
genuine no-op — no new commit, no conflicts), then addressed real
actionable findings from automated PR reviewers (Codex, Copilot) that
were unresolved on the pushed branch.

## What was verified

- `git merge origin/main --no-ff` → "Already up to date" (origin/main is
  an ancestor of HEAD; nothing to integrate).
- Alembic: single head (`d9af310aa79d`, "add updated_at to users" — landed
  on this branch since my last session), applies cleanly from an empty
  database via `python -m app.cli migrate`.
- Full test suite: 402 passed, 1 skipped (Postgres integration test, no
  local Postgres/Docker daemon available in this environment).
- `ruff check`/`ruff format --check`: clean.
- Live app boot + HTTP smoke test: login, Admin landing, Users,
  Authentication, Connections, Jobs, Audit Log all 200 for admin; all
  403 for a non-admin; anonymous 303; Trust Center isolation confirmed
  (public route returns a plain 404 with no authenticated-shell markup
  when nothing is published — no shared nav/session leakage).
- GitHub Actions CI (test, test-postgres, docker, GitGuardian) — see PR
  for final status at head `3a4bd40`.

## An incident during verification, caught and fixed

Raw `alembic upgrade head`/`alembic downgrade` ignore `GRC_DATABASE_PATH`
entirely — they read `sqlalchemy.url` directly from `alembic.ini`
(`sqlite:///./data/grc.db`), not through `app.config`/`app.db`. An early
verification attempt using `GRC_DATABASE_PATH=/tmp/... alembic ...`
therefore silently targeted the real local `data/grc.db` (a UAT scratch
database from prior sessions) instead of the intended scratch file — a
downgrade briefly dropped `users.updated_at` from that real dev database.
Caught immediately by checking `alembic_version` before/after; restored
via `python -m app.cli migrate` (which does correctly honor
`GRC_DATABASE_PATH`). No data of consequence was lost (UAT seed users,
timestamps only, immediately backfilled by the same migration). All
subsequent migration testing used `alembic.config.Config` with an
explicit `sqlalchemy.url` override in Python — the only way to safely
target an arbitrary SQLite file without touching `alembic.ini` or the
real path.

## Review findings addressed

Two were genuine crash bugs (500 errors), not style nits:

- **P1**: `admin_authentication.py`'s OAuth client-secret rotation reused
  a literal `Secret.name` on every save; `Secret.name` has a unique
  constraint, so the second rotation crashed with `IntegrityError`. Fixed
  by suffixing each save's secret name with a fresh id.
- **P2**: a Google identity matched by stable `google_subject` whose
  email changed to an address already owned by a *different* user
  crashed on `User.email`'s unique constraint instead of being rejected
  as a collision. Fixed to check for the conflicting owner first.

Plus three smaller, real fixes:

- Dashboard "Where things live" linked non-admins to admin-only pages
  (403 dead end) — now conditional on `role == "admin"`.
- Admin > Users page copy said Google sign-in links "by email"; the
  actual logic matches by `google_subject` first — copy corrected.
- `pending` users got the same "no longer active" message as `disabled`
  users at both login paths — now a distinct, accurate message.

## Known limitations (documented, not fixed this round)

- **Last-active-admin race** (`app/routers/admin_users.py::_active_admin_count`):
  under a multi-worker Postgres deployment, two concurrent admin
  demote/disable requests could each read "another active admin exists"
  and both commit, leaving zero active admins. Needs DB-level
  serialization (e.g. `SELECT ... FOR UPDATE` or an advisory lock) — a
  larger change than a review-fix pass, and a low-probability window at
  this app's single-tenant, low-concurrency scale.
- **`Cache-Control: no-store` applies to public pages too** (e.g.
  `/health`, public Trust Center) — correct for its stated purpose
  (prevent authenticated-page replay after logout) but suboptimal for
  pages that could otherwise be cached by browsers/CDNs. Left as-is
  pending a product decision on whether public pages should be
  independently cacheable.

## PR status

PR #8 was already converted from draft to ready-for-review by prior
work (not part of this session) before this merge-preparation task
began. It remains **open and unmerged** throughout this session, per
explicit instruction not to merge it.
