# Issue #45: auditor-facing historical timeline / point-in-time reconstruction

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a browsable, read-only history/timeline view over two representative
event-sourced domains — policy-version lifecycle (#31) and control
occurrences (#11) — plus an "as of" point-in-time reconstruction for
policy versions, proving `app/events.py`'s event store is usable by an
actual person, not just an internal correctness check. See
`docs/superpowers/specs/2026-08-20-issue45-historical-timeline-design.md`
for the full design.

## What changed

- `app/history.py` (new): `get_event_history` (generic per-aggregate
  chronological event listing) and `reconstruct_policy_as_of` (pure,
  in-memory replay of policy_version events bounded by a cutoff — never
  writes to the database, never uses a nested-transaction/SAVEPOINT
  rollback approach, deliberately, given #65's known pysqlite/SAVEPOINT
  hazard).
- `app/routers/policies.py`: `GET /policies/{policy_id}/history` (event
  timeline across every version + optional `?as_of=` reconstruction).
- `app/routers/occurrences.py`: `GET /occurrences/{occurrence_id}/history`
  (event timeline for one control occurrence).
- `app/templates/policies/history.html`,
  `app/templates/occurrences/history.html` (new) + a "View history" link
  added to each domain's existing detail page.
- **No schema change, no migration** — pure read/query logic over the
  existing `DomainEvent` table.

## A real bug found via testing (stale ORM relationship)

`reconstruct_policy_as_of`'s first draft read `policy.versions` (a lazy
relationship) instead of querying `PolicyVersion` directly. A test
adding a second version to the same `Policy` object, in the same
long-lived session, after the relationship had already been accessed
once, found `policy.versions` returned a stale cached collection missing
the new version. Fixed by querying `PolicyVersion` explicitly by
`policy_id` — the same "stale relationship" bug class this session has
now found repeatedly (#28, #42).

## Test strategy and results

- **Unit** (`tests/test_history.py`, 5 tests): chronological listing;
  reconstruction before any event; reconstruction strictly between two
  known events (the issue's own required verification); cross-version
  supersession at three cutoffs; proof reconstruction never mutates the
  real `PolicyVersion` row.
- **Browser/E2E** (`tests/test_history_routes.py`, 7 tests): full
  lifecycle through real HTTP, history listing, "as of" at today vs.
  yesterday, 404s for unknown objects, graceful handling of a malformed
  `as_of` (no 500), and an HTTP-layer read-only proof (`DomainEvent`
  count unchanged across repeated requests).
- **Headless UAT** (`tests/uat/test_history.py`, 2 scenarios): drives a
  real policy through submit → approve → make-effective over real HTTP;
  "as of today" strictly between approve and make-effective correctly
  shows "approved" not "effective." `GRC_UAT_MODE=1 pytest -m uat
  tests/uat/test_history.py` — **PASS**.
- **Regression**: full `pytest -q` — see below.
- **Lint/format**: `ruff check .` / `ruff format --check .` clean.
- **SQLite/PostgreSQL**: no migration in this issue; every query is
  plain SQLAlchemy Core with no backend-specific SQL.
- **Claude Desktop UAT: PENDING** — runbook below.

## Claude Desktop UAT runbook (PENDING — not yet executed)

1. Build/commit: this PR's branch/head commit.
2. Environment: SQLite (default), a running `uvicorn app.main:app`.
3. Prerequisites: an operator user; a policy with at least two versions
   moved through submit/approve/effective/superseded (or use an existing
   seeded policy).
4. Persona: any logged-in user (read-only, no role restriction).
5. Scenario: open a policy's detail page, click "View history / reconstruct
   as of a past date," confirm every lifecycle event is listed with a
   real timestamp and actor; pick a date between two known transitions
   and confirm the reconstructed table shows the correct in-between
   state, not current state.
6. Negative case: pick a date before the policy existed — confirm the
   page says no version existed yet, rather than fabricating a state.
7. Persisted-result check: confirm no new `DomainEvent`/`AuditEvent` row
   was created by viewing history or reconstructing any date.
8. Repeat steps 5–6 for a control occurrence's `/history` page (event
   listing only, no "as of" reconstruction for this domain).

## Known deferred/untested paths

- No point-in-time query language across every event-sourced domain —
  explicit non-goal in the issue.
- No history view for control-definition versions (#42) or evidence
  artifacts (#32) yet — two domains already satisfy the acceptance
  criteria; a future issue could extend the same pattern.
