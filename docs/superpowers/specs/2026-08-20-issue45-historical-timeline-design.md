# Issue #45: auditor-facing historical timeline / point-in-time reconstruction

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Goal

Give an operator or auditor a browsable way to reconstruct historical
state — "what was true about this object as of date X" and "why/when did
this fact change" — derived from `app/events.py`'s authoritative event
history, distinct from #33/#14's current-state dashboard and #34's static
audit-package export. See issue #45 for the full framing and its own gap
analysis against those three.

## Domain selection

Per the issue's own "prove it for one or two domains" instruction:
**policy-version lifecycle (#31)** as the primary domain (full event
timeline + "as of" reconstruction) and **control occurrences (#11)** as a
lighter second domain (event timeline only, no reconstruction needed
since only one "as of" proof is required). Both are already event-sourced
via `app/events.py`, satisfying the issue's dependency on #21/#22.

## Design

### Generic per-object event history

`app/history.py::get_event_history(session, *, aggregate_type, aggregate_id)`
— a plain `select(DomainEvent).where(...).order_by(recorded_at, aggregate_sequence)`.
Works for any event-sourced aggregate; used for both policy versions and
control occurrences.

### "As of" reconstruction — deliberately NOT a nested-transaction replay

The obvious-looking approach — run `app/policy_lifecycle.py`'s real
projector functions inside a `session.begin_nested()` SAVEPOINT bounded
by the cutoff, then roll it back — was rejected. This repo has a known,
real pysqlite/SAVEPOINT interaction hazard (issue #65): a `rollback()`
after an earlier `commit()` in the same session can silently fail to
undo a released SAVEPOINT's writes. A read-only history view must never
be the thing that risks touching the live projection table, even
transiently and even if rolled back correctly today — the failure mode
above is exactly the kind of "looks safe, isn't" trap this repo has
already been burned by once.

Instead, `reconstruct_policy_as_of` replays policy_version events into a
**pure, in-memory `PolicyVersionSnapshot` dataclass**, mirroring
`app/policy_lifecycle.py::POLICY_VERSION_PROJECTORS`' transitions exactly
(one small `_apply_*` function per event_type) but never issuing a single
database write. This duplicates a modest amount of transition logic —
an accepted, deliberate cost for a correctness/safety property that a
"just reuse the real projectors" approach cannot honestly claim to have
in this specific codebase.

A version whose `created_at` (a plain, immutable-by-convention fact — see
`PolicyVersion`'s own docstring) is after the `as_of` cutoff is simply
omitted from the result — it did not exist yet, and is not part of the
replayed lifecycle state either way.

### Routes

`GET /policies/{policy_id}/history` (optional `?as_of=YYYY-MM-DD`) and
`GET /occurrences/{occurrence_id}/history` — both `require_login` only,
no new authorization tier (matches the issue's explicit "does not add
authorization roles beyond #37").

## A real bug found via testing, not reasoning

`reconstruct_policy_as_of`'s first draft read `policy.versions` (an ORM
lazy relationship) instead of querying `PolicyVersion` directly. A test
that adds a second version to the same `Policy` object, in the same
long-lived session, after the relationship had already been accessed
once, found that `policy.versions` returned a **stale, cached**
collection missing the new version — a real SQLAlchemy footgun, not a
hypothetical one. This wouldn't manifest through the actual HTTP route
(a fresh session per request never has this staleness), but the
`reconstruct_policy_as_of` function itself is safer without depending on
that assumption — fixed to query `PolicyVersion` explicitly by
`policy_id` every time, matching this session's now-familiar "stale
relationship" bug class (previously found in #28, #42's own new code).

## Test strategy and results

- **Unit** (`tests/test_history.py`, 5 tests): chronological event
  listing; reconstruction before any event (draft); reconstruction
  strictly between two known events matches expected mid-transition
  state (the issue's own required verification); cross-version
  supersession reconstruction at three different cutoffs, including a
  version that doesn't exist yet at an earlier cutoff; proof that
  reconstruction never mutates the real `PolicyVersion` row.
- **Browser/E2E** (`tests/test_history_routes.py`, 7 tests): full
  lifecycle through real HTTP, history listing, "as of" reconstruction
  at today vs. yesterday, 404 for unknown policy/occurrence, graceful
  handling of a malformed `as_of` query param (no 500), and an HTTP-layer
  read-only proof (`DomainEvent` row count unchanged across repeated
  history/as-of requests).
- **Headless UAT** (`tests/uat/test_history.py`, 2 scenarios, PASS):
  drives a real policy through submit → approve → make-effective over
  real HTTP, confirms the history page lists every event, and confirms
  "as of today" strictly between approve and make-effective shows
  "approved" not "effective" — proving the reconstruction reflects a
  real point in time, not just current state. A reader can view history
  (read-only, no role restriction).
- **Regression**: full `pytest -q` — see PR for final count.
- **Lint/format**: clean.
- **SQLite/PostgreSQL**: no schema change in this issue at all — pure
  read/query logic over the existing `DomainEvent` table, using only
  plain SQLAlchemy Core constructs. No migration needed.
- **Claude Desktop UAT: PENDING** — requires a running deployment;
  runbook in the worklog.

## Known deferred/untested paths

- No general point-in-time query language across every event-sourced
  domain — explicit non-goal in the issue ("does not require this... on
  day one").
- No history/reconstruction view for control-definition versions (#42)
  or evidence artifacts (#32) — both are event-sourced and could get the
  same treatment later, but two domains already satisfy the issue's own
  acceptance criteria.
