# Issue #38: Headless dev/UAT mode for CLI agents and CI

**Date:** 2026-08-18
**Author:** Claude (agent)
**Type:** feat

## Summary

Adds a headless UAT harness (`tests/uat/`) that drives the real FastAPI
app over a real TCP socket (`uvicorn` in a background thread + a real
`httpx.Client`, not an in-process `TestClient` and not a browser) so CLI
agents and CI can run realistic end-to-end acceptance scenarios without
Claude Desktop. Covers the required #11/#12 representative scenarios
(SOC 2 primary/ISO preserved, shared-control mapping, full control
occurrence lifecycle including a deterministic fake evidence provider,
and an authorization/CSRF failure-closed scenario). Excluded from the
default `pytest` run (`addopts = "-m 'not uat'"`); run explicitly via
`GRC_UAT_MODE=1 pytest -m uat tests/uat` or `python -m app.cli uat`.

Adversarial review of this work surfaced a real, pre-existing SQLite
read-after-write correctness gap in `app/db.py`, independent of this
issue — filed as **issue #55** (see below).

## Files changed

- `app/main.py` — `create_app` gains an optional `database_url` parameter
  (mutually exclusive with `database_path`/`data_dir`) so the harness can
  point the real app at an explicit PostgreSQL URL without mutating the
  ambient `DATABASE_URL` environment variable or the process-wide
  `get_settings()` cache.
- `app/cli.py` — new `uat` subcommand: sets `GRC_UAT_MODE=1`, lazily
  imports `pytest` (never required for `migrate`/`create-user`/etc. in a
  minimal production install), and shells to
  `pytest -m uat tests/uat`.
- `tests/uat/` (new package) — `harness.py` (`LiveServer` real-socket
  runner, `build_uat_app`, `RequestRecorder`, `redirect_path`,
  `extract_csrf_field`, `csrf_header_value`, `create_uat_user`,
  `poll_for_visibility`, `assert_uat_mode_enabled`), `conftest.py`
  (`uat_server`/`uat_client` fixtures, failure-artifact hook),
  `test_soc2_control_lifecycle.py` (scenarios 1-10), and
  `test_authorization_boundaries.py` (scenario 11).
- `tests/test_uat_harness_safety.py` (new, normal suite) — static/live
  proof that `app/` never references the harness (except the sanctioned
  lazy-import `cli.py uat` command), the Dockerfile never copies `tests/`
  or invokes the `uat` subcommand, the harness only activates with
  `GRC_UAT_MODE=1`, `reset_postgres_schema` refuses a non-test-scoped
  database name, and a passing/failing synthetic headless scenario exits
  0/non-zero with a written failure artifact.
- `tests/test_create_app_database_url.py` (new) — unit tests for the new
  `create_app` parameter.
- `pyproject.toml` — `uat` pytest marker + `addopts = "-m 'not uat'"`.
- `.github/workflows/ci.yml` — headless UAT step added to both the
  SQLite (`test`) and PostgreSQL (`test-postgres`) jobs.
- `.gitignore` — `artifacts/` (headless UAT failure artifacts).
- `.agent/TESTING.md`, `.agent/LOOP.md`, `CLAUDE.md` — headless UAT
  documented as a distinct, required layer alongside Claude Desktop UAT.
- `docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md`
  (new) — full design, including §2's real-socket-vs-browser decision and
  §10's post-implementation adversarial-review finding.

## Design decisions

- **Real socket HTTP, not a browser.** The app's required scenarios are
  server-rendered forms plus one JSON register-grid API with no
  client-executed logic; `uvicorn` + `httpx.Client` against a real port
  is strictly more "real" than the existing `TestClient`-based tests
  while adding no new dependency (both already in `pyproject.toml`) and
  no browser runtime.
- **Pytest as the runner**, not a bespoke test-running harness — reuses
  markers/`-k` selection/`--junitxml` instead of inventing parallel
  infrastructure.
- **Every scenario id comes from a real HTTP response** (redirect
  Location, rendered link, register-grid JSON) — not a side-channel
  `session_factory()` read. This was always intended for faithfulness to
  real usage; adversarial review (see below) confirmed it also
  structurally avoids most of a real, separate SQLite bug.

## Bug found during verification: SQLite read-after-write gap (issue #55)

**Capture:** while stress-testing the harness, a side-channel DB read
immediately after an HTTP write intermittently returned nothing for a
row the write had already succeeded at creating (~30-50% miss rate in a
tight loop).

