# Issue #55: SQLite read-after-write visibility gap

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** fix (partial — remainder tracked as issue #57)

## Summary

`app/db.py`'s SQLite engine had no connection-invalidation strategy, so
SQLAlchemy's default `QueuePool` could hand a request a pooled connection
that hadn't yet observed a write committed moments earlier on a
*different* pooled connection — reproducible with nothing but two
ordinary sequential HTTP requests through the real app (`POST /people` →
`GET` the redirect target), no concurrency and no side-channel database
access involved.

**What shipped:** `app/db.py::_invalidate_on_checkin` hard-invalidates
every SQLite connection the instant it's checked back into the pool
(wired via SQLAlchemy's `checkin` event), so every checkout always opens
a fresh physical connection — deterministically, with no timing race —
while still going through the *default* `QueuePool`'s normal bounded
concurrency. This is a real, measured improvement: roughly a 10-40x
reduction in observed miss rate.

**What did not ship, and why:** the investigation below found that the
remaining, much rarer residual (~1 miss per several hundred sequential
request cycles) is **not** a SQLite issue at all — it's a FastAPI
response-vs-dependency-cleanup ordering behavior that would affect any
backend equally. That is filed and tracked separately as **issue #57**,
explicitly out of scope for #55 (a real fix there means touching the
commit boundary of every route handler, or moving off sync SQLAlchemy
`Session`, either of which is an architectural decision, not a
mechanical one). This worklog documents the full path to that
conclusion because two intermediate fix attempts were tried, measured,
and rejected first, and that record is worth keeping.

## Root-cause investigation, part 1: connection pooling (the part that #55 actually fixes)

- A plain multi-threaded SQLAlchemy `Session` repro (write on one thread,
  read on another, no ASGI app involved) did **not** reproduce the bug
  (0/200), nor did the existing in-process `TestClient`-based pattern
  (0/300 with the original `QueuePool` default). Only a real socket
  connection to a real running `uvicorn` server reproduced it.
- Baseline (original code): 1/80 misses in one isolated run against the
  real app; the original adversarial review (during #38) observed up to
  ~50% under tighter write-heavy loops.

### Attempt 1 — `poolclass=NullPool`: fixed most of it, broke concurrency

`NullPool` opens a brand-new physical connection for every checkout and
discards it on return — no connection is ever reused. This reliably
eliminated the staleness in every repro attempted at the time (hundreds
of iterations, 0 misses). An adversarial review of this fix (before it
was committed) ran a concurrent-load smoke test — 25 simultaneous logins
against the same app — and found real `sqlite3.OperationalError: database
is locked` 500s (reproducible, several per run). The original,
unmodified code showed **zero** such errors under the identical load
(5/5 clean runs).

Root cause of the regression: `NullPool` has no cap on how many physical
connections can be open at once. The original `QueuePool` (`pool_size=5,
max_overflow=10`) naturally throttles concurrent DB-layer activity to at
most 15 simultaneous connections; requests beyond that wait at the
Python pool-checkout layer instead. With `NullPool`, all 25 requests
immediately open 25 simultaneous connections and all immediately attempt
to write; enough of them queue behind SQLite's single-writer lock for
long enough that some exceed the 5-second `busy_timeout`. Rejected.

### Attempt 2 — `pool_recycle=0`: fixed staleness *almost* always

Keeps the default, bounded `QueuePool` but tells it to treat every
connection as immediately expired, forcing a fresh physical connection
on every checkout while preserving normal concurrency throttling. Fixed
both of attempt 1's problems (0 lock errors across 5 runs of
25-concurrent logins; comparable throughput to baseline at 50-100
concurrent clients). But repeating the staleness test several more times
surfaced one failure. `SQLAlchemy`'s own `_ConnectionRecord.get_connection`
source explains why: the recycle check is `time.time() - self.starttime
> self.__pool._recycle`, i.e. with `_recycle == 0`, a **strict**
inequality against wall-clock time. SQLAlchemy's own source comment
acknowledges `time.time()` isn't guaranteed sub-second precision; a
connection created and reused within the same clock tick has
`elapsed == 0.0`, which fails `> 0`, so it is reused *unrecycled*. A
genuine, if rare, timing race in the fix's own mechanism. Rejected in
favor of something with no timing dependency at all.

### Shipped fix — hard-invalidate on checkin (no timing dependency)

`Pool._ConnectionRecord.invalidate()` (hard, the default) closes the
underlying DBAPI connection **immediately and synchronously** and sets
its cached handle to `None`. The *next* `get_connection()` call checks
`if self.dbapi_connection is None` — an unconditional identity check, not
a timestamp comparison — and reconnects. Hooking SQLAlchemy's `checkin`
event to call `connection_record.invalidate()` on every SQLite checkin
forces a fresh connection on every subsequent checkout with no possible
timing race.

Verification at the time: 6 consecutive runs of a 150-iteration
zero-tolerance staleness test (900 total round trips) — 0 misses. 5 runs
of 25-concurrent logins — 0 lock errors. 50-concurrent login+3-writes
load — 0 errors, throughput matching attempt 2.

## Root-cause investigation, part 2: the residual (why this fix alone isn't "done")

Running the same zero-tolerance staleness test many more times (as part
of adversarially reviewing the fix itself, not just accepting the first
clean batch) surfaced an occasional failure — roughly 1 per several
hundred iterations, clearly rarer than either rejected attempt but not
zero. Two things ruled out every remaining SQLite-specific theory:

1. Removing `PRAGMA journal_mode=WAL` entirely (default rollback-journal
   mode) still showed the same rare failure. Rollback-journal mode's
   writer holds an *exclusive* lock for the whole transaction — a reader
   that can proceed at all has, by definition, already seen the writer
   either commit or roll back. If staleness is possible even there, it
   cannot be a WAL-snapshot-timing issue.
2. Reading FastAPI's actual installed source (`fastapi==0.141.1`,
   `fastapi/routing.py`) showed that for a matched route with
   dependencies, the response is sent to the client **before** the
   `AsyncExitStack` holding each dependency's post-`yield` cleanup code
   exits:

   ```python
   async with self._solve_dependencies(...) as solved_result:
       response = await route.app.get_response_for_scope(scope)
       ...
       await response(scope, receive, send)  # response fully sent HERE
   # only now does cleanup (get_db's session.commit()) run
   ```

   `app/deps.py::get_db` is a *sync* generator dependency. FastAPI wraps
   sync generator dependencies via `contextmanager_in_threadpool`
   (`fastapi/dependencies/utils.py:573`), dispatching **both** the
   pre-yield setup and the post-yield cleanup (`session.commit()`) as
   separate `run_in_threadpool` calls into the same worker thread pool
   the endpoint function itself runs on. The response can therefore reach
   the client while the commit is still only *scheduled*, not yet
   executed, if the thread pool is at all contended.

