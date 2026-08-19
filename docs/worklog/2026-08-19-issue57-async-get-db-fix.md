# Issue #57: response-sent-before-commit ordering on sync `get_db`

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** fix

## Summary

`app/deps.py::get_db` was a plain (sync) generator dependency. FastAPI
wraps a sync generator dependency's setup *and* teardown via
`contextmanager_in_threadpool` (`fastapi/dependencies/utils.py`), which
dispatches both through `run_in_threadpool` — a separate call into the
same worker-thread pool the endpoint itself runs on. Confirmed directly
against the installed `fastapi==0.141.1` source
(`fastapi/routing.py`'s `RouteGroup.handle`/`_solve_dependencies`): the
HTTP response is sent to the client *inside* the `async with
self._solve_dependencies(...)` block, and dependency cleanup
(`get_db`'s `session.commit()`) only runs when that block exits, *after*
the response has already gone out. For a sync generator, exiting
requires an additional `run_in_threadpool` round trip (acquiring a
thread-pool slot, dispatching, awaiting) — a real scheduling gap during
which the event loop can serve a *new* incoming request (e.g. the
client's own immediate follow-up to a redirect) before the previous
request's commit has actually executed.

Filed while investigating #55; confirmed backend-independent (removing
SQLite's WAL mode entirely still reproduced it) and specific to sync
generator dependencies with meaningful post-yield work.

## Fix

`get_db` is now `async def`. FastAPI's `_solve_generator` wraps an async
generator dependency with `asynccontextmanager` and enters/exits it
directly via `await stack.enter_async_context(cm)` / the exit stack's
own `__aexit__` — both run on the event loop, with **no thread-pool hop
at all**. This does not make any database call actually asynchronous
(the `Session` and its `commit()`/`rollback()`/`close()` calls stay
fully synchronous); it only removes the scheduling gap the thread-pool
dispatch introduced for the *cleanup* phase specifically.

## Verification

- **Structural regression test**
  (`tests/test_get_db_async_cleanup.py::test_get_db_is_an_async_generator_dependency`) —
  confirmed to fail against the reverted (sync) version, passes with the fix.
- **Permanence test** (same file) — the same eventually-consistent,
  bounded-retry pattern `tests/test_sqlite_read_after_write.py`
  established for #55, deliberately *not* asserting a hard zero-miss
  count (see below).
- **Manual measurement** (not encoded as a hard assertion, per the
  lesson #55's own CI surprise taught): 0 immediate misses across
  5×150 sequential `POST`+`GET` round trips (750 total), and 0/150 even
  under deliberate artificial CPU contention (6 CPU-bound Python
  processes pinning all 4 available cores) — a much larger improvement
  than #55's fix alone showed under the same local conditions. This is
  evidence, not a guarantee across every possible CI runner.
- **Concurrency regression check** (the real risk this specific fix
  could introduce): since `get_db`'s cleanup now runs *inline on the
  event loop* rather than in a worker thread, a slow/lock-contended
  `session.commit()` could in principle stall the whole server rather
  than just one request. Measured directly: 50 concurrent clients
  (login + 3 writes each) — 0 errors, ~12s (actually faster than the
  #55-only baseline's ~15-21s, plausibly from removing the thread-pool
  round trip's own overhead). At 80 concurrent clients, both this fix
  *and* the pre-existing #55-only baseline (tested side by side,
  `git stash` swap) collapse identically (~35-36s, ~80/80 client-side
  timeouts) — a pre-existing SQLite/single-process scalability ceiling
  around 50-80 concurrent clients that exists with or without this fix,
  not a regression this change introduces. SQLite is this app's
  low-ops/simple-deployment backend by design (PRD §6.1), not a
  high-concurrency one; addressing that ceiling is out of scope here.
- **Full regression suite:** `pytest` (default) — green (see PR for
  exact count).
- **Headless UAT:** `GRC_UAT_MODE=1 pytest -m uat tests/uat` — green.
- **Lint/format:** clean.

## Files changed

- `app/deps.py` — `get_db` is now `async def` (body unchanged); docstring
  explains why.
- `tests/test_get_db_async_cleanup.py` (new, normal suite).

## Known deferred/untested paths

- Not verified against PostgreSQL directly (the underlying mechanism is
  backend-independent by construction — it's about FastAPI's dependency
  cleanup dispatch, not the database — so no PostgreSQL-specific
  behavior is expected, but this wasn't independently measured against
  a live PostgreSQL server in this sandbox).
- The pre-existing ~50-80-concurrent-client ceiling noted above remains
  unaddressed; tracked as a pre-existing characteristic, not a new issue.
- `require_login`/`require_admin`/`verify_csrf*` are plain functions
  (not generators) and were never in scope for this fix; confirmed via
  grep that `get_db` is the only `yield`-based dependency in `app/deps.py`.