**Reproduce:** confirmed with *no* side-channel access at all — a bare
`POST /people` followed by a `GET` on the redirect target (exactly what a
browser does) missed the just-created row 1/60 times in an isolated run.
Every miss resolved on retry within ~50ms.

**Root cause:** `app/db.py`'s SQLite engine has no explicit `poolclass`
→ SQLAlchemy's default `QueuePool` (5 connections) + `check_same_thread:
False` + WAL mode means a request can be served by a pooled
connection/thread that hasn't yet observed a very recent commit made on
a different one.

**Classification:** real, pre-existing, SQLite-only (PostgreSQL's MVCC
is immune), present on the exact single-process `uvicorn app.main:app`
deployment the `Dockerfile` runs — not a harness artifact, not specific
to multiple workers/tabs.

**Fix status:** **not fixed here** — filed as
[issue #55](https://github.com/yangjeep/minigrc/issues/55), explicitly
out of scope for #38 (a real fix, e.g. constraining the SQLite engine to
a single physical connection, has throughput/concurrency tradeoffs that
need their own measurement, not a decision folded into this PR).
`tests/uat/harness.py::poll_for_visibility` is an honest bounded-retry
stopgap for the one unavoidable non-UI-visible check in this PR's own
scenarios (the evidence-occurrence link), not a fix, and is documented as
such.

**Regression coverage:** the reproduction is documented in issue #55 for
whoever picks it up; it is intentionally not duplicated as a normal-suite
regression test here, since adding one now would either (a) be flaky by
the bug's own nature or (b) require the fix this issue explicitly defers.

## Verification

- **Unit tests:** `tests/test_create_app_database_url.py` (4, new) —
  `create_app(database_url=...)` behavior/mutual-exclusivity; PASS.
- **Regression tests:** `tests/test_uat_harness_safety.py` (14, new,
  normal suite) — production-safety guarantees, exit-code/diagnostics
  proof, the `reset_postgres_schema` guard; PASS.
- **Full regression suite:** `pytest` (default, `uat`-excluded) — 528
  passed, 1 skipped, 4 deselected (the uat tests), 0 failed.
- **Integration/backend:** headless UAT exercises real DB/event/
  persistence paths through the live app (SQLite locally; PostgreSQL via
  CI's `test-postgres` job, not locally executable in this sandbox — no
  local Postgres/docker available here).
- **Headless UAT:** `GRC_UAT_MODE=1 pytest -m uat tests/uat` — 3 passed
  (lifecycle[sqlite], both authorization-boundary scenarios), 1 skipped
  (lifecycle[postgres], no local `TEST_DATABASE_URL`). Stress-tested 15+
  consecutive runs with zero flakes after the id-sourcing/poll fixes.
- **Browser/E2E:** N/A beyond headless UAT itself — no new user-facing
  routes/templates were added by this issue.
- **Claude Desktop UAT:** PENDING — Claude Desktop was not available in
  this environment; headless UAT above is the required, non-skippable
  substitute per the updated `.agent/TESTING.md` §1.5.
- **Lint/format:** `ruff check .` / `ruff format --check .` — clean.
- **Security/integrity:** production-safety proven mechanically
  (`tests/test_uat_harness_safety.py`): no `app/` module (other than the
  sanctioned, lazy-import `cli.py uat` command) references the harness;
  the Dockerfile never copies `tests/` or installs `[dev]` extras and
  never invokes the `uat` subcommand; the harness refuses to build
  anything without `GRC_UAT_MODE=1`; it never reads
  `GRC_DATA_DIR`/`DATABASE_URL` from the ambient environment for its own
  app instance; `reset_postgres_schema` refuses a non-test-scoped
  database name.
- **Bugs found:** one (issue #55, above) — real, root-caused, filed,
  explicitly deferred; the in-scope mitigation
  (`poll_for_visibility` + id-sourcing from real HTTP responses) is
  covered by the passing headless UAT runs themselves.

## Known deferred/untested paths

- PostgreSQL headless UAT path is implemented and will run in CI's
  `test-postgres` job, but was not directly executed in this session (no
  local PostgreSQL/Docker available in this sandbox).
- Issue #12 is still open on GitHub despite its PR (#54) being merged —
  unrelated housekeeping noticed during this issue's dependency check,
  not fixed here.
