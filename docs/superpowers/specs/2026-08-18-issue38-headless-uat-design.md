# Issue #38: Headless dev/UAT mode for CLI agents and CI

Status: implemented in this issue's own scope.

## 1. Repository-reality check

Verified before design:

- Issues #22 (event store), #11 (control operations), #12 (SOC 2 primary
  framework) are merged into `main` (PR #52, #52-adjacent, #54). #12's
  GitHub issue is still open despite its PR being merged — flagged as a
  housekeeping item, not a blocker for #38.
- No headless-browser or real-socket HTTP automation exists anywhere in
  the repo today. Every existing test (`tests/*.py`) uses FastAPI's
  `TestClient`, which drives the real `app` object in-process over an ASGI
  transport (no real socket, no separate process). This already exercises
  real routing/CSRF/session/domain/persistence code — it is not a mock —
  but it never crosses a real process/socket boundary the way a deployed
  server or a browser would.
- The app is almost entirely server-rendered forms with redirects
  (`app/routers/*.py` return `TemplateResponse`/`redirect_with_flash`).
  The one JS-driven exception is the generic register grid
  (`app/registers/router.py`, Tabulator.js on the frontend) — a plain
  JSON API (`GET/POST/PATCH/DELETE /api/registers/<name>`) authorized and
  CSRF-protected the same way as the HTML routes (`verify_csrf_header`).
  Calling that JSON API directly with a real `X-CSRF-Token` header is
  exactly what the browser's JS does — not a bypass.
- `app/cli.py`'s docstring ("kept to the two commands this app actually
  needs") is already stale — six commands exist. Adding a `uat` command
  does not introduce a new precedent.
- The `Dockerfile` only copies `app/`, `migrations/`, and `alembic.ini`
  into the production image, and `pip install .` (no `[dev]` extras) is
  the production install path. `tests/` is never present and `pytest` is
  never installed in the shipped image — this is a structural guarantee,
  not a runtime flag, that no UAT code can execute in production.
- `httpx` and `uvicorn` are already base dependencies (`pyproject.toml`);
  no new dependency is required for a real-socket HTTP harness.
- No manual evidence-upload route exists yet; the only place
  `EvidenceSnapshot` rows are constructed today is
  `app/aws_connector.py::build_evidence_snapshot`, fed by
  `check_cloudtrail`/`check_iam`. The occurrence-performance form
  (`app/routers/occurrences.py`) already accepts an `evidence_snapshot_id`
  to link an existing snapshot via `app/control_occurrences.py::link_evidence`.

## 2. Decision: real HTTP over a real socket, not a browser

The issue's candidate shape suggests "Playwright or an equally lightweight
maintained option" OR "an equivalent real HTTP+HTML automation approach
appropriate to the repo." Given the app has no client-executed JS logic
in any of the required MVP scenarios (register-grid AJAX calls are the
only JS, and they're callable directly as real JSON+CSRF requests),
adding Playwright would mean a new heavy dependency, browser-binary
download/install, and materially more CI/agent-sandbox flakiness for zero
additional realism in the scenarios this issue requires.

**Decision:** drive the real app over a real TCP socket with `uvicorn`
serving `app.main.create_app()` in a background thread, and `httpx.Client`
issuing real HTTP requests against `http://127.0.0.1:<port>`. This is
strictly more "real" than the existing `TestClient`-based tests (crosses
an actual process/socket boundary, matches how Claude Desktop or a real
CI-run server is hit) while adding no new dependency and no browser
runtime. If a future scenario needs real JS execution, a browser-based
runner can be layered in later without changing this harness's contract
(same live server + fixture setup, different driver).

## 3. Harness architecture

New `tests/uat/` package, excluded from the default `pytest` run:

```
tests/uat/
  __init__.py
  harness.py        # LiveServer (real socket uvicorn), UatSession helpers
  conftest.py        # fixtures: uat_server, uat_client, uat_db (sqlite/postgres)
  test_soc2_control_lifecycle.py   # scenarios 1-10
  test_authorization_boundaries.py # scenario 11
```

`tests/uat/harness.py`:

- `assert_uat_mode_enabled()` — raises unless `GRC_UAT_MODE=1` is set in
  the environment. Called first thing by every fixture that builds an
  app/server. This is defense-in-depth on top of §4's structural
  guarantee, not the primary safety mechanism.
- `LiveServer` — a context manager: builds a `uvicorn.Config`/`Server`
  bound to `127.0.0.1:0` (OS-assigned free port), runs `server.serve()` in
  a background thread via its own event loop, polls `server.started`,
  yields the resolved base URL, and on exit sets `server.should_exit =
  True` and joins the thread with a bounded timeout.
- `build_uat_app(backend, tmp_path)` — SQLite: always calls
  `create_app(database_path=str(tmp_path / "uat.db"))`, exactly like
  `tests/conftest.py`'s existing isolation pattern — it never reads
  `GRC_DATA_DIR`/`DATABASE_URL`/`GRC_DATABASE_PATH` from the ambient
  environment. Postgres: requires `TEST_DATABASE_URL` (same env var
  `tests/test_postgres_compat.py` already uses) and calls the new
  `create_app(database_url=...)` parameter (§5); refuses to run if
  `TEST_DATABASE_URL` is unset, never falls back to ambient `DATABASE_URL`.
- `RequestRecorder` — an `httpx` event-hook pair recording the last N
  (method, url, status, truncated body) tuples per test, used for failure
  artifacts.
- `create_uat_user(session_factory, *, role)` — deterministic fixture
  user creation via the same `hash_password`/ORM-insert pattern
  `tests/conftest.py` already uses for `test_user`/`admin_user`, not a new
  mechanism.

`tests/uat/conftest.py`:

- `pytest_collection_modifyitems`/marker registration: every test in
  `tests/uat/` is auto-marked `uat`. `pyproject.toml` sets
  `addopts = "-m 'not uat'"` so plain `pytest` (the existing CI job,
  the existing documented command) is unaffected and does not pay the
  cost of spinning up real servers; `pytest -m uat tests/uat` is the
  opt-in headless UAT run.
- `uat_server(tmp_path)` fixture: calls `assert_uat_mode_enabled()`, then
  `build_uat_app("sqlite", tmp_path)`, then `LiveServer(app)`, yields
  `(base_url, session_factory)`.
- `uat_client(uat_server)`: `httpx.Client(base_url=..., event_hooks=...)`.
- `pytest_runtest_makereport` hook: on a failed `call` phase for an item
  under `tests/uat/`, writes
  `artifacts/uat/<run-timestamp>/<test-name>.txt` containing the
  exception, the recorded request/response log, and the last response's
  full HTML body. `artifacts/` is added to `.gitignore`.

## 4. Production-safety guarantees (fail-closed)

Layered, strongest first:

1. **Structural**: the production Docker image never contains `tests/`
   and never installs `pytest` (`pip install .`, not `.[dev]`). There is
   no code path in `app/` that imports anything under `tests/`, and
   nothing in `tests/uat/` is imported by `app/*`. A grep-backed
   regression test (`tests/test_uat_harness_safety.py`,
   **outside** `tests/uat/` so it always runs in the normal suite) asserts
   this: no file under `app/` contains the substring `tests.uat`, `import
   tests`, or references a `uat`/`dev-reset`/`dev-seed` route.
2. **No new app route of any kind.** `app/main.py` gains zero new
   endpoints for this issue. `create_app()`'s only new capability is an
   additional constructor parameter (§5), which is inert unless a caller
   passes it explicitly — nothing in the running application ever does.
3. **Never reads real data-directory config.** The harness only ever
   builds its app via an explicit `database_path` (tmp file) or an
   explicit `TEST_DATABASE_URL`-sourced `database_url` — both supplied by
   the harness itself, never derived from `GRC_DATA_DIR`/`DATABASE_URL`/
   `GRC_DATABASE_PATH` in the ambient environment. There is no code path
   by which the harness can point at an operator's real data directory or
   production database, accidentally or otherwise.
4. **Explicit opt-in gate.** `GRC_UAT_MODE=1` is required before any
   fixture builds an app, and default `pytest` collection excludes
   `tests/uat/` via the marker/`addopts` split in §3 — running the
   ordinary documented `pytest` command never touches this code at all.

## 5. `app/main.py::create_app` — new `database_url` parameter

`create_app` currently accepts `database_path`/`data_dir` and always
blanks `database_url` when either is given — there is no supported way to
point it at an explicit Postgres URL without mutating the ambient
`DATABASE_URL` environment variable and the process-wide
`get_settings()` `lru_cache`, which would leak across fixtures within the
same pytest process. Since the issue explicitly requires exercising the
**real app** (not just `build_engine`/`init_db`, which is all
`test_postgres_compat.py` does today) against PostgreSQL, `create_app`
gains a third optional parameter:

```python
def create_app(
    database_path: str | None = None,
    data_dir: str | None = None,
    database_url: str | None = None,
) -> FastAPI:
```

- Mutually exclusive with `database_path`/`data_dir` (raises
  `ValueError` if combined — same "ambiguous config" philosophy as
  `reject_ambiguous_database_config`, just caught earlier with a clearer
  message since this is a programming error, not an operator
  misconfiguration).
- When given, `model_copy(update={"database_url": database_url,
  "database_path": None})` — same override shape the existing
  `database_path`/`data_dir` branch already uses, just selecting the
  other backend.
- No behavior change for every existing caller (`app/main.py:__getattr__`,
  every test in `tests/conftest.py`) — the parameter defaults to `None`
  and the existing two-parameter branch is untouched.

## 6. Scenario fixture data (deterministic, via real HTTP/domain calls)

Per RULES.md §3 and PRD §4.2, fixture setup goes through the same
domain/HTTP boundaries a real user would use — never a raw ORM insert for
anything that is itself a material compliance fact. The one exception is
constructing the deterministic login user, which mirrors
`tests/conftest.py`'s own established `test_user`/`admin_user` pattern
(a `User` row is not a compliance fact).

Scenario walkthrough (`test_soc2_control_lifecycle.py`):

1. **Login** — `GET /login`, extract CSRF, `POST /login`; assert redirect
   and a session cookie is set.
2. **SOC 2 primary / ISO available** — `GET /frameworks`; assert the SOC 2
   catalog (`catalog_key="soc2-2017-sample"`) renders with the "Primary"
   badge ahead of ISO 27001 in list order, and ISO's requirements remain
   fully browsable (`GET /frameworks/{iso_id}`).
3. **Create a control mapped to both frameworks** —
   `POST /api/registers/controls` (JSON + `X-CSRF-Token`, the real
   register-grid path) with `cadence_type="calendar"`,
   `cadence_interval_months=1`; then `POST /controls/{id}/mappings` once
   against a SOC 2 requirement and once against an ISO requirement
   (`app/routers/controls.py::add_mapping`, the real form path).
4. **Owner** — `POST /controls/{id}/owner` with a `Person` created via
   `POST /api/registers/people` first (real register-grid path).
5. **Control period** — admin `POST /control-periods` then
   `POST /control-periods/{id}/activate`.
6. **Generate occurrence** — admin
   `POST /control-periods/{id}/generate-occurrences`; assert a
   `ControlOccurrence` row now exists for the control/period.
7. **Perform** — `POST /occurrences/{id}/perform` with a `performed_on`
   date and the evidence snapshot from step 8 pre-created (see below).
8. **Evidence** — before performing, seed one `EvidenceSnapshot` via the
   real deterministic-fake-provider path the issue calls for:
   `app/aws_connector.py::build_evidence_snapshot` fed a synthetic
   `AwsCheckResult` (no real AWS call, no network) — the same function
   the real AWS connector UI/CLI path uses, just given a fake result
   instead of a live `boto3` session. `perform`'s `evidence_snapshot_id`
   field links it via the real `link_evidence` domain call.
9. **Verify rendered state** — `GET /occurrences/{id}`; assert "Performed"
   state, linked evidence, and `GET /controls/{id}` no longer lists the
   occurrence under "upcoming"/"overdue".
10. **Shared control across frameworks** — re-fetch `GET /controls/{id}`
    and assert both the SOC 2 and ISO requirement mappings are present
    simultaneously, with no duplicated `InternalControl` row (one control
    id, two mappings) — proving PRD §4.1's shared-control invariant via a
    real end-to-end path, not just a unit test on the mapping table.

`test_authorization_boundaries.py` (scenario 11):

- A **non-admin** logged-in user attempts `POST /control-periods` (admin-
  only) — assert `403` and no `ControlPeriod` row was created.
- A logged-in user submits `POST /controls/{id}/owner` with a **stale/
  missing CSRF token** — assert `400` and the control's `owner_person_id`
  is unchanged. This proves an authorization/integrity failure fails
  closed (no partial mutation, no silent bypass) rather than corrupting
  state, satisfying `.agent/LOOP.md` §9's adversarial-review question
  about retries/invalid input directly inside the UAT layer.

Both backends (SQLite always; Postgres when `TEST_DATABASE_URL` is set)
run the full lifecycle scenario via `pytest.mark.parametrize("backend",
["sqlite", "postgres"])`, with the Postgres case `skipif`'d exactly like
`test_postgres_compat.py` already does when no Postgres is available.

## 7. Documented command

```bash
GRC_UAT_MODE=1 pytest -m uat tests/uat
GRC_UAT_MODE=1 pytest -m uat tests/uat -k login_and_soc2   # select a scenario
GRC_UAT_MODE=1 TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/minigrc_uat \
    pytest -m uat tests/uat
```

`python -m app.cli uat [-k EXPR] [--postgres]` is added as a thin
convenience wrapper: it sets `GRC_UAT_MODE=1` in-process, lazily imports
`pytest` (so a minimal production install — no `[dev]` extras — never
needs `pytest` importable for any other CLI command), and calls
`pytest.main(["-m", "uat", "tests/uat", *extra_args])`, returning its exit
code. This is the "one documented command" the issue asks for; it adds no
new test-running logic of its own.

Failure artifacts land under `artifacts/uat/<run timestamp>/` (gitignored).
A machine-readable summary is available via pytest's own `--junitxml`
flag (`pytest -m uat tests/uat --junitxml=artifacts/uat/results.xml`) —
already a de facto standard consumed by CI tooling; no bespoke format is
invented.

## 8. Docs/contract updates

- `.agent/TESTING.md` — new "Headless UAT" subsection distinguishing it
  from Claude Desktop UAT (§1.5 relabeled), updated §2 test-selection
  table, and the release gate (§6) now requires headless UAT green
  wherever Claude Desktop is unavailable.
- `.agent/LOOP.md` §6/§7 — headless UAT step inserted before the Claude
  Desktop UAT gate; Desktop UAT may report `PENDING` but headless UAT may
  not be skipped once #38 exists for a user-visible change.
- `CLAUDE.md` — add the documented command to the Commands block.

## 9. Test strategy for the harness itself

- Unit: `create_app`'s new `database_url` parameter (accepts it, rejects
  combination with `database_path`/`data_dir`, existing two-parameter
  behavior unchanged) — plain `tests/test_db_init.py`-style test, runs in
  the normal suite.
- Regression/safety: `tests/test_uat_harness_safety.py` (normal suite,
  always runs) — static grep assertions from §4.1, plus a live assertion
  that `create_app()` with no arguments (closest thing to "production
  default startup") exposes no route whose path contains `uat`, `dev-`,
  or `reset`.
- Headless-UAT-on-itself: prove a passing scenario exits 0 and a broken
  one exits non-zero with an artifact written, by running
  `pytest.main` against a tiny synthetic scenario module from within a
  normal-suite test (subprocess or `pytest.main` in-process against a
  temp file) — this is the "prove failing UAT is non-zero" acceptance
  bullet, verified mechanically rather than by inspection.
- The real scenarios (§6) are exercised directly via
  `GRC_UAT_MODE=1 pytest -m uat tests/uat` during this issue's own
  verification pass and reported in the worklog, not re-simulated inside
  the normal suite.

## 10. Post-implementation adversarial review: a confirmed SQLite finding

Adversarial review (per `.agent/LOOP.md` §9's "Can SQLite and PostgreSQL
differ semantically?") surfaced a real, pre-existing correctness gap:
`app/db.py`'s SQLite engine has no explicit `poolclass`, so SQLAlchemy
defaults to `QueuePool`; combined with `check_same_thread: False` and WAL
mode, a request handled on one pooled connection/thread can transiently
fail to see a write committed moments earlier on a different one. This
was independently confirmed with nothing but two ordinary HTTP
requests — `POST /people` followed immediately by `GET` on the redirect
target, no side-channel DB access at all — missing the just-created row
roughly 1-2.5% of the time in an isolated loop (and far more often under
a tight write-heavy loop). It reproduces against the exact
`uvicorn app.main:app` invocation the `Dockerfile`'s `CMD` runs, with a
single process and a single browser tab; it is not specific to multiple
workers, multiple tabs, or this harness's own connection usage. PostgreSQL
is unaffected (MVCC gives immediate committed-read visibility).

This is filed as **issue #55** — a real defect in `app/db.py`, out of
scope for #38 to fix (it needs its own design/verification pass:
candidate fixes like constraining the SQLite engine to a single physical
connection have real throughput/concurrency tradeoffs that deserve
dedicated measurement, not a decision folded into this PR).

Two changes in this PR exist because of that finding:

- Every scenario in §6 sources ids from real HTTP responses (redirects,
  rendered links, the register-grid JSON API) rather than a
  `session_factory()` side-channel read — this was always the intended
  design (§6 as originally written), but the adversarial review confirmed
  *why* it matters beyond faithfulness to real usage: a raw DB peek
  shortly after a write measurably races the same gap issue #55 describes,
  just more often than the app's own request path does.
- The one remaining unavoidable side-channel read (the evidence-occurrence
  link, which has no rendered surface anywhere in the app) uses
  `tests/uat/harness.py::poll_for_visibility`, a small bounded retry —
  not a real fix, an honest stopgap pending #55.