This is backend-independent — nothing about it is SQLite-specific — and
explains every remaining observation (persists with any SQLite pool
strategy tried, persists with WAL removed entirely, is rare and
timing-dependent). Filed as **issue #57**.

## Why not `StaticPool`/`pool_size=1` (considered, measured, rejected)

`StaticPool` (or `QueuePool` pinned to exactly one connection) makes
every request share literally the *same* physical connection; genuinely
concurrent use of one `sqlite3` connection object from two threads at
once (not merely sequential) risks interleaved transaction state — a
data-corruption risk, not just a staleness one. `pool_size=1,
max_overflow=0` was measured directly: it does fix the pooling-level
staleness (0/150), but it also serializes *every* request — including
plain reads — through one connection; at 50 concurrent clients this
produced ~49/50 client-side timeouts (25s+ each). Rejected outright once
measured.

## `:memory:` — explicitly rejected

Forcing a fresh connection on every checkout gives `sqlite:///:memory:`
its own private, empty in-memory database per connection — silently
breaking any code that relied on state persisting across connections.
Nothing in this app currently passes `:memory:` (`grep` confirmed across
`app/`, `tests/`, `migrations/`, `docs/`, `charts/`, `compose.yaml`,
`.env.example`), so `build_engine` now raises `ValueError` immediately if
it ever is.

## Files changed

- `app/db.py` — `_invalidate_on_checkin` (new), wired via the `checkin`
  event for the sqlite dialect only; explicit `:memory:` rejection;
  docstring covering the mechanism and the two rejected attempts.
