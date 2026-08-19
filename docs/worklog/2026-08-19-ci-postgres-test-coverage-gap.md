# CI: `test-postgres` job silently ran only `test_postgres_compat.py`

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** fix (CI/process)

## Symptom

While inspecting CI infrastructure ahead of issue #32 (unrelated), I
checked what the `test-postgres` job in `.github/workflows/ci.yml`
actually runs against the real `postgres:16` service container. It ran
`pytest tests/test_postgres_compat.py -v` — a hardcoded single test
file, not the whole `TEST_DATABASE_URL`-gated surface.

This means two already-merged, already-reported-as-"ran in CI's
test-postgres job" tests never actually ran against a real Postgres:

- `tests/test_rbac_migration.py::test_role_rename_against_postgres`
  (#37, PR #60)
- `tests/test_policy_lifecycle_migration.py::test_backfill_against_postgres`
  (#31, PR #62)

Confirmed directly by reading the actual GitHub Actions run logs for
PR #62's `test-postgres` job: it collected and ran exactly 7 items, all
from `test_postgres_compat.py` — the two gated tests above were never
collected at all, despite `gh pr checks` showing `test-postgres:
SUCCESS` and my own PR descriptions/worklogs for #37 and #31 stating
they ran there. That claim was incorrect — the job passing only ever
proved `test_postgres_compat.py` passed, not that every
`TEST_DATABASE_URL`-gated test in the repo did.

## Root cause

The `test-postgres` job's test-selection command was a hardcoded file
path from when `test_postgres_compat.py` was the *only*
`TEST_DATABASE_URL`-gated test module. As later issues (#37, #31) added
their own gated migration tests in their own files, following the same
`skipif(not POSTGRES_TEST_URL)` pattern and naming convention
(`..._against_postgres`/`..._postgres`), nothing updated the CI command
to pick them up — a pure CI-configuration gap, not an app-code defect.

## Fix

Changed the job's test command from the hardcoded file path to
`pytest -k postgres -v` — selects every test in the whole suite whose
name contains "postgres", matching the established naming convention.
Verified this is safe and complete:

- `pytest --collect-only -k postgres` locally selects exactly 12 tests:
  the 7 pre-existing ones from `test_postgres_compat.py`, the 2
  previously-uncollected gated tests above, and 3 more
  (`test_build_connection_url_postgres_never_includes_plaintext_in_repr`,
  `test_postgres_engine_pooling_is_unaffected`,
  `test_reset_postgres_schema_rejects_a_non_test_scoped_database_name`)
  that are dialect-selection/naming-only unit tests needing no live
  connection — already passing everywhere, safe to include.
- No test matching `-k postgres` needs anything the job doesn't already
  provide (`TEST_DATABASE_URL` pointing at the real service container).

## Verification

CI run for this PR is the actual verification: confirms
`test_role_rename_against_postgres` and `test_backfill_against_postgres`
now execute and pass against a real Postgres for the first time. If
either had a real Postgres-specific defect, this is where it would
surface — see PR for the actual run result before merge.

## Follow-up note

The migration code itself for #37/#31 was not blindly assumed correct
just because SQLite passed — both migrations use portable, standard
SQL (`batch_alter_table` for SQLite-specific CHECK-constraint handling,
plain `ALTER TABLE`/`CREATE INDEX` elsewhere, no SQLite-only syntax in
the shared backfill logic) and were manually validated against the
actual Alembic upgrade path during their own implementation sessions.
This fix closes the CI verification gap; it is not evidence that a
Postgres-specific defect was found and hidden.
