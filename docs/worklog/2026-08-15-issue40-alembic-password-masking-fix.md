# Issue #40: fix PostgreSQL init_db password masking passed to Alembic

**Date:** 2026-08-15
**Author:** Claude (agent)
**Type:** fix

## Summary

`app/db.py::init_db` was passing `str(engine.url)` (SQLAlchemy's masked,
`***`-for-password rendering) into Alembic's `Config.set_main_option`, but
Alembic reparses that exact string into its own connection engine
(`migrations/env.py`), so any password-authenticated PostgreSQL deployment
would fail migrations trying to authenticate with the literal string
`"***"`. Fixing this surfaced a second, independent bug: Python's
`configparser` (which `Config.set_main_option` uses internally) treats a
bare `%` as invalid interpolation syntax, so any URL-encoded special
character in a real password (e.g. `%40` for `@`) would raise `ValueError`
regardless of the masking fix. Both are fixed together.

## Files Changed

- `app/db.py` — `init_db` now passes `engine.url.render_as_string(hide_password=False)`
  (the real credential) instead of `str(engine.url)`; a new
  `_escape_for_configparser` helper escapes `%` as `%%` for every value
  passed to `set_main_option` (both the `sqlalchemy.url` and
  `script_location` options — the latter had the identical hazard,
  unescaped, for any checkout path containing a literal `%`).
- `tests/test_postgres_compat.py` — two new always-run unit tests (no live
  Postgres needed): the real password (including URL-encoded special
  characters) reaches Alembic's config unmasked, and it never leaks into
  captured logs/diagnostics.

## Verification

- [x] Tests pass (`pytest`) — full suite green (see exact count in the
      PR description once CI confirms; 404 baseline+2 new locally).
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`) for
      every file in this change.
- [x] Manually verified: mutation-tested both new tests (revert the fix →
      test fails for the expected reason; injected a deliberate password
      leak into `init_db` → the leak test correctly fails; reverted →
      passes again). Also verified the leak test survives running after
      `tests/test_db_init.py` (which exercises the real, non-monkeypatched
      `init_db` → `alembic.env.py::fileConfig(disable_existing_loggers=True)`
      path that would otherwise silently disable the logger this test
      depends on for the rest of the session).

## Decisions & Alternatives Rejected

- **Fixed `script_location`'s identical unescaped hazard in the same
  commit**, even though it's pre-existing and not the issue's literal
  scope — same function, same bug class, and leaving one of two adjacent
  `set_main_option` calls unescaped after reasoning through the hazard
  for the other would be a confusing inconsistency for the next reader.
  Extracted a small `_escape_for_configparser` helper used by both.
- **Rewrote the leak-detection test after an independent adversarial
  review found it was vacuous as first written**: the original password
  (`"p@ssSuperSecret123!"`) contains characters `render_as_string`
  percent-encodes, so the *raw* string the test asserted against never
  appears anywhere a real leak would actually surface (only the *encoded*
  form does) — the test passed even with a deliberately injected leak.
  Fixed by using an encoding-safe password (matching
  `tests/test_credential_leakage.py`'s `SECRET_MARKER` convention: letters/
  digits/hyphens only, so raw and rendered forms are identical) and
  asserting against both the raw password and the fully-rendered URL.
  The same review found the test would also go silently inert for the
  rest of any real `pytest` run, regardless of password choice, because
  `migrations/env.py`'s `fileConfig(disable_existing_loggers=True)`
  permanently disables any logger not named in `alembic.ini`'s
  `[loggers]` section the first time a *real* `init_db` runs (which
  nearly every other test file triggers via the `app` fixture) — fixed by
  resetting `logging.getLogger("app.db").disabled = False` before
  capturing, verified this specifically defeats that ordering trap.
- **Did not change CI's `test-postgres` job to use password auth**
  (it currently uses `POSTGRES_HOST_AUTH_METHOD: trust`, a deliberate
  prior decision per that job's own comment, made specifically to avoid a
  GitGuardian false-positive on a hardcoded CI password). The two new
  tests intercept exactly what `init_db` passes to Alembic without
  needing a live server at all, which is a more precise proof of the
  fix's mechanism than an indirect "did authentication succeed"
  inference would be, and avoids reopening a security-posture question
  the team already deliberated once.

## Known Gaps / Follow-ups

- CI's live-Postgres job still uses passwordless trust auth, so it does
  not end-to-end-exercise real password authentication through
  `init_db`/Alembic. The two new unit tests are the actual proof for
  this fix; a follow-up could revisit password auth for CI if the team
  decides the GitGuardian trade-off is worth it, but that's a separate
  decision from this bug fix.
