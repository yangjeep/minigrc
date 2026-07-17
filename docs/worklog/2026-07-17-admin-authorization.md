# Minimal admin authorization

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

First slice of `feat/startup-compliance-operations`: a binary `admin` /
`user` role on `User`, needed because upcoming work (Google Drive OAuth
tokens, AWS role ARNs, vendor roster imports) introduces credential-adjacent
surfaces that shouldn't be open to every logged-in user. No general RBAC —
see ADR #12.

## Files Changed

- `app/models.py` — `USER_ROLES`, `User.role` (default `"user"`).
- `app/deps.py` — `require_admin` dependency (403 if `user.role != "admin"`).
- `app/cli.py` — `create_user` promotes the first-ever user to admin
  automatically; new `promote_admin(email)` / `promote-admin` CLI command
  grants admin to an existing user without ever accepting a password, and
  writes an audit event (`actor="cli"`).
- `migrations/versions/ad57f3b48bfb_*.py` — adds `users.role` with
  `server_default='user'` so existing rows (including a pre-existing sole
  user) backfill safely; promote via CLI after upgrading.
- `tests/conftest.py` — `admin_user` / `admin_client` fixtures for future
  admin-gated route tests.
- `tests/test_admin.py` — first-user-becomes-admin, promote (grants role +
  audits, rejects unknown email, idempotent no double-audit),
  `require_admin` accept/reject.
- `docs/decisions/architectural-decisions.md` — ADR #12.

## Verification

- [x] `pytest` — 72 passed
- [x] `ruff check .` / `ruff format --check .` — clean
- [x] Migration verified on a fresh database and via upgrade from the
  `feat/initial-grc-foundation` schema head.

## Decisions & Alternatives Rejected

- Auto-admin for the first CLI-created user rather than requiring a manual
  promote step for that specific case — the spec calls for "a safe
  migration/bootstrap path," and forcing an extra manual promote for the
  only user in a brand-new deployment doesn't reduce risk, just friction.
  A pre-existing deployment upgrading into this migration still needs the
  explicit `promote-admin` command, since the migration can't know which
  of possibly several existing users should become admin.
- Did not gate any *existing* routes (frameworks/risks/controls/policies)
  behind `require_admin` — those aren't credential or integration surfaces,
  and gating them wasn't requested.

## Known Gaps / Follow-ups

- `require_admin` isn't wired into any route yet — no admin-only route
  exists until the Vendor/Google/AWS work in later commits on this branch.
