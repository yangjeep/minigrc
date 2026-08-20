# Issue #72 (recurrence): real fix for the readiness-stage UAT flake

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** fix

## Summary

Issue #72's original occurrence (during #34's PR CI run) was diagnosed at
the time as "possibly a missing status-code assertion masking the real
failing action," and #73 added that assertion. It recurred identically in
#18's PR (#75) CI run with the assertion in place and passing —
proving #73's fix was a legitimate diagnostic improvement but not the
actual root cause.

## Root cause

While fixing #18's own new UAT flakes (raw DB peeks immediately after an
HTTP POST — see `docs/worklog/2026-08-19-issue18-oidc-role-mapping.md`),
the mechanism became clear: this is issue #57's documented residual
(`tests/test_sqlite_read_after_write.py`) — a state-changing POST's
response can be sent to the client before that same request's `get_db`
dependency cleanup (`session.commit()`) actually lands, under thread-pool
contention, measured at up to ~29% miss rate on GitHub Actions CI vs.
~0.1-0.3% locally. `test_readiness_stage.py` hits this in **two** shapes:

1. `_current_stage(client)` — a GET immediately following a state-
   changing POST, expecting to observe that POST's effect. This is a
   cross-HTTP-request race, not a raw DB peek, so the existing
   `poll_for_visibility` helper (built for DB-peek reads) doesn't apply
   directly — a new HTTP-level equivalent was needed.
2. The owner-backfill block's own `session.scalars(select(InternalControl))`
   peek, run immediately after the `generate-starter-controls` POST — if
   this peek runs before that POST's commit lands, it silently backfills
   zero owners (no error, no exception — just an empty loop), and the
   stage then legitimately reads back as "Foundation incomplete" because
   the app is correctly reporting truthful state for what actually
   happened. This explains why the failure was 100% reproducible in
   symptom (always the same two stage strings) despite being a genuine
   timing race, not a logic bug: the *test's own* backfill silently did
   nothing, which is a much larger and more consistent visible effect
   than a one-request stage-read miss would be.

## Fix

- `_assert_stage_eventually(client, expected)` (new, in
  `tests/uat/test_readiness_stage.py`) retries `_current_stage(client)`
  for a bounded 2s window before asserting — the HTTP-level analogue of
  `poll_for_visibility`, used at all six stage-transition checkpoints in
  the scenario (only one of which had actually flaked so far, but all six
  share the identical race shape).
- The owner-backfill block now calls `poll_for_visibility` to wait for
  the just-created controls to actually be visible *before* backfilling
  — closing the actual gap that produced the observed symptom.
- Applied the same `poll_for_visibility` treatment to the two genuinely
  at-risk raw DB peeks in `tests/uat/test_oidc_login.py` (issue #76,
  filed while investigating this) — new-user/identity creation and the
  post-second-login re-check. The peek that checks for *absence* of a
  row after a rejected (no-write) login was left as a plain read, per
  the same reasoning already documented in #75's worklog: nothing new is
  written on that path, so there is no fresh commit to race.

## Test results

- `tests/uat/test_readiness_stage.py`: 3/3 local reruns pass (the race
  cannot be forced to reproduce locally — CI's higher thread-pool
  contention is what originally surfaced it, consistent with #57's own
  measured CI-vs-local gap).
- `tests/uat/test_oidc_login.py`: 3/3 local reruns pass.
- Full regression suite / full headless UAT: see PR.
- Lint/format: clean.

## Known residual risk

This closes the two specific, identified race windows in these two
files. The general #57 residual is architectural (a real fix means
either committing explicitly in every route handler or moving off sync
`Session` entirely — explicitly out of scope, per #57's own description)
— any *other* UAT scenario with a bare cross-request or DB-peek
read-immediately-after-a-write pattern remains theoretically exposed to
the same class of flake until it independently hits CI's higher-
contention environment. Not attempting a blanket audit of every existing
UAT file in this fix — scoped to the two files with an actual observed
or closely-related failure.

## Addendum (same day): a third recurrence, and a more robust fix

Despite the `poll_for_visibility`-before-backfill fix above,
`test_stage_progresses_and_regresses_with_real_state` failed a **third**
time in PR #78's (issue #24, unrelated) CI run, with the identical
symptom (`'Foundation incomplete' == 'Operating controls'`). A rerun
with zero code changes passed cleanly — confirming this remained a
genuine timing race, not a logic bug, but also proving the previous fix
reduced rather than eliminated the failure rate.

Re-reading `app/onboarding.py`/`app/readiness.py` confirmed the stage
logic itself is correct (`assign_owners` is complete only when no
control has both an empty `owner` and no `owner_person_id`); the
remaining exposure was structural: the owner-backfill step was still a
**raw DB write from an entirely separate connection** (the test's own
`session_factory()`), which the subsequent `_current_stage` HTTP read
then had to race across *two* hops (the original POST's commit, then
the test's own raw commit) rather than one.

**Real fix**: replaced the raw DB peek-and-write with the real register
API the app itself exposes — `GET /api/registers/controls` (retried
under the same bounded-poll pattern as `_assert_stage_eventually`, since
listing immediately after the generate-starter-controls POST is the
same cross-request race) followed by `PATCH
/api/registers/controls/{id}` (matching the exact pattern already
proven in `tests/uat/test_readiness_dashboard.py`) to assign each
owner. This collapses the double-hop into the same single, already-
mitigated "GET/PATCH shortly after a POST" shape every other step in
this test already handles correctly — no more out-of-band raw session
write for the test to reconcile against the server's own view at all.
5/5 local reruns pass after this change (up from the previous fix's 3/3
— not proof of elimination given the race can't be forced locally, but
consistent with removing a genuine additional hop).
