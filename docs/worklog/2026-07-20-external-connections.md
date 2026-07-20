# Feature 6: external database connections

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Sixth phase of the platform pivot (umbrella issue #5, PR #6), start of
"checkpoint 2". Adds `ExternalConnection` (postgres/mysql/sqlite/generic),
built on Feature 5's `Secret` foundation, plus an admin-only
administration UI: a read-only register grid for the list, plain
Bootstrap forms for create/edit/delete, and a bounded connection test.

## Files Changed

- `app/models.py` — `ExternalConnection` model.
- `app/connections.py` — `build_connection_url`, `run_connection_test`
  (fixed `SELECT 1` probe, bounded `connect_timeout`, sanitized failure
  messages, audits every test).
- `migrations/versions/bef6beccbfd1_add_external_connections_table.py`.
- `app/routers/connections.py` — admin-gated CRUD + test routes;
  read-only `CONNECTIONS_REGISTER_CONFIG` for the list grid.
- `app/templates/connections/list.html`, `form.html`.
- `app/main.py` — router wiring, new "Connections" nav item (distinct
  from the existing "Connectors" SaaS-integration placeholder).
- `pyproject.toml` — added `pymysql`.
- `tests/test_connections.py` (7), `tests/test_connections_router.py` (6).

## Security fixes found while building this feature

- **`app/registers/router.py::update_row`/`bulk_update` applied any key
  in the client's `fields` payload via `setattr`, not just declared
  editable field names.** Harmless for Controls/Requirements (no
  sensitive columns), but would have let a client PATCH an arbitrary
  column (e.g. `secret_id`) on a connections register. Fixed: `_validate`
  now rejects any payload key that isn't a declared editable field (or
  the register's `scope_field`) with 422, before any `setattr` runs.
  Regression test added; one existing test's assertion updated (it
  depended on the old, vulnerable silent-ignore behavior).
- `serialize()` crashed (`TypeError`) for a `read_only=True` field with
  no `compute` override — needed for Connections' plain read-only
  columns (name, db_type, host, ...). Fixed: falls back to `getattr`
  when `compute` is `None`.

## Verification

- [x] Tests pass (`pytest` — 259 passed, 1 skipped)
- [x] Lint/format clean
- [x] Manually verified in a real browser (isolated scratch data dir,
      test encryption key): admin-only nav item; create a postgres
      connection with a password; list grid shows no secret and no
      delete/add-row affordance; edit form pre-fills without exposing
      the stored secret; "Test connection" against a deliberately
      unreachable port fails within the bounded timeout with a
      sanitized message (`OperationalError: connection failed` — no
      password, no DSN); grid updates to reflect the failed test.

## Decisions & Alternatives Rejected

- Register grid used **only** for the read-only list (all fields
  `read_only=True`, `creatable=False`, `deletable=False`,
  `bulk_enabled=False`) — credentials and TLS config go through a plain
  server-rendered Bootstrap form, not JSON-API-editable grid cells, per
  the explicit instruction and because a credential field genuinely
  shouldn't be spreadsheet-editable.
- `run_connection_test` is the *only* SQL this feature ever executes — a
  fixed `SELECT 1`. No arbitrary-query capability exists anywhere, so
  "no unrestricted SQL interface for normal users" holds trivially (there
  is no query interface for anyone, admin included).
- Connection test runs synchronously (bounded to a few seconds) rather
  than through a job queue — per the spec, acceptable for now since
  Feature 7 (worker) lands immediately after and can move this
  server-side call into a job without changing its public contract.

## Known Gaps / Follow-ups

- MSSQL still deferred (ADR #24 — `pyodbc` needs a system ODBC driver).
- No non-admin browser check performed manually (covered by
  `test_connections_list_requires_admin`/`test_create_connection_as_regular_user_forbidden`
  instead — HTTP-level, equivalent coverage).
- Connection test is synchronous — Feature 7 should route it through the
  worker so a slow/hanging host doesn't tie up a request thread.