- `tests/test_sqlite_read_after_write.py` (new, normal suite) —
  `test_sqlite_engine_invalidates_connections_on_checkin` (structural,
  deterministic), `test_postgres_engine_pooling_is_unaffected`,
  `test_sequential_http_write_then_read_is_eventually_consistent` (a
  bounded-retry eventual-consistency assertion — **not** a zero-miss
  assertion, since that would be false given #57; see that test's and
  this file's docstrings), and
  `test_moderately_concurrent_writes_do_not_lock_the_database` (25
  concurrent logins — regression coverage for attempt 1's rejected
  finding).
- `tests/uat/harness.py`, `tests/uat/test_soc2_control_lifecycle.py` —
  comments updated from "tracked as #55, unresolved" to reflect the fix
  and the follow-up #57; `poll_for_visibility` kept as defense-in-depth.
- `docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md` §10
  — updated to record the fix landed and #57 was filed.

## Verification

- **Regression tests:** the eventual-consistency test and the
  concurrency test were each confirmed to fail against the relevant
  *rejected* attempt (`NullPool` for the concurrency test; the original
  code for the eventual-consistency test) before being finalized against
  the shipped fix. Repeated runs of the final test file (8+ consecutive
  invocations) showed no flakes: the bounded-retry design means any rare
  #57-caused delay simply resolves within the test's own retry window,
  which is the truthful, intended behavior — not a workaround.
- **Unit tests:** `test_sqlite_engine_invalidates_connections_on_checkin`,
  `test_postgres_engine_pooling_is_unaffected` — PASS.
- **Targeted:** `tests/test_db_init.py`, `tests/test_sqlite_integrity.py`,
  `tests/test_postgres_compat.py`, `tests/test_create_app_database_url.py`,
  `tests/test_cli.py` — 22 passed, 1 skipped (Postgres live-migration
  test, no local Postgres in this sandbox).
- **Full regression suite:** `pytest` (default) — green (see PR for
  exact count).
- **Headless UAT:** `GRC_UAT_MODE=1 pytest -m uat tests/uat` — green.
- **SQLite vs PostgreSQL equivalence:** explicitly preserved —
  `test_postgres_engine_pooling_is_unaffected` pins that only the sqlite
  branch changed. Whether issue #57 also reproduces against PostgreSQL
  is unverified (no local PostgreSQL in this sandbox) and is called out
  explicitly in #57's acceptance criteria.
- **Lint/format:** clean.
- **Adversarial review:** performed multiple times over the course of
  this investigation — the first (against attempt 1) is what surfaced
  the concurrency regression that killed it; a later one (repeating the
  "fixed" test many times) is what surfaced the #57 residual in the first
  place, rather than accepting an initially-clean batch of runs at face
  value.

## Post-push correction: the frequency assertion wasn't portable

The version of this fix first pushed for review included a third
assertion on `test_sequential_http_write_then_read_is_eventually_consistent`:
an upper bound of 2 *immediate* misses per 150 iterations, reasoned from
local measurements (~0.1-0.3% post-fix vs ~1-2.5%+ pre-fix). CI (GitHub
Actions) immediately falsified the premise: the same code, same test,
showed **43/150 (≈29%) immediate misses** — permanence still held (every
one resolved within the retry window), but the frequency was two orders
of magnitude higher than local measurements.

This is itself an important data point for #57 (posted to that issue):
the residual's manifestation rate appears dominated by host CPU/
thread-scheduling contention, which varies enormously across
environments — consistent with the root cause (a threadpool-dispatched
commit racing an already-sent response; a busier/more constrained
thread pool makes the race window matter far more often). Practically,
this means #57 is not a rare curiosity confined to unusual conditions —
on modest self-hosted hardware (this app's actual target deployment
profile) it could be frequent, not rare.

The frequency assertion was removed as a result — it cannot be made
portable across environments without either an environment-specific
threshold (bad practice) or an impractically large sample size, and a
fixed threshold calibrated against one environment is guaranteed to
misfire on another. The structural test
(`test_sqlite_engine_invalidates_connections_on_checkin`) remains the
real, portable regression coverage for this fix's actual mechanism; the
permanence test's retry window was widened from 1.0s to 3.0s for extra
margin given the now-confirmed cross-environment variability.

## Known deferred/untested paths

- `app/worker.py` was not independently load-tested against this fix,
  but it uses the same `app/db.py::build_engine` helper, so it inherits
  the same fix automatically.
- The pre-existing ~50-100-concurrent-client scalability ceiling this
  investigation surfaced (present in the original code too, unrelated to
  #55) is not addressed — SQLite is documented as the "low-ops/simple
  deployment" backend, not a high-concurrency one; out of scope here.
- Issue #57 (the actual remaining root cause) is unresolved and requires
  its own architectural decision before a fix is attempted — its
  practical severity was revised upward after the CI measurement above.
